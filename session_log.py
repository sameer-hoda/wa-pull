"""
Session Log — persistent log of every session ID ever created.

A session ID is unique and traceable later.  We log:
  {session_id, created_at, task_count}

Storage: JSON file at config.SESSION_LOG_FILE (list, newest first, capped).
"""

import json
import datetime
from typing import Optional

import config

_MAX_LOG_ENTRIES = 500


def _load() -> list[dict]:
    try:
        if config.SESSION_LOG_FILE.exists():
            return json.loads(config.SESSION_LOG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[session_log] load error: {e}")
    return []


def _save(entries: list[dict]) -> None:
    config.STORE_DIR.mkdir(parents=True, exist_ok=True)
    config.SESSION_LOG_FILE.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def record(session_id: str, task_count: int) -> None:
    """Append a session entry to the log (newest first)."""
    entries = _load()
    entries.insert(0, {
        "session_id": session_id,
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "task_count": task_count,
    })
    _save(entries[:_MAX_LOG_ENTRIES])


def recent(limit: int = 10) -> list[dict]:
    return _load()[:limit]


def get(session_id: str) -> Optional[dict]:
    for e in _load():
        if e["session_id"] == session_id:
            return e
    return None


def count() -> int:
    return len(_load())
