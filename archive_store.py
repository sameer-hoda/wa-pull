"""
Archive Store — persistent list of archived tasks.

Once a task is archived, it stays archived across sessions, restarts and
/pull regenerations.  We persist by (title, source_chat) pairs so the LLM
task extractor can be told to exclude them forever.

Storage: JSON file at config.ARCHIVE_FILE.
  {
    "<title>::<source_chat>": {
        "title": "...",
        "source_chat": "...",
        "archived_at": <iso timestamp>,
        "session_id": "<session that archived it>"
    },
    ...
  }
"""

import json
import datetime
from typing import Optional

import config


def _load() -> dict:
    try:
        if config.ARCHIVE_FILE.exists():
            return json.loads(config.ARCHIVE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[archive] load error: {e}")
    return {}


def _save(data: dict) -> None:
    config.STORE_DIR.mkdir(parents=True, exist_ok=True)
    config.ARCHIVE_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _key(title: str, source_chat: str) -> str:
    return f"{title}::{source_chat or ''}"


def archive_task(title: str, source_chat: str, session_id: str) -> None:
    """Persist a single archived task."""
    data = _load()
    data[_key(title, source_chat)] = {
        "title": title,
        "source_chat": source_chat or "",
        "archived_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": session_id,
    }
    _save(data)


def archive_many(items: list[dict], session_id: str) -> None:
    """items: [{"title": ..., "source_chat": ...}, ...]"""
    for it in items:
        archive_task(it.get("title", ""), it.get("source_chat", ""), session_id)


def is_archived(title: str, source_chat: str) -> bool:
    return _key(title, source_chat) in _load()


def get_archived_titles(source_chat: Optional[str] = None) -> list[str]:
    """Return archived titles, optionally filtered by source chat."""
    data = _load()
    if source_chat is None:
        return [v["title"] for v in data.values()]
    return [v["title"] for v in data.values() if v["source_chat"] == source_chat]


def get_archived_items() -> list[dict]:
    """Return full archived items (title + source_chat)."""
    return list(_load().values())


def clear() -> int:
    """Wipe the archive. Returns count removed."""
    n = len(_load())
    _save({})
    return n


def count() -> int:
    return len(_load())
