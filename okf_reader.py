"""
OKF Reader — read and search the OKF bundle.

Provides:
  - read_bundle()         → full text dump of all concept docs
  - read_concept()        → single concept by chat name
  - search_bundle()       → find concepts mentioning a keyword
  - get_index()           → the index.md content
"""

import re
from pathlib import Path
from typing import Optional

import config


def get_index() -> str:
    """Return the content of index.md."""
    path = config.OKF_DIR / "index.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def read_bundle(max_chars: int = 50000) -> str:
    """
    Return a concatenated text dump of all concept documents.
    Truncated to max_chars for LLM context.
    """
    bundle = Path(config.OKF_DIR)
    if not bundle.exists():
        return ""

    parts = []
    total = 0
    for md_file in sorted(bundle.rglob("*.md")):
        if md_file.name in ("index.md", "log.md"):
            continue
        content = md_file.read_text(encoding="utf-8")
        parts.append(content)
        total += len(content)
        if total >= max_chars:
            break

    return "\n\n---\n\n".join(parts)[:max_chars]


def read_concept(chat_name: str) -> Optional[str]:
    """Read a single concept document by fuzzy-matching the chat name."""
    bundle = Path(config.OKF_DIR)
    if not bundle.exists():
        return None

    target = _safe_filename(chat_name).lower()
    for md_file in bundle.rglob("*.md"):
        if md_file.name in ("index.md", "log.md"):
            continue
        if target in md_file.stem.lower():
            return md_file.read_text(encoding="utf-8")
    return None


def search_bundle(query: str, max_results: int = 5) -> list[str]:
    """
    Search the OKF bundle for concepts mentioning *query*.
    Returns list of matching concept document texts.
    """
    bundle = Path(config.OKF_DIR)
    if not bundle.exists():
        return []

    query_lower = query.lower()
    results = []
    for md_file in sorted(bundle.rglob("*.md")):
        if md_file.name in ("index.md", "log.md"):
            continue
        content = md_file.read_text(encoding="utf-8")
        if query_lower in content.lower():
            results.append(content)
            if len(results) >= max_results:
                break

    return results


def _safe_filename(name: str) -> str:
    safe = re.sub(r"[^\w\s-]", "", name).strip()
    safe = re.sub(r"[\s_-]+", "_", safe)
    return safe[:80] or "unknown"


def get_recent_chats_text(days: int = 7, max_chars: int = 20000) -> str:
    """
    Return formatted text of recent WhatsApp messages across all non-archived
    chats, for LLM context.
    """
    import db
    messages = db.get_all_non_archived_messages(days=days, per_chat_limit=100)
    if not messages:
        return ""

    lines = []
    for m in messages:
        time_str = m["time"].strftime("%Y-%m-%d %H:%M")
        sender = m["sender"]
        chat = m["chat_name"]
        content = m["content"][:500]
        lines.append(f"[{time_str}] ({chat}) {sender}: {content}")

    text = "\n".join(lines)
    return text[:max_chars]
