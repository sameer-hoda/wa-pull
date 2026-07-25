"""
Handlers — process incoming MeChat messages and route to the right action.

This is the core logic of the bot.  Each handler:
  1. Reads the user's message
  2. Parses intent (via LLM or keyword)
  3. Executes the action
  4. Sends the response back to MeChat
"""

import re
import datetime

import config
import llm
import sender
import session as session_module
import task_extractor
import okf_reader
import db
import contact_resolution
from monitor import monitor
from session import Session, session_manager
from scheduler import _build_bulletin, _format_ist, _format_date, _format_activity_text, _now_ist


def handle_message(text: str) -> None:
    """Main entry point — handle an incoming MeChat message."""
    text = text.strip()
    if not text:
        return

    if text.lower() == "/pull":
        session = session_manager.get_session()
        if session is not None:
            sender.send_to_mechat(
                f"⚠️ Session `{session.session_id}` is already active with "
                f"{len(session.tasks)} tasks.\n\n"
                "Type */pull* to start a fresh session."
            )
            return
        _handle_pull()
        return

    if text.lower() == "/hourlyb":
        _handle_hourlyb()
        return

    session = session_manager.get_session()

    if session is None:
        _handle_no_session(text)
        return

    has_tasks = len(session.tasks) > 0
    intent = llm.parse_intent(text, has_task_list=has_tasks)
    action = intent.get("action", "unknown")

    session.touch()

    # Check pending send confirmation FIRST
    if session.get_pending_send() is not None:
        if action == "confirm":
            _handle_confirm_send(session)
            return
        elif action == "send":
            session.clear_pending_send()
        else:
            session.clear_pending_send()
            sender.send_to_mechat("❌ Send cancelled.")
            # Fall through

    if action == "archive":
        _handle_archive(session, intent.get("task_numbers", []))
    elif action == "context":
        _handle_context(session, intent.get("task_number"))
    elif action == "action":
        _handle_action(session, intent.get("task_number"))
    elif action == "send":
        _handle_send(session, intent.get("task_number"),
                     intent.get("option_letter"))
    elif action == "get_more":
        _handle_get_more(session)
    elif action == "pull":
        _handle_pull()
    else:
        _handle_unknown(session, text)


def _handle_pull() -> None:
    """Generate top-15 tasks with progress messages."""
    # Step 1: Load context
    sender.send_to_mechat(
        "🔄 _Scanning your chats and OKF…_\n"
        "⏳ This takes 2-3 min. Updates at 50% and 90%."
    )

    okf_text, recent_chats, jid_map = task_extractor.load_context()

    # Step 2: 50% — extracting
    sender.send_to_mechat("⏳ _50% — Extracting tasks via AI…_")

    tasks = task_extractor.extract_tasks_from_context(
        okf_text, recent_chats, jid_map
    )

    if not tasks:
        sender.send_to_mechat("📭 No pending tasks found. You're all caught up!")
        return

    # Step 3: 90% — formatting
    sender.send_to_mechat("⏳ _90% — Formatting task list…_")

    session = session_manager.create_session(tasks)
    timeout_min = config.SESSION_TIMEOUT_SECONDS // 60
    message = task_extractor.format_task_list(tasks, session_id=session.session_id)

    sender.send_to_mechat(
        f"✅ *Session `{session.session_id}`* ({timeout_min}min)\n\n{message}"
    )


def _handle_hourlyb() -> None:
    """Generate the full hourly bulletin — tasks only (v5: no done-today/left-on-read)."""
    now = _now_ist()

    # If a session is already active, warn and replace
    existing = session_manager.get_session()
    if existing is not None:
        sender.send_to_mechat(
            f"🔄 Replacing active session `{existing.session_id}` with an hourly bulletin…"
        )

    # 1. Load context + extract tasks
    sender.send_to_mechat("🔄 _Building your hourly bulletin…_")
    okf_text, recent_chats, jid_map = task_extractor.load_context()
    tasks = task_extractor.extract_tasks_from_context(okf_text, recent_chats, jid_map)

    if not tasks:
        sender.send_to_mechat(
            f"🕒 *Hourly Bulletin* · {_format_ist(now)}\n\n"
            "📭 No pending tasks found. You're all caught up!\n"
            "_hourlyB · hourly bulletins · free text · self learning_"
        )
        return

    # 2. Create session + send bulletin
    session = session_manager.create_session(tasks)
    bulletin = _build_bulletin(session.session_id, tasks, now)
    sender.send_to_mechat(bulletin)
    print(f"[hourlyb] Bulletin sent — {len(tasks)} tasks")


def _handle_no_session(text: str) -> None:
    lower = text.lower()
    if re.search(r"\b(task|archive|context|action|send)\b", lower) or \
       re.search(r"\b\d+\b", lower):
        sender.send_to_mechat("⚠️ No active session. Generating task list…")
        _handle_pull()
        return
    sender.send_to_mechat(
        "👋 _No active session._\n"
        "A new hourly bulletin starts at the top of every hour "
        f"({config.IST_START_HOUR:02d}:00–23:00 IST).\n"
        "Type */pull* to generate one on demand.\n"
        "_hourlyB · hourly bulletins · free text · self learning_"
    )


