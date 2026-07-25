#!/usr/bin/env python3
"""
Dump all prompts + last 48h chat data + OKF context into a single .md file
for offline testing of different task-extraction / summarisation algorithms.
"""

import datetime
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import config
import db
import okf_reader

OUT = SCRIPT_DIR / "prompt_lab.md"

def _dump_db_data():
    """Collect last 48h of messages + OKF text."""
    sections = []

    # ── OKF bundle ──────────────────────────────────────────────────────────
    try:
        okf_text = okf_reader.read_bundle(max_chars=400_000)
        if okf_text:
            sections.append(("OKF BUNDLE", okf_text))
        else:
            sections.append(("OKF BUNDLE", "(empty — no OKF bundle yet)"))
    except Exception as e:
        sections.append(("OKF BUNDLE", f"(error: {e})"))

    # ── Recent chats (7 days, formatted) ────────────────────────────────────
    try:
        recent = okf_reader.get_recent_chats_text(days=7, max_chars=400_000)
        if recent:
            sections.append(("RECENT CHATS (7 days, formatted)", recent))
        else:
            sections.append(("RECENT CHATS (7 days)", "(no recent messages)"))
    except Exception as e:
        sections.append(("RECENT CHATS (7 days)", f"(error: {e})"))

    # ── Chat name → JID map ─────────────────────────────────────────────────
    try:
        jid_lines = []
        for chat in db.get_non_archived_chats(min_messages=0):
            jid_lines.append(f"  {chat['name']} → {chat['jid']}")
        sections.append(("CHAT NAME → JID MAP", "\n".join(jid_lines[:200])))
    except Exception as e:
        sections.append(("CHAT NAME → JID MAP", f"(error: {e})"))

    # ── Archived tasks ──────────────────────────────────────────────────────
    try:
        import archive_store
        archived = archive_store.get_archived_titles()
        if archived:
            sections.append(("ARCHIVED TASKS", "\n".join(f"- {t}" for t in archived)))
    except Exception:
        pass

    return sections

