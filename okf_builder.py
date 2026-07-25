"""
OKF Builder — builds an Open Knowledge Format (OKF) bundle from WhatsApp chats.

OKF spec: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md

A bundle is a directory of markdown files with YAML frontmatter.
This module:
  1. Gets all non-archived chats
  2. For each chat, generates an OKF concept document via LLM
  3. Writes the bundle to okf_bundle/
  4. Generates index.md and log.md
  5. Builds a persona.md (global voice + per-group voice) so drafted
     responses sound like the user, not the bot.
"""

import os
import re
import datetime
from pathlib import Path

import config
import db
import llm


def _safe_filename(name: str) -> str:
    """Convert a chat name to a safe filename."""
    # Remove/replace unsafe chars
    safe = re.sub(r"[^\w\s-]", "", name).strip()
    safe = re.sub(r"[\s_-]+", "_", safe)
    return safe[:80] or "unknown"


def _slug(chat_name: str, chat_jid: str) -> str:
    """Generate a slug for a chat (used as concept ID)."""
    name = _safe_filename(chat_name)
    # add a short hash of jid for uniqueness
    jid_hash = abs(hash(chat_jid)) % 10000
    return f"{name}_{jid_hash}"


def build_okf_bundle(progress_callback=None, chats: list[dict] = None) -> str:
    """
    Build the complete OKF bundle from all non-archived chats.

    Args:
        progress_callback: optional callable(chat_name, index, total)
        chats: optional list of chat dicts to (re)build. If None, rebuilds ALL
            non-archived chats (full rebuild). If provided, only those chats'
            concept docs are (re)generated — used for incremental hourly updates.

    Returns:
        Path to the bundle directory.
    """
    bundle_dir = config.OKF_DIR
    bundle_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.date.today().isoformat()

    if chats is None:
        chats = db.get_non_archived_chats(min_messages=3)
    total = len(chats)

    if progress_callback:
        progress_callback("Starting", 0, total)

    concepts_built = 0
    errors = []

    for i, chat in enumerate(chats):
        chat_name = chat["name"]
        chat_jid = chat["jid"]

        if progress_callback:
            progress_callback(chat_name, i + 1, total)

        # Get messages for this chat
        messages = db.get_chat_messages(
            chat_jid, days=config.OKF_LOOKBACK_DAYS, limit=500
        )
        if not messages:
            continue

        # Format messages for LLM
        msgs_text = "\n".join(
            f"[{m['time'].strftime('%Y-%m-%d %H:%M')}] {m['sender']}: {m['content']}"
            for m in messages
        )

        # Generate OKF concept via LLM
        try:
            concept_md = llm.build_okf_concept(chat_name, msgs_text)
        except Exception as e:
            errors.append(f"{chat_name}: {e}")
            # Fallback: minimal concept doc
            concept_md = _fallback_concept(chat_name, messages, today)

        # Write concept file
        slug = _slug(chat_name, chat_jid)
        # put group chats in groups/ subdir, DMs in contacts/
        subdir = "groups" if chat_jid.endswith("@g.us") else "contacts"
        concept_dir = bundle_dir / subdir
        concept_dir.mkdir(exist_ok=True)
        concept_path = concept_dir / f"{slug}.md"

        with open(concept_path, "w", encoding="utf-8") as f:
            f.write(concept_md)

        concepts_built += 1

    # Refresh index + log on a full rebuild only (cheap to skip for incremental)
    if chats is None:
        all_chats = db.get_non_archived_chats(min_messages=3)
        _write_index(bundle_dir, all_chats, today)
        _write_log(bundle_dir, today, concepts_built, errors)

    return str(bundle_dir)


def concept_path_for(chat_name: str, chat_jid: str) -> Path:
    """Return the on-disk path of the concept doc for a chat (may not exist)."""
    slug = _slug(chat_name, chat_jid)
    subdir = "groups" if chat_jid.endswith("@g.us") else "contacts"
    return config.OKF_DIR / subdir / f"{slug}.md"


def read_concept_md(chat_name: str, chat_jid: str) -> str:
    """Read the current concept doc for a chat ("" if it does not exist)."""
    path = concept_path_for(chat_name, chat_jid)
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def incremental_update(chats: list[dict], progress_callback=None) -> list[dict]:
    """
    Incrementally rebuild concept docs ONLY for *chats* and return a list of
    per-chat diffs: [{"group", "old", "new"}].

    Used by the end-of-hour wrap-up to update the OKF and detect new context.
    """
    diffs: list[dict] = []
    if not chats:
        return diffs

    # Snapshot current versions before rebuilding
    snapshots = {}
    for chat in chats:
        snapshots[chat["jid"]] = read_concept_md(chat["name"], chat["jid"])

    # Rebuild only these chats
    build_okf_bundle(progress_callback=progress_callback, chats=chats)

    # Collect diffs (only where content changed)
    for chat in chats:
        old = snapshots.get(chat["jid"], "")
        new = read_concept_md(chat["name"], chat["jid"])
        if old != new:
            diffs.append({
                "group": chat["name"],
                "old": old,
                "new": new,
            })
    return diffs


def _fallback_concept(chat_name: str, messages: list[dict], today: str) -> str:
    """Minimal OKF concept when LLM fails."""
    recent = messages[-20:]
    body = "\n".join(
        f"- [{m['time'].strftime('%Y-%m-%d %H:%M')}] {m['sender']}: {m['content'][:200]}"
        for m in recent
    )
    return f"""---
type: WhatsApp Chat
title: {chat_name}
description: Auto-generated fallback concept (LLM was unavailable)
tags: [whatsapp, auto-generated]
timestamp: {today}T00:00:00Z
---

# Summary

This concept was generated as a fallback. The LLM was unavailable during OKF
build, so only raw recent messages are included.

# Recent Activity

{body}
"""


