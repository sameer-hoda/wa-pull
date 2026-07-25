"""
Pulse Monitor — watches all non-archived chats for triggers and generates
proactive "pulse" alerts in MeChat.

Triggers:
  1. user_sent      — user sends a message in any non-archived chat
  2. user_tagged    — someone tags the user (@<own_lid>) in any chat
  3. user_asked     — someone asks the user a question (contains "?" and is
                      directed at the user — in a DM, or mentions the user's name)
  4. quick_succession — 4+ messages from others in the same chat within one poll

For each trigger (subject to a per-chat cooldown), the monitor:
  - Loads that chat's recent context + its OKF concept doc (+ persona)
  - Calls generate_pulse() to classify and generate context + task + A/B/C options
  - If actionable, adds the task to the session (creating one if needed) and
    sends a "pulse @ hh:mm" alert to MeChat with the task number and options

Pulse tasks use the SAME task numbering as the hourly bulletin — they are
appended to the session's task list. The user can:
  send <task_number> <A|B|C>   → send that option to the source chat
  context <task_number>        → get richer context (uses existing command)
  archive <task_number>        → dismiss (uses existing command)

The monitor runs in its own daemon thread, polling every MONITOR_POLL_INTERVAL
seconds (default 10s). It is independent of the MeChat command loop in main.py.
"""

import json
import time
import datetime
import threading
from typing import Optional

import config
import db
import llm
import sender
import okf_builder
import contact_resolution


