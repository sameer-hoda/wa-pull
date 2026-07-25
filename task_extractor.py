"""
Task Extractor — extract and score top-N tasks from the OKF + recent chats.

Wraps the LLM task extraction.  Exposes context-loading separately so
handlers can show progress messages between steps.
"""

import config
import llm
import okf_reader
import db
import archive_store


def load_context() -> tuple[str, str, dict]:
    """
    Load the OKF + recent chats + chat name→JID map.
    Fast — returns pre-extraction so handlers can show progress.
    Returns (okf_text, recent_chats, chat_name_to_jid).
    """
    okf_text = okf_reader.read_bundle(max_chars=200000)
    recent_chats = okf_reader.get_recent_chats_text(days=7, max_chars=300000)

    chat_name_to_jid = {}
    try:
        for chat in db.get_non_archived_chats(min_messages=0):
            chat_name_to_jid[chat["name"]] = chat["jid"]
    except Exception:
        pass

    return okf_text, recent_chats, chat_name_to_jid


def extract_tasks_from_context(okf_text: str, recent_chats: str,
                                chat_name_to_jid: dict,
                                offset: int = 0,
                                archived_titles: list[str] = None,
                                current_titles: list[str] = None) -> list[dict]:
    """
    Extract tasks from already-loaded context. This is the slow (LLM) part.
    """
    # Merge in-session + persistent archived titles
    all_archived = list(archived_titles or [])
    all_archived.extend(archive_store.get_archived_titles())
    all_archived = list(dict.fromkeys(all_archived))

    # Build previous_titles: combine last bulletin tasks + current page tasks to avoid dupes
    previous_lines = []

    # Current page tasks (for "get more" — tells LLM to not repeat 1-10)
    if current_titles:
        previous_lines.extend(current_titles)

    # Last bulletin tasks (for hour-over-hour freshness)
    try:
        prev = llm._last_bulletin_tasks
        if prev:
            for t in prev:
                prev_title = t.get("title", "")
                if prev_title not in previous_lines:
                    previous_lines.append(prev_title)
    except Exception:
        pass

    previous_titles = "\n".join(f"- {t}" for t in previous_lines) if previous_lines else "(unavailable)"

    tasks = llm.extract_tasks(
        okf_index=okf_text,
        recent_chats=recent_chats,
        offset=offset,
        archived_titles=all_archived,
        chat_name_to_jid=chat_name_to_jid,
        previous_titles=previous_titles,
    )

    # Post-extraction safety filter: remove tasks that match archived titles.
    # The LLM prompt tells the LLM to skip these, but this is belt-and-braces.
    # We ONLY filter against archived titles — NOT current_titles, because the
    # LLM already receives previous_titles and is told not to repeat. Filtering
    # current_titles too aggressively kills legitimate new tasks that touch the
    # same topic from a different angle.
    block_titles = set(all_archived or [])

    def _is_duplicate(title: str) -> bool:
        if not title:
            return False
        title_lower = title.lower().strip()
        for bt in block_titles:
            bt_lower = bt.lower().strip()
            if not bt_lower:
                continue
            # Exact match only — substring/word-overlap was too aggressive
            # and killed legitimate tasks that shared words with archived ones.
            if title_lower == bt_lower:
                return True
        return False

    filtered = [t for t in tasks if not _is_duplicate(t.get("title", ""))]
    tasks = filtered

    for i, task in enumerate(tasks):
        task["task_number"] = offset + i + 1

    return tasks


def get_initial_tasks() -> list[dict]:
    """Get the first page of tasks (backward-compat, one-shot call)."""
    okf, chats, jid_map = load_context()
    return extract_tasks_from_context(okf, chats, jid_map)


def get_more_tasks(archived_titles: list[str], current_count: int,
                   current_titles: list[str] = None) -> list[dict]:
    okf, chats, jid_map = load_context()
    return extract_tasks_from_context(
        okf, chats, jid_map,
        offset=current_count,
        archived_titles=archived_titles,
        current_titles=current_titles,
    )


def format_task_lines(tasks: list[dict]) -> str:
    """Render per-task lines — compact one-line format."""
    emoji_map = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
    lines = []
    for task in tasks:
        num = task.get("task_number", "?")
        title = task.get("title", "Unknown")
        urgency = task.get("urgency", "medium")
        who = task.get("who_waiting", "")
        age = f"{task['waiting_hours']}h" if task.get("waiting_hours") else ""
        deadline = task.get("deadline", "")
        is_new = task.get("is_new", False)
        state = task.get("state", "")

        emo = emoji_map.get(urgency, "🟡")
        new_flag = " 🆕" if is_new else ""
        warn = " ⚠️" if state in ("waiting_on_me", "needs_decision") else ""

        meta_parts = [x for x in [who, age, deadline] if x]
        meta = " · ".join(meta_parts)

        line = f"{emo} *{num}* {title}{new_flag}"
        if meta:
            line += f" · {meta}"
        line += warn
        lines.append(line)
    return "\n".join(lines)


def format_task_list(tasks: list[dict], session_id: str = "") -> str:
    """Compact one-line task list."""
    if not tasks:
        return "📭 No pending tasks. You're all caught up!"

    body = format_task_lines(tasks)
    header = f"📋 *Tasks* ({len(tasks)})"
    timeout_min = config.SESSION_TIMEOUT_SECONDS // 60

    sid_line = f"\n🆔 `{session_id}` · ⏱️ {timeout_min}min" if session_id else ""

    footer = (
        "\n\n" + "━" * 12
        + f"\n• archive 1,3 • context 2 • action 4 • send 4A • more"
        + sid_line
        + "\n_hourlyB · hourly bulletins · free text · self learning_"
    )

    return header + "\n\n" + body + footer