def _handle_archive(session: Session, task_numbers: list[int]) -> None:
    if not task_numbers:
        sender.send_to_mechat('⚠️ Which task numbers? e.g. "archive 1, 3, 5"')
        return

    valid_nums = [n for n in task_numbers if session.get_task(n) is not None]
    if not valid_nums:
        sender.send_to_mechat(
            f"⚠️ Tasks {task_numbers} not found. "
            f"Valid: {session.get_active_task_numbers()}"
        )
        return

    archived = session.archive_tasks(valid_nums)
    lines = [f"📦 *Archived {len(archived)}*"]
    for t in archived:
        lines.append(f"  ✓ ~{t.get('title', '?')}~")
    lines.append(f"{len(session.tasks)} remaining · `{session.session_id}`")
    sender.send_to_mechat("\n".join(lines))


def _handle_context(session: Session, task_number: int | None) -> None:
    if task_number is None:
        sender.send_to_mechat('⚠️ Which task? e.g. "context 3"')
        return
    task = session.get_task(task_number)
    if task is None:
        sender.send_to_mechat(f"⚠️ Task {task_number} not found.")
        return

    sender.send_to_mechat(f"📖 _Getting context on task {task_number}…_")
    okf_text = okf_reader.read_bundle(max_chars=30000)
    recent_chats = okf_reader.get_recent_chats_text(days=7, max_chars=15000)

    try:
        ctx = llm.get_context(
            task_title=task.get("title", ""),
            task_summary=task.get("summary", ""),
            source_chat=task.get("source_chat", ""),
            okf_text=okf_text,
            recent_chats=recent_chats,
        )
        sender.send_to_mechat(ctx)
    except Exception as e:
        sender.send_to_mechat(f"❌ Error: {e}")


def _handle_action(session: Session, task_number: int | None) -> None:
    if task_number is None:
        sender.send_to_mechat('⚠️ Which task? e.g. "action 4"')
        return
    task = session.get_task(task_number)
    if task is None:
        sender.send_to_mechat(f"⚠️ Task {task_number} not found.")
        return

    sender.send_to_mechat(f"🎯 _Drafting for task {task_number}…_")

    okf_text = okf_reader.read_bundle(max_chars=20000)
    recent_chats = okf_reader.get_recent_chats_text(days=7, max_chars=10000)
    persona_text = _load_persona()

    try:
        options = llm.get_action_options(
            task_title=task.get("title", ""),
            task_summary=task.get("summary", ""),
            source_chat=task.get("source_chat", ""),
            source_jid=task.get("source_jid", ""),
            task_number=task_number,
            okf_text=okf_text,
            recent_chats=recent_chats,
            persona_text=persona_text,
        )
        # Resolve raw LIDs in the options text to contact names
        options = contact_resolution.resolve_text(options)
        sender.send_to_mechat(options)
    except Exception as e:
        sender.send_to_mechat(f"❌ Error: {e}")


def _handle_send(session: Session, task_number: int | None,
                 option_letter: str | None) -> None:
    if task_number is None or option_letter is None:
        sender.send_to_mechat('⚠️ Format: "send 3 A" (task number + letter)')
        return

    task = session.get_task(task_number)
    if task is None:
        sender.send_to_mechat(f"⚠️ Task {task_number} not found.")
        return

    option_letter = option_letter.upper()
    source_chat = task.get("source_chat", "")
    source_jid = task.get("source_jid", "")

    # If this is a pulse task with pre-generated options, use them directly
    pulse_options = task.get("pulse_options", {})
    if pulse_options:
        option_text = pulse_options.get(option_letter, "")
        if not option_text:
            sender.send_to_mechat(
                f"⚠️ No option {option_letter} for task {task_number}. "
                f"Available: {', '.join(sorted(pulse_options.keys()))}"
            )
            return
        option_text = contact_resolution.resolve_text(option_text)
    else:
        # Standard task — generate options via LLM and extract the requested one
        sender.send_to_mechat(f"📨 _Generating option {option_letter}…_")

        okf_text = okf_reader.read_bundle(max_chars=20000)
        recent_chats = okf_reader.get_recent_chats_text(days=7, max_chars=10000)
        persona_text = _load_persona()

        try:
            options_msg = llm.get_action_options(
                task_title=task.get("title", ""),
                task_summary=task.get("summary", ""),
                source_chat=source_chat,
                source_jid=source_jid,
                task_number=task_number,
                okf_text=okf_text,
                recent_chats=recent_chats,
                persona_text=persona_text,
            )

            option_text = _extract_option(options_msg, option_letter)
            if not option_text:
                sender.send_to_mechat(
                    f"⚠️ Could not find option {option_letter}. "
                    f"Try 'action {task_number}' to see options again."
                )
                return

            option_text = contact_resolution.resolve_text(option_text)
        except Exception as e:
            sender.send_to_mechat(f"❌ Error: {e}")
            return

    # Resolve target JID
    target_jid = source_jid
    target_name = source_chat

    if not target_jid:
        target_jid = _find_chat_jid_exact(source_chat)
        if target_jid:
            target_name = _get_chat_name(target_jid) or source_chat

    if not target_jid:
        sender.send_to_mechat(
            f"⚠️ Could not find exact group for '{source_chat}'.\n"
            f"JID was empty and no exact name match found."
        )
        return

    session.set_pending_send(
        task_number=task_number,
        option_letter=option_letter,
        text=option_text,
        target_jid=target_jid,
        target_name=target_name,
    )

    msg = (
        f"📤 *Confirm send*\n"
        f"━━━━━━━━━━━━━\n"
        f"📍 *To:* {target_name}\n"
        f"🆔 `{target_jid}`\n"
        f"📝 *Task {task_number} · Option {option_letter}*\n"
        f"━━━━━━━━━━━━━\n\n"
        f"{option_text}\n\n"
        f"━━━━━━━━━━━━━\n"
        f"✅ Reply *yes* to send. Anything else cancels."
    )
    sender.send_to_mechat(msg)