def _write_index(bundle_dir: Path, chats: list[dict], today: str) -> None:
    """Write the root index.md for the bundle."""
    lines = [
        f"# OKF Bundle — Updated {today}",
        "",
        f"Auto-generated from {len(chats)} non-archived WhatsApp chats.",
        "",
        "## Groups",
        "",
    ]

    groups = [c for c in chats if c["jid"].endswith("@g.us")]
    contacts = [c for c in chats if c["jid"].endswith("@s.whatsapp.net")]

    for chat in groups:
        slug = _slug(chat["name"], chat["jid"])
        lines.append(f"- [{chat['name']}](groups/{slug}.md) — {chat['msg_count']} msgs")

    lines.extend(["", "## Direct Messages", ""])
    for chat in contacts:
        slug = _slug(chat["name"], chat["jid"])
        lines.append(f"- [{chat['name']}](contacts/{slug}.md) — {chat['msg_count']} msgs")

    with open(bundle_dir / "index.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _write_log(bundle_dir: Path, today: str, count: int, errors: list[str]) -> None:
    """Write/update the log.md file."""
    log_path = bundle_dir / "log.md"

    existing = ""
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8")

    entry = f"- **Update**: Rebuilt OKF bundle with {count} concepts."
    if errors:
        entry += f" {len(errors)} errors occurred."

    # If there's already an entry for today, replace it; otherwise prepend
    today_header = f"## {today}"
    if today_header in existing:
        # Insert entry right after today's header
        existing = existing.replace(
            today_header, f"{today_header}\n{entry}", 1
        )
        content = existing
    else:
        new_section = f"## {today}\n{entry}\n"
        if existing:
            # Insert new section after the title
            lines = existing.split("\n", 1)
            title = lines[0]
            rest = lines[1] if len(lines) > 1 else ""
            content = title + "\n\n" + new_section + rest
        else:
            content = "# Directory Update Log\n\n" + new_section

    with open(log_path, "w", encoding="utf-8") as f:
        f.write(content)


# ── Persona builder ───────────────────────────────────────────────────────────

def _format_user_messages(messages: list[dict], limit: int = 80) -> str:
    """Render the user's own outgoing messages as a transcript."""
    mine = [m for m in messages if m.get("is_from_me")]
    mine = mine[-limit:]
    return "\n".join(
        f"[{m['time'].strftime('%Y-%m-%d %H:%M')}] {m['content'][:300]}"
        for m in mine
    )


def build_persona(progress_callback=None) -> str:
    """
    Build okf_bundle/persona.md describing how the user actually writes.

    Two layers:
      1. GLOBAL persona — tone, length, vocabulary, punctuation, emoji use,
         sign-off habits — distilled from the user's outgoing messages across
         all chats.
      2. PER-GROUP persona — how the user specifically writes in each major
         group (formality, language mix, who they address, common phrases).

    The persona is consumed by llm.get_action_options() so drafted responses
    sound like the user wrote them, not the bot.
    """
    chats = db.get_non_archived_chats(min_messages=5)
    # Keep the top 25 most active chats for per-group persona (cost control)
    chats = chats[:25]

    if progress_callback:
        progress_callback("persona: global", 0, len(chats) + 1)

    # ── 1. Global persona ───────────────────────────────────────────────────
    global_lines = []
    for chat in chats:
        msgs = db.get_chat_messages(chat["jid"], days=config.OKF_LOOKBACK_DAYS, limit=300)
        global_lines.append(_format_user_messages(msgs, limit=40))
    global_transcript = "\n\n".join(global_lines)[:60000]

    global_persona = ""
    try:
        global_persona = llm.build_global_persona(global_transcript)
    except Exception as e:
        print(f"[persona] global failed: {e}")
        global_persona = _fallback_global_persona(global_transcript)

    # ── 2. Per-group persona ────────────────────────────────────────────────
    per_group_blocks = []
    for i, chat in enumerate(chats):
        if progress_callback:
            progress_callback(f"persona: {chat['name']}", i + 1, len(chats) + 1)
        msgs = db.get_chat_messages(chat["jid"], days=config.OKF_LOOKBACK_DAYS, limit=300)
        transcript = _format_user_messages(msgs, limit=60)
        if len(transcript.splitlines()) < 5:
            continue
        try:
            block = llm.build_group_persona(
                chat_name=chat["name"],
                chat_jid=chat["jid"],
                transcript=transcript,
                global_persona=global_persona,
            )
            per_group_blocks.append(block)
        except Exception as e:
            print(f"[persona] {chat['name']} failed: {e}")

    today = datetime.date.today().isoformat()
    body = f"""---
type: User Persona
title: User — Communication Style
description: How the user writes across WhatsApp, used to draft replies in their voice
tags: [persona, voice, drafting]
timestamp: {today}T00:00:00Z
---

# Global Voice

{global_persona}

# Per-Group Voice

""" + "\n\n".join(per_group_blocks)

    config.PERSONA_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.PERSONA_FILE.write_text(body, encoding="utf-8")
    return str(config.PERSONA_FILE)


def _fallback_global_persona(transcript: str) -> str:
    return (
        "Write concisely and directly. Short sentences. Minimal emoji. "
        "Use lowercase often. No formal sign-offs. Skip greetings. "
        "Factual, action-oriented. Sample of user's own messages:\n\n"
        + transcript[:2000]
    )
