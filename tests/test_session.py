"""
Tests for the session module.
Run: python3 -m pytest tests/test_session.py -v
"""

import sys
import os
import time
from pathlib import Path

# Add parent dir to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from session import Session, SessionManager


def test_session_creation():
    """A session can be created with a task list and has a session_id."""
    tasks = [
        {"task_number": 1, "title": "Task A", "summary": "Do A"},
        {"task_number": 2, "title": "Task B", "summary": "Do B"},
    ]
    session = Session(tasks)
    assert len(session.tasks) == 2
    assert session.offset == 2
    assert not session.is_expired()
    assert len(session.session_id) == 12  # 12-char hex upper


def test_session_get_task():
    """Can retrieve a task by number."""
    tasks = [
        {"task_number": 1, "title": "Task A"},
        {"task_number": 2, "title": "Task B"},
    ]
    session = Session(tasks)
    assert session.get_task(1)["title"] == "Task A"
    assert session.get_task(2)["title"] == "Task B"
    assert session.get_task(99) is None


def test_session_archive():
    """Archiving removes tasks and tracks titles."""
    tasks = [
        {"task_number": 1, "title": "Task A", "source_chat": "G1"},
        {"task_number": 2, "title": "Task B", "source_chat": "G2"},
        {"task_number": 3, "title": "Task C", "source_chat": "G3"},
    ]
    session = Session(tasks)
    archived = session.archive_tasks([1, 3])
    assert len(archived) == 2
    titles = [t.get("title") for t in archived]
    assert "Task A" in titles
    assert "Task C" in titles
    assert len(session.tasks) == 1
    assert session.tasks[0]["title"] == "Task B"
    assert len(session.archived_titles) == 2


def test_session_add_tasks():
    """Can add more tasks (for 'get more')."""
    tasks = [{"task_number": 1, "title": "Task A"}]
    session = Session(tasks)
    new_tasks = [{"task_number": 11, "title": "Task K"}]
    session.add_tasks(new_tasks)
    assert len(session.tasks) == 2
    assert session.offset == 2


def test_session_expiry():
    """Session expires after timeout."""
    # Temporarily set a very short timeout
    original = config.SESSION_TIMEOUT_SECONDS
    config.SESSION_TIMEOUT_SECONDS = 0.1

    session = Session([{"task_number": 1, "title": "A"}])
    assert not session.is_expired()

    time.sleep(0.15)
    assert session.is_expired()

    config.SESSION_TIMEOUT_SECONDS = original


def test_session_manager():
    """SessionManager create/get/end."""
    original = config.SESSION_TIMEOUT_SECONDS
    config.SESSION_TIMEOUT_SECONDS = 10

    mgr = SessionManager()
    assert mgr.get_session() is None
    assert not mgr.has_session()

    session = mgr.create_session([{"task_number": 1, "title": "A"}])
    assert mgr.has_session()
    assert mgr.get_session() is session

    mgr.end_session()
    assert not mgr.has_session()

    config.SESSION_TIMEOUT_SECONDS = original


def test_session_touch():
    """Touch resets the expiry timer."""
    original = config.SESSION_TIMEOUT_SECONDS
    config.SESSION_TIMEOUT_SECONDS = 0.2

    session = Session([{"task_number": 1, "title": "A"}])
    time.sleep(0.15)
    session.touch()
    assert not session.is_expired()

    config.SESSION_TIMEOUT_SECONDS = original


def test_session_pending_send():
    """Pending send confirmation state works."""
    session = Session([{"task_number": 1, "title": "A"}])
    assert session.get_pending_send() is None

    session.set_pending_send(1, "A", "hello world",
                              "group@g.us", "Test Group")
    pending = session.get_pending_send()
    assert pending is not None
    assert pending["text"] == "hello world"
    assert pending["target_jid"] == "group@g.us"

    session.clear_pending_send()
    assert session.get_pending_send() is None


if __name__ == "__main__":
    test_session_creation()
    test_session_get_task()
    test_session_archive()
    test_session_add_tasks()
    test_session_expiry()
    test_session_manager()
    test_session_touch()
    test_session_pending_send()
    print("✅ All session tests passed!")