class PulseMonitor:
    """Watches all non-archived chats and fires pulse alerts to MeChat."""

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_seen: datetime.datetime = datetime.datetime.now(
            datetime.timezone.utc
        )
        self._cooldowns: dict[str, float] = {}   # chat_jid → last alert epoch
        self._lock = threading.Lock()
        self._own_lid_num: str = ""
        self._mechat_jid: str = ""

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self):
        if not config.MONITOR_ENABLED:
            print("[monitor] Disabled via MONITOR_ENABLED=0")
            return
        try:
            self._mechat_jid = db.get_mechat_chat_jid()
            self._own_lid_num = self._mechat_jid.split("@")[0]
        except Exception as e:
            print(f"[monitor] Could not resolve own LID: {e}")
            self._own_lid_num = ""

        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(
            f"[monitor] Started — polling every {config.MONITOR_POLL_INTERVAL}s, "
            f"cooldown {config.MONITOR_COOLDOWN_SECONDS}s/chat"
        )

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ── main loop ────────────────────────────────────────────────────────────
    def _loop(self):
        while self._running:
            try:
                self._scan()
            except Exception as e:
                print(f"[monitor] scan error: {e}")
            time.sleep(config.MONITOR_POLL_INTERVAL)

    def _scan(self):
        """Fetch new messages, group by chat, detect triggers."""
        try:
            new_messages = db.get_new_messages_across_chats(
                since=self._last_seen,
                exclude_jid=self._mechat_jid,
                limit=500,
            )
        except Exception as e:
            print(f"[monitor] db fetch error: {e}")
            return

        if not new_messages:
            return

        self._last_seen = new_messages[-1]["time"]

        # Group messages by chat
        by_chat: dict[str, list[dict]] = {}
        for msg in new_messages:
            content = msg.get("content", "")
            if not content or not content.strip():
                continue
            try:
                if llm.is_bot_message(content):
                    continue
            except Exception:
                pass
            by_chat.setdefault(msg["chat_jid"], []).append(msg)

        for chat_jid, msgs in by_chat.items():
            self._check_chat(chat_jid, msgs)

    def _check_chat(self, chat_jid: str, msgs: list[dict]):
        """Detect triggers for one chat and fire if warranted."""
        trigger, trigger_msgs = self._detect_trigger(chat_jid, msgs)
        if trigger is None:
            return
        self._maybe_trigger(chat_jid, trigger, trigger_msgs)

    def _detect_trigger(self, chat_jid: str,
                        msgs: list[dict]) -> tuple[Optional[str], list[dict]]:
        """
        Detect which trigger (if any) fires for this chat's new messages.
        Returns (trigger_name, trigger_msgs) or (None, []).
        """
        user_msgs = [m for m in msgs if m["is_from_me"]]
        other_msgs = [m for m in msgs if not m["is_from_me"]]

        # Priority: user_tagged > user_asked > quick_succession > user_sent
        if self._own_lid_num:
            for m in other_msgs:
                if f"@{self._own_lid_num}" in m["content"]:
                    return "user_tagged", [m]

        # user_asked: someone sent a message with "?" that seems directed
        # at the user (DM, or mentions user's name/tag)
        for m in other_msgs:
            content = m["content"]
            if "?" in content:
                is_dm = chat_jid.endswith("@s.whatsapp.net") or chat_jid.endswith("@lid")
                if is_dm:
                    return "user_asked", [m]
                if f"@{self._own_lid_num}" in content:
                    return "user_asked", [m]

        # quick_succession: 4+ messages from others in this poll (groups only)
        if len(other_msgs) >= 4 and chat_jid.endswith("@g.us"):
            return "quick_succession", other_msgs

        # user_sent: user sent a message
        if user_msgs:
            return "user_sent", user_msgs

        return None, []

    # ── trigger → context → LLM → pulse ───────────────────────────────────────
    def _maybe_trigger(self, chat_jid: str, trigger: str, trigger_msgs: list[dict]):
        chat_name = trigger_msgs[0].get("chat_name", chat_jid)

        # Per-chat cooldown
        now = time.time()
        last = self._cooldowns.get(chat_jid, 0)
        if now - last < config.MONITOR_COOLDOWN_SECONDS:
            return

        # Load recent chat context
        try:
            messages = db.get_chat_messages(
                chat_jid,
                days=config.OKF_LOOKBACK_DAYS,
                limit=config.MONITOR_CONTEXT_MESSAGES,
            )
        except Exception as e:
            print(f"[monitor] context load failed for {chat_name}: {e}")
            return
        if not messages:
            return

        ctx_lines = []
        for m in messages[-config.MONITOR_CONTEXT_MESSAGES:]:
            t = m["time"].astimezone(config.IST).strftime("%H:%M")
            who = "ME" if m["is_from_me"] else m["sender"]
            ctx_lines.append(f"[{t}] {who}: {m['content'][:300]}")
        chat_context = "\n".join(ctx_lines)

        # OKF concept for this chat
        okf_concept = ""
        try:
            okf_concept = okf_builder.read_concept_md(chat_name, chat_jid)
        except Exception:
            pass

        # Persona
        persona_text = ""
        try:
            if config.PERSONA_FILE.exists():
                persona_text = config.PERSONA_FILE.read_text(encoding="utf-8")
        except Exception:
            pass

        # LLM generates the pulse
        try:
            result = llm.generate_pulse(
                trigger=trigger,
                trigger_msgs=trigger_msgs,
                chat_name=chat_name,
                chat_jid=chat_jid,
                chat_context=chat_context,
                okf_concept=okf_concept,
                persona_text=persona_text,
            )
        except Exception as e:
            print(f"[monitor] LLM failed for {chat_name}: {e}")
            return

        if result.get("action_type", "none") == "none":
            return

        # Set cooldown — an alert will fire
        self._cooldowns[chat_jid] = now

        # Build the task and add it to the session
        task = self._build_task(result, chat_name, chat_jid)
        task_number = self._add_task_to_session(task)

        # Send the pulse message
        self._send_pulse(task_number, task, result, chat_name)

    def _build_task(self, result: dict, chat_name: str, chat_jid: str) -> dict:
        """Build a task dict from the LLM result."""
        t = result.get("task", {})
        return {
            "task_number": 0,  # will be set by _add_task_to_session
            "title": t.get("title", ""),
            "summary": t.get("summary", ""),
            "who_waiting": t.get("who_waiting", ""),
            "waiting_hours": t.get("waiting_hours", 0),
            "deadline": t.get("deadline", ""),
            "source_chat": chat_name,
            "source_jid": chat_jid,
            "urgency": t.get("urgency", "medium"),
            "state": t.get("state", "waiting_on_me"),
            "is_new": True,
            "score": t.get("score", 0),
            "score_reason": t.get("score_reason", ""),
            "pulse_options": result.get("options", {}),
            "pulse_context": result.get("context", ""),
        }

    def _add_task_to_session(self, task: dict) -> int:
        """Add a pulse task to the active session (creating one if needed).
        Returns the task number assigned."""
        from session import session_manager, Session

        session = session_manager.get_session()
        if session is None:
            session = session_manager.create_session([task])
            task["task_number"] = 1
        else:
            next_num = session.offset + 1
            task["task_number"] = next_num
            session.add_tasks([task])
        return task["task_number"]

    # ── pulse delivery ────────────────────────────────────────────────────────
    def _send_pulse(self, task_number: int, task: dict, result: dict,
                    chat_name: str):
        """Send the pulse alert to MeChat."""
        now = datetime.datetime.now(config.IST)
        time_str = now.strftime("%H:%M")

        # Resolve chat name — for @lid chats, try to get a human name
        display_name = chat_name
        try:
            resolved = contact_resolution.resolve_contact(chat_name)
            if resolved and resolved != chat_name:
                display_name = resolved
        except Exception:
            pass

        context = result.get("context", "")
        options = result.get("options", {})

        emoji_map = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        urgency_emoji = emoji_map.get(task.get("urgency", "medium"), "🟡")

        lines = [
            f"pulse @ {time_str}",
            f"📍 {display_name}",
            "",
            context,
            "",
            f"📋 *Task created:*",
            f"{urgency_emoji} *{task_number}* {task.get('title', '?')}",
        ]

        meta_parts = [x for x in [
            task.get("who_waiting", ""),
            f"{task['waiting_hours']}h" if task.get("waiting_hours") else "",
            task.get("deadline", ""),
        ] if x]
        if meta_parts:
            lines.append(f"   {' · '.join(meta_parts)}")

        if options:
            lines += [
                "",
                f"quick response to {display_name}:",
            ]
            for letter in ("A", "B", "C"):
                text = options.get(letter, "")
                if text:
                    lines.append(f"*{letter}.* {text}")

            lines += [
                "",
                f"send {task_number} A/B/C",
            ]

        try:
            sender.send_to_mechat("\n".join(lines))
            print(
                f"[monitor] Pulse task #{task_number} for {display_name}: "
                f"{task.get('title', '?')[:60]}"
            )
        except Exception as e:
            print(f"[monitor] send_pulse failed: {e}")


# Singleton
monitor = PulseMonitor()
