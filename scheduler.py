"""
Scheduler — drives the hourly session + end-of-hour OKF wrap-up.

A session starts automatically at the top of every hour, 8 AM–midnight IST.
Each hourly session sends an "Hourly Bulletin" containing:
  - session id + time in IST
  - top 20 tasks to check (compact one-line format, v5)

10 minutes before the next hour's session (at :50), the OKF is updated for
chats that were active this hour and an "hour wrap-up" message is sent with
the key new context absorbed (≤ 60 words). This doubles as "what happened this
hour and how it is saved in memory".

At the first session of the day (8 AM) the OKF bundle + persona are fully
rebuilt before the bulletin is generated.
"""

import time
import datetime
import threading

import config
import okf_builder
import task_extractor
import sender
import db
import llm
import okf_reader
from session import session_manager


def _now_ist() -> datetime.datetime:
    return datetime.datetime.now(config.IST)


def _format_ist(dt: datetime.datetime) -> str:
    return dt.strftime("%H:%M IST")


def _format_date(dt: datetime.datetime) -> str:
    return dt.strftime("%B %d")


def _format_activity_text(messages: list[dict]) -> str:
    """Render the recent-activity log for the LLM, marking the user's messages."""
    lines = []
    for m in messages:
        time_str = m["time"].astimezone(config.IST).strftime("%Y-%m-%d %H:%M")
        chat = m.get("chat_name", "")
        if m.get("is_from_me"):
            lines.append(f"[{time_str}] ({chat}) [ME] {m['content'][:400]}")
        else:
            sender_name = m.get("sender", "?")
            lines.append(f"[{time_str}] ({chat}) {sender_name}: {m['content'][:400]}")
    return "\n".join(lines)


def _format_task_lines(tasks: list[dict]) -> str:
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


def _build_bulletin(session_id: str, tasks: list[dict],
                    now: datetime.datetime) -> str:
    parts = [
        f"🕒 *Hourly Bulletin* · {_format_ist(now)} · {_format_date(now)}",
        f"🆔 Session `{session_id}`",
        "",
        f"📋 *Tasks* ({len(tasks)})",
        _format_task_lines(tasks),
        "",
        "━" * 12,
        "• archive 1,3 • context 2 • action 4 • send 4A • more",
        "_hourlyB · hourly bulletins · free text · self learning_",
    ]
    return "\n".join(parts)


class HourlyScheduler:
    """Runs the hourly session (top of the hour) and wrap-up (:50)."""

    def __init__(self):
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_session_key: str | None = None
        self._last_wrapup_key: str | None = None

    def start(self):
        """Start the scheduler in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[scheduler] Started — hourly sessions "
              f"{config.IST_START_HOUR:02d}:00–{config.IST_END_HOUR:02d}:00 IST, "
              f"wrap-up at :{config.WRAPUP_MINUTE:02d}")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self):
        while self._running:
            now = _now_ist()
            if config.IST_START_HOUR <= now.hour <= config.IST_END_HOUR:
                date_hour_key = now.strftime("%Y-%m-%d %H")

                # Top of the hour → start a new hourly session + bulletin
                if now.minute == 0 and date_hour_key != self._last_session_key:
                    print(f"[scheduler] Hourly session at {now.isoformat()}")
                    self._last_session_key = date_hour_key
                    try:
                        self._run_hourly_session(now)
                    except Exception as e:
                        print(f"[scheduler] Hourly session failed: {e}")

                # 10 min before next hour → OKF update + wrap-up message
                elif (now.minute == config.WRAPUP_MINUTE
                      and date_hour_key != self._last_wrapup_key):
                    print(f"[scheduler] Hour wrap-up at {now.isoformat()}")
                    self._last_wrapup_key = date_hour_key
                    try:
                        self._run_wrapup(now)
                    except Exception as e:
                        print(f"[scheduler] Wrap-up failed: {e}")

            time.sleep(20)  # check every 20s

    # ── Hourly session + bulletin ────────────────────────────────────────────
    def _run_hourly_session(self, now: datetime.datetime):
        """Generate and send the hourly bulletin, and create the session."""
        # First session of the day → full OKF + persona rebuild
        if now.hour == config.FULL_REBUILD_HOUR:
            def progress(chat_name, idx, total):
                print(f"[scheduler] OKF: {idx}/{total} — {chat_name}")

            sender.send_to_mechat(
                "🌅 _Good morning! Rebuilding your knowledge base for the day…_"
            )
            try:
                okf_builder.build_okf_bundle(progress_callback=progress)
            except Exception as e:
                print(f"[scheduler] OKF rebuild failed: {e}")

            sender.send_to_mechat("🎭 _Learning your communication style…_")
            try:
                okf_builder.build_persona(progress_callback=progress)
            except Exception as e:
                print(f"[scheduler] Persona build failed: {e}")

        # 1. Load context + extract top tasks
        sender.send_to_mechat("🔄 _Building your hourly bulletin…_")
        okf_text, recent_chats, jid_map = task_extractor.load_context()
        tasks = task_extractor.extract_tasks_from_context(
            okf_text, recent_chats, jid_map
        )
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

    # ── End-of-hour wrap-up: update OKF + new-context message ────────────────
    def _run_wrapup(self, now: datetime.datetime):
        """Update the OKF for chats active this hour and send the wrap-up."""
        active_chats = db.get_chats_active_since(hours=1.0, min_messages=1)

        if not active_chats:
            sender.send_to_mechat(
                f"🧠 *Hour wrap-up* · {_format_ist(now)}\n\n"
                "No new context absorbed this hour.\n"
                "━" * 12 + "\n"
                "✅ OKF memory unchanged\n"
                "_hourlyB · hourly bulletins · free text · self learning_"
            )
            return

        def progress(chat_name, idx, total):
            print(f"[scheduler] OKF incr: {idx}/{total} — {chat_name}")

        diffs = okf_builder.incremental_update(active_chats, progress_callback=progress)

        try:
            summary = llm.summarize_hourly_context(diffs)
        except Exception as e:
            print(f"[scheduler] context summary failed: {e}")
            summary = "No new context absorbed this hour."

        msg = (
            f"🧠 *Hour wrap-up* · {_format_ist(now)}\n\n"
            f"{summary}\n"
            + ("━" * 12) + "\n"
            "✅ Saved to OKF memory\n"
            "_hourlyB · hourly bulletins · free text · self learning_"
        )
        sender.send_to_mechat(msg)


scheduler = HourlyScheduler()