def _dump_prompts():
    """Extract the key prompts from llm.py as plain text."""
    prompts = []

    # -- Bulletin structure --
    prompts.append((
        "FULL BULLETIN STRUCTURE (how Python assembles the final message)",
        """The final Hourly Bulletin is NOT produced by a single LLM call.
It is assembled from 2 LLM calls + Python formatting:

=========== LLM CALL 1: Task Extraction ===========
Returns JSON array of {task_number, title, summary, source_chat, source_jid, score, urgency}
→ Python renders each task as:  *{N}.* {emoji} *{title}*  ∙  _{source_chat}_
  Emoji map: critical=🔴  high=🟠  medium=🟡  low=🟢
→ Title max 80 chars (enforced in LLM prompt)
→ Exactly TASKS_PER_PAGE (10) tasks, numbered page_start to page_end

=========== LLM CALL 2: Hourly Extras ===========
Returns JSON: {"done_today": [...], "left_on_read": [...]}
→ Python renders each done bullet as:  • {bullet}
→ Python renders each lor bullet as:  • {bulletin}

=========== FINAL BULLETIN (assembled in Python) ===========
🕒 *Hourly Bulletin* · HH:MM IST · Month DD
🆔 Session `ABC123`

📋 *Top 10 Tasks to check*
*1.* 🔴 *Task Title*  ∙  _Source Chat_
*2.* 🟠 *...*
...

✅ *What got done today*
• Approved July salary for the team
• ...

📥 *Left on read*
• Chat -- what was asked (who, time)
• ...

━━━━━━━━━━━━
• archive 1,3 • context 2 • action 4 • send 4A • more

CONSTRAINTS:
- Tasks must be numbered 1-N so user can reference them (archive 1,3 etc.)
- Archived tasks (in the list above) MUST be excluded from extraction
- source_jid MUST come from the JID map exactly — never guess
- If source_chat is not in JID map, source_jid MUST be "" (empty string)
- Every task needs: task_number, title, summary, source_chat, source_jid, score, urgency
- "done_today" bullets: max 12 words, start with action verb, no emoji
- "left_on_read" bullets: format "<chat> — <what was asked> (<who, time>)", max 14 words
- No markdown headers (##) in any LLM output — the Python code adds *bold* headers
- All outputs must be valid JSON or clean plain text (as specified per prompt)"""
    ))

    # -- Task extraction prompt --
    prompts.append((
        "TASK EXTRACTION PROMPT",
        """SYSTEM:
You are an AI Chief of Staff. Your job is to read the user's
WhatsApp knowledge base and extract the most important pending
follow-ups. You return ONLY valid JSON.

PROMPT TEMPLATE:
Today is {today}.

=== OPEN KNOWLEDGE FORMAT (OKF) WIKI ===
{okf_index[:40000]}

=== RECENT WHATSAPP CHATS (last 7 days) ===
{recent_chats[:20000]}
{archived_str}
{jid_hint}

INSTRUCTIONS:
Extract exactly {TASKS_PER_PAGE} pending follow-ups numbered {page_start} to {page_end}.

IMPORTANT: Extract ALL important pending items from ALL non-archived chats —
not just things directly asked of the user.  Look for:
  - Decisions that need to be made
  - Questions awaiting answers (from anyone, not just the user)
  - Status updates that were requested
  - Deadlines approaching
  - Blockers or escalations mentioned in any chat
  - Follow-ups committed to by anyone in any group

Sort by COMBINED SCORE (weighted, highest-first):
  1. UNANSWERED QUESTIONS (highest priority): direct asks, questions, or explicit
     requests directed at the user or the group that still have no response. Look
     for messages that end in question marks, requests for data/approval/decisions
     where no follow-up reply exists in the same chat. These are the most urgent
     because someone is waiting.
  2. BLOCKERS & DEADLINES: anything blocking progress, approaching deadlines,
     escalations, or time-sensitive decisions.
  3. RECENCY: more recent activity = higher score.
  4. IMPORTANCE: decisions needed, key deliverables, cross-team dependencies.
  5. USER INVOLVEMENT: small boost if the user is directly tagged or assigned,
     but items NOT involving the user are still included if they are important
     to the group/project.

Be explicit about WHY each task scores highly — cite the unanswered question,
blocker, or deadline that makes it urgent. Do NOT demote tasks just because
they're from a different chat or don't directly mention the user.

For EACH task return:
  "task_number": integer ({page_start}-{page_end}),
  "title": short title (max 80 chars),
  "summary": 1-2 sentence summary of what needs to happen,
  "source_chat": the WhatsApp group/chat name where this originated,
  "source_jid": the EXACT JID from the map above that matches source_chat (never guess),
  "score": float 0-100 (combined score),
  "urgency": one of "critical" | "high" | "medium" | "low"

If source_chat does not appear in the JID map, set source_jid to \"\" (empty string).
Return ONLY a JSON array of {TASKS_PER_PAGE} objects. No markdown, no preamble."""
    ))

    # -- Hourly extras: done-today + left-on-read --
    prompts.append((
        "HOURLY EXTRAS PROMPT (done_today + left_on_read)",
        """SYSTEM:
You are an AI Chief of Staff. You read a WhatsApp activity log and
summarize what the user got done today and where they left people on
read. You return ONLY valid JSON.

PROMPT TEMPLATE:
Today is {today}.

=== RECENT ACTIVITY LOG ===
{activity_text[:30000]}

=== OKF CONTEXT (for background) ===
{okf_text[:15000]}

From the activity log above, produce TWO lists:

1. "done_today": Exactly 5 crisp bullets of the most SUBSTANTIAL and MEANINGFUL
   things the user (messages tagged [ME]) got DONE or COMPLETED today.
   - ONLY items from TODAY (the most recent calendar day in the log).
   - EXCLUDE: "taking a call", "had a chat", "scheduling", "acknowledging",
     "noted", "ok", "thanks", reactions, or any trivial ack.
   - INCLUDE: decisions made, code/docs shipped, bugs fixed, reports shared,
     approvals given, contracts closed, escalations resolved, things shipped.
   - Each bullet: max 12 words, start with an action verb, no emoji, no fluff.
   - If nothing substantial was done today, return an empty list [].

2. "left_on_read": Exactly 5 crisp bullets of messages where the user LEFT
   PEOPLE ON READ — i.e. someone sent the user a direct question, request, or
   explicit ask (in a group or DM) that the user has NOT yet responded to.
   - Look for incoming questions/asks (to the user or to the group at large
     where the user is implicated) with no subsequent [ME] reply in that chat.
   - Each bullet: "<chat/group> — <what was asked> (<who asked, time>)".
   - Max 14 words per bullet. No emoji.
   - If none, return an empty list [].

Return ONLY JSON:
{"done_today": ["...","..."], "left_on_read": ["...","..."]}"""
    ))

    # -- Activity text formatting --
    prompts.append((
        "ACTIVITY LOG FORMAT (how messages are tagged for the LLM)",
        """Format: [{time}] ({chat_name}) [ME] {content}   ← messages from the user
         [{time}] ({chat_name}) {sender}: {content}    ← messages from others

The [ME] tag is used to identify the user's own messages.
The LLM uses this to:
  - For "done_today": look at [ME] messages for substantial accomplishments
  - For "left_on_read": find incoming messages (no [ME] tag) that have NO
    subsequent [ME] reply in the same chat"""
    ))

    # -- Activity log for last 48h --
    try:
        now_ist = datetime.datetime.now(config.IST)
        since = now_ist - datetime.timedelta(hours=48)
        activity = db.get_recent_activity(hours=48, per_chat_limit=100)
        lines = []
        for m in activity:
            time_str = m["time"].astimezone(config.IST).strftime("%Y-%m-%d %H:%M")
            chat = m.get("chat_name", "")
            if m.get("is_from_me"):
                lines.append(f"[{time_str}] ({chat}) [ME] {m['content'][:400]}")
            else:
                lines.append(f"[{time_str}] ({chat}) {m.get('sender','?')}: {m['content'][:400]}")
        prompts.append(("ACTIVITY LOG (last 48h, formatted for LLM)", "\n".join(lines)))
    except Exception as e:
        prompts.append(("ACTIVITY LOG (last 48h)", f"(error: {e})"))

    return prompts


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("# prompt-lab — Full Context Dump\n\n")
        f.write(f"**Generated**: {datetime.datetime.now(config.IST).isoformat()}\n\n")
        f.write("This file contains ALL the data and prompts needed to test\n")
        f.write("different task-extraction and hourly-summary algorithms.\n\n")
        f.write("---\n\n")

        f.write("## 1. PROMPTS (Current Logic)\n\n")
        for title, body in _dump_prompts():
            f.write(f"### {title}\n\n")
            f.write("```\n")
            f.write(body)
            f.write("\n```\n\n")

        f.write("---\n\n")
        f.write("## 2. DATABASE CONTEXT\n\n")
        for title, body in _dump_db_data():
            f.write(f"### {title}\n\n")
            if len(body) > 2000:
                f.write(f"_(content truncated — {len(body)} chars total)_\n\n")
                f.write(body)
            else:
                f.write(body)
            f.write("\n\n")

    print(f"✅ Written to {OUT}")
    print(f"   Size: {OUT.stat().st_size:,} bytes")