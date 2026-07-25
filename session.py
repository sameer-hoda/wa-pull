"""
Session management — tracks the active task list, unique session ID,
and 5-minute inactivity timeout.

A session is created when /pull is run.  It holds:
  - session_id: unique, traceable ID (persisted via session_log)
  - task_list: the current list of tasks (dicts)
  - archived_titles: titles of archived tasks (in-session + persistent)
  - pending_send: confirmation state for "send" actions
  - created_at: when the session started
  - last_activity: last time the user interacted
  - offset: current page offset for "get more"

A session expires after SESSION_TIMEOUT_SECONDS of inactivity.
"""

import time
import uuid
import threading
from typing import Optional

import config
import session_log


class Session:
    """A single user session with a task list."""

    def __init__(self, tasks: list[dict]):
        self.session_id: str = uuid.uuid4().hex[:12].upper()
        self.tasks: list[dict] = tasks
        self.archived_titles: list[str] = []
        self.pending_send: Optional[dict] = None  # {task_number, option_letter, text, target_jid, target_name}
        self.created_at: float = time.time()
        self.last_activity: float = time.time()
        self.offset: int = len(tasks)
        # Persist for traceability
        try:
            session_log.record(self.session_id, len(tasks))
        except Exception:
            pass

    def is_expired(self) -> bool:
        """Check if the session has expired due to inactivity."""
        elapsed = time.time() - self.last_activity
        return elapsed > config.SESSION_TIMEOUT_SECONDS

    def touch(self):
        """Update last_activity to now."""
        self.last_activity = time.time()

    def get_task(self, task_number: int) -> Optional[dict]:
        """Get a task by its number. Returns None if not found."""
        for task in self.tasks:
            if task.get("task_number") == task_number:
                return task
        return None

    def archive_tasks(self, task_numbers: list[int]) -> list[dict]:
        """
        Archive tasks by their numbers. Removes them from the active list,
        adds their titles to archived_titles, and persists to archive_store.
        Returns the archived task dicts (with title + source_chat).
        """
        archived = []
        remaining = []
        for task in self.tasks:
            if task.get("task_number") in task_numbers:
                title = task.get("title", f"Task {task.get('task_number')}")
                source_chat = task.get("source_chat", "")
                source_jid = task.get("source_jid", "")
                archived.append(task)
                self.archived_titles.append(title)
                # Persist so it stays archived forever
                try:
                    import archive_store
                    archive_store.archive_task(title, source_chat, self.session_id)
                except Exception:
                    pass
            else:
                remaining.append(task)
        self.tasks = remaining
        self.touch()
        return archived

    def add_tasks(self, new_tasks: list[dict]) -> None:
        """Add more tasks to the session (for 'get more')."""
        self.tasks.extend(new_tasks)
        self.offset = len(self.tasks)
        self.touch()

    def get_active_task_numbers(self) -> list[int]:
        """Return the task numbers still in the active list."""
        return [t.get("task_number") for t in self.tasks]

    def set_pending_send(self, task_number: int, option_letter: str,
                         text: str, target_jid: str, target_name: str) -> None:
        """Store a pending send awaiting user confirmation."""
        self.pending_send = {
            "task_number": task_number,
            "option_letter": option_letter.upper(),
            "text": text,
            "target_jid": target_jid,
            "target_name": target_name,
        }
        self.touch()

    def clear_pending_send(self) -> None:
        self.pending_send = None
        self.touch()

    def get_pending_send(self) -> Optional[dict]:
        return self.pending_send


class SessionManager:
    """Manages the single active session (one user bot)."""

    def __init__(self):
        self._session: Optional[Session] = None
        self._lock = threading.Lock()

    def get_session(self) -> Optional[Session]:
        """Get the current session, or None if expired/none.
        Notifies via sender when session expires."""
        with self._lock:
            if self._session is None:
                return None
            if self._session.is_expired():
                self._session = None
                # Notify user that session ended
                try:
                    from sender import send_to_mechat
                    send_to_mechat(
                        f"⏰ *Session ended* ({config.SESSION_TIMEOUT_SECONDS}s inactivity).\n\n"
                        "A new hourly bulletin starts at the top of the next hour "
                        f"({config.IST_START_HOUR:02d}:00–23:00 IST).\n"
                        "Type */pull* to generate one on demand.\n"
                        "_hourlyB · hourly bulletins · free text · self learning_"
                    )
                except Exception:
                    pass
                return None
            return self._session

    def create_session(self, tasks: list[dict]) -> Session:
        """Create a new session with the given task list."""
        with self._lock:
            self._session = Session(tasks)
            return self._session

    def end_session(self):
        """End the current session."""
        with self._lock:
            self._session = None

    def has_session(self) -> bool:
        """Check if there's an active, non-expired session."""
        return self.get_session() is not None


# Singleton
session_manager = SessionManager()