def _handle_confirm_send(session: Session) -> None:
    pending = session.get_pending_send()
    if not pending:
        sender.send_to_mechat("⚠️ No pending send to confirm.")
        return

    target_jid = pending["target_jid"]
    target_name = pending["target_name"]
    text = pending["text"]
    task_number = pending["task_number"]
    option_letter = pending["option_letter"]

    sender.send_to_mechat(f"📤 _Sending to {target_name}…_")

    success = sender.send_message(target_jid, text)
    if success:
        sender.send_to_mechat(
            f"✅ *Sent to {target_name}*\n"
            f"━━━━━━━━━━━━━\n\n"
            f"{text}\n\n"
            f"━━━━━━━━━━━━━\n"
            f"🆔 `{session.session_id}`"
        )
    else:
        sender.send_to_mechat(f"❌ Failed to send to {target_name}.")
    session.clear_pending_send()


def _handle_get_more(session: Session) -> None:
    sender.send_to_mechat("🔄 _Getting more tasks…_")

    # Get current active tasks before extending — copy to avoid reference issues
    current_tasks = list(session.tasks)

    # Build title list of current tasks to prevent LLM from generating duplicates
    current_titles = [t.get("title", "") for t in current_tasks]

    new_tasks = task_extractor.get_more_tasks(
        archived_titles=session.archived_titles,
        current_count=session.offset,
        current_titles=current_titles,
    )
    if not new_tasks:
        sender.send_to_mechat("📭 No more tasks found.")
        return

    session.add_tasks(new_tasks)

    # Show all tasks as a single merged list (compact one-line format)
    all_tasks = list(session.tasks)
    parts = [
        f"📋 *Tasks* ({len(all_tasks)})",
        task_extractor.format_task_lines(all_tasks),
        "",
        "━" * 12,
        "• archive 1,3 • context 2 • action 4 • send 4A • more",
        f"🆔 `{session.session_id}`",
    ]

    sender.send_to_mechat("\n".join(parts))


def _handle_unknown(session: Session, text: str) -> None:
    timeout_min = config.SESSION_TIMEOUT_SECONDS // 60
    sender.send_to_mechat(
        f"🤔 _Didn't catch that._\n\n"
        f"━━━━━━━━━━━━━\n"
        f"• archive 1,3 • context 2 • action 4\n"
        f"• send 5A (A–G) • more\n"
        f"• /pull — generate now\n"
        f"• /hourlyb — full hourly bulletin\n"
        f"━━━━━━━━━━━━━\n"
        f"🆔 `{session.session_id}` · ⏱️ {timeout_min}min\n"
        f"_hourlyB · hourly bulletins · free text · self learning_"
    )


def _extract_option(options_msg: str, letter: str) -> str | None:
    letter = letter.upper()
    # Match the option body up to the next option (A-G), a category header, or a divider line (━).
    pattern = rf"\*?{letter}\.?\*\s*(.+?)(?=\n\*?[A-G]\.?\*|\n\*[A-Z][A-Z -]+\*|\n━{{3,}}|\Z)"
    match = re.search(pattern, options_msg, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def _find_chat_jid_exact(chat_name: str) -> str | None:
    try:
        for chat in db.get_non_archived_chats(min_messages=0):
            if chat["name"].lower() == chat_name.lower():
                return chat["jid"]
    except Exception:
        pass
    return None


def _get_chat_name(jid: str) -> str | None:
    try:
        for chat in db.get_non_archived_chats(min_messages=0):
            if chat["jid"] == jid:
                return chat["name"]
    except Exception:
        pass
    return None


def _load_persona() -> str:
    try:
        if config.PERSONA_FILE.exists():
            return config.PERSONA_FILE.read_text(encoding="utf-8")
    except Exception:
        pass
    return ""