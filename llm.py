"""
LLM layer — Gemini wrapper for all AI calls.

Uses the google-genai SDK (same as momentum_update generators).
Provides:
  - extract_tasks()        → top-N scored task list from OKF + raw chats
  - get_hourly_extras()    → "what got done today" + "left on read" bullets
  - build_okf_concept()    → single OKF markdown concept for one chat
  - build_global_persona() → global user voice for drafting
  - build_group_persona()  → per-group user voice for drafting
  - get_context()          → rich contextual update on a task from OKF
  - summarize_hourly_context() → ≤60 word wrap-up of new OKF context this hour
  - get_action_options()   → 7 persona-aware responses for a task's group (A-G)
  - parse_intent()         → parse free-text user intent
  - filter_bot_messages()  → strip task_dog/bulletin scoreboards from activity log
  - build_business_context() → goals/people/open-threads from OKF
"""

import json
import datetime
from typing import Optional

import config
from google import genai
from google.genai import types

_client: Optional[genai.Client] = None

# ── Bot message filter (0.2) ─────────────────────────────────────────────────

BOT_PREFIXES = (
    "🐕 task_dog", "🕒 *Hourly Bulletin*", "📥", "🧠", "🆔 Session",
    "📊 *SCOREBOARD*", "*📊 SCOREBOARD*", "✅ MOVED SINCE LAST UPDATE",
    "*✅ MOVED SINCE LAST UPDATE*",
)

def is_bot_message(text: str) -> bool:
    """Check if a message looks like bot output (scoreboards, bulletins)."""
    stripped = text.lstrip()
    for prefix in BOT_PREFIXES:
        if stripped.startswith(prefix) or prefix in stripped[:100]:
            return True
    return False

def filter_bot_messages(activity_text: str) -> str:
    """Remove lines that look like bot output from activity log."""
    lines = activity_text.split("\n")
    filtered = []
    for line in lines:
        # Lines are formatted as: [time] (chat) [ME] content
        # or: [time] (chat) sender: content
        # Extract just the content part after the last ") "
        idx = line.rfind(") ")
        if idx >= 0:
            content = line[idx+2:]
        else:
            content = line
        if not is_bot_message(content):
            filtered.append(line)
    return "\n".join(filtered)

# ── Business context builder (0.3) ───────────────────────────────────────────

def build_business_context(okf_text: str) -> str:
    """
    Extract the useful business context from the OKF bundle, skipping
    wa-pull's own project README and architecture docs.

    If the OKF is mostly the bot's own project docs, return an empty string
    so the LLM doesn't get confused.
    """
    if not okf_text:
        return "(no business context available)"

    bot_signal_words = ("wa-pull", "lsof -i", "store/messages.db", "Go Bridge",
                        "bridge HTTP", "whatsmeow", "QR auth", "mechat_poll",
                        "MECHAT_POLL", "File Map", "## File Map", "## Architecture")

    bot_signal_count = sum(1 for w in bot_signal_words if w.lower() in okf_text.lower())

    # If more than half the signal words appear, this is the bot's own README
    if bot_signal_count > len(bot_signal_words) // 2:
        return "(no business context available)"

    # Otherwise, keep the OKF but from the tail (most recent) and summarize
    okf_tail = okf_text[-30000:]
    return okf_tail


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(
            api_key=config.GEMINI_API_KEY,
            http_options={"timeout": 60_000},
        )
    return _client


def _call(model: str, prompt: str, system: str = "", temperature: float = 0.2) -> str:
    """Single Gemini call, returns text."""
    cfg_kwargs = {"temperature": temperature}
    if system:
        cfg_kwargs["system_instruction"] = system
    resp = _get_client().models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(**cfg_kwargs),
    )
    return resp.text.strip()


def _call_json(model: str, prompt: str, system: str = "", temperature: float = 0.2) -> dict | list:
    """Call Gemini and parse JSON from the response."""
    text = _call(model, prompt, system, temperature)
    # strip markdown fences
    if text.startswith("```json"):
        text = text[7:]
    elif text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    return json.loads(text.strip())


# ── Pulse action generator (real-time monitor) ────────────────────────────────

def generate_pulse(trigger: str, trigger_msgs: list[dict], chat_name: str,
                    chat_jid: str, chat_context: str,
                    okf_concept: str, persona_text: str = "") -> dict:
    """
    Decide what immediate action a trigger needs, and generate a pulse alert
    with context, a task, and A/B/C quick response options.

    Args:
        trigger:       "user_sent" | "user_tagged" | "user_asked" | "quick_succession"
        trigger_msgs:  list of triggering message dicts (content, is_from_me, sender)
        chat_name:     name of the chat the trigger came from
        chat_jid:      JID of that chat
        chat_context:  recent messages from that chat (formatted transcript)
        okf_concept:   this chat's OKF concept doc ("" if none)
        persona_text:  user's persona for drafting in their voice

    Returns:
        {"action_type": "pulse"|"none", "context": "2 lines",
         "task": {title, summary, who_waiting, waiting_hours, deadline,
                  source_chat, source_jid, urgency, state, is_new, score, score_reason},
         "options": {"A": "...", "B": "...", "C": "..."}}
    """
    now = datetime.datetime.now(config.IST)
    now_str = now.strftime("%H:%M")

    system = (
        "You are the user's real-time chief of staff embedded in their WhatsApp. "
        "A message just happened in a chat. You decide if it needs a pulse "
        "alert and return ONLY valid JSON. You are RUTHLESSLY selective — a "
        "pulse costs the user's attention, so 80%+ of triggers should return "
        "'none'. Only fire when there is something material: a reply owed, a "
        "commitment to track, a decision needed, or a development they must "
        "know about. They send directions, instructions, and factual "
        "replies constantly — those do NOT need a pulse. Never pad. Never "
        "offer help."
    )

    persona_block = ""
    if persona_text:
        persona_block = f"""
=== USER PERSONA (match this voice for any drafted reply) ===
{persona_text[:4000]}
"""

    trigger_descs = {
        "user_sent": "The user just sent a message in this chat.",
        "user_tagged": "Someone just tagged the user in this chat.",
        "user_asked": "Someone just asked the user a question in this chat.",
        "quick_succession": "Several messages arrived in quick succession in this group.",
    }
    trigger_desc = trigger_descs.get(trigger, "Something happened in this chat.")

    trigger_msgs_text = "\n".join(
        f"  {'[ME]' if m.get('is_from_me') else m.get('sender', '?')}: {m.get('content', '')[:300]}"
        for m in trigger_msgs
    )

    prompt = f"""Right now it is {now_str} IST.

TRIGGER: {trigger_desc}

TRIGGERING MESSAGES:
{trigger_msgs_text}

=== CHAT: {chat_name} ({chat_jid}) ===

=== RECENT CHAT CONTEXT (last {config.MONITOR_CONTEXT_MESSAGES} messages) ===
{chat_context[:15000]}

=== OKF CONCEPT FOR THIS CHAT ===
{okf_concept[:8000] if okf_concept else "(no OKF concept yet)"}
{persona_block}
────────────────────────────────────────────────────────
You are the gatekeeper. A pulse alert costs the user's attention — only fire one
when there is something MATERIAL they need to: track, follow up on, respond to,
or be aware of. When in doubt, return "none".

=== WHEN TO FIRE A PULSE ===

Fire "pulse" ONLY if one of these is true:
  1. A REPLY IS OWED — someone asked the user a question or made a request, and
     there is no response from them yet. The task = sending that reply.
  2. A COMMITMENT WAS MADE — the user (or someone) committed to doing something
     with a timeline or deliverable ("will share by EOD", "sending in a bit",
     "call tomorrow 3pm"). The task = tracking that deliverable.
  3. SOMETHING MATERIAL HAPPENED — a number moved, a decision was made, a
     blocker emerged, an escalation occurred. The task = acknowledging or
     acting on it.
  4. A DECISION IS NEEDED — two positions stated, no resolution, the user needs
     to break the tie. The task = making the call.
  5. QUICK_SUCCESSION and the thread needs the user's input — a fast exchange
     happened and someone is waiting for their take.

=== WHEN TO RETURN "none" ===

Return "none" if ANY of these apply:
  - The user sent an informational message with no follow-up owed (directions,
    instructions, logistics, "come to tower 5 flat 903", "send me the file",
    status updates, "noted", "ok", "thanks", "done").
  - The message is a one-off instruction or factual reply with no open loop
    ("the meeting is at 3pm", "use the staging URL", "it's on the shared
    drive").
  - The user sent a message and nobody is waiting on a response — they gave an
    instruction, shared info, or closed a loop. No follow-up action exists.
  - The message is banter, emoji, an ack, or social chatter.
  - The "task" would just restate what was said ("Tell someone to come to
    tower 5" is NOT a task — they already told them).
  - The message is <10 words and doesn't contain a commitment, deadline, or
    question from someone else.

Be ruthless. 80%+ of user_sent triggers should return "none". The user sends
directions, instructions, and factual replies constantly — those do NOT need
a pulse. A pulse means "user, you need to do something about this LATER".

=== PULSE FORMAT ===

If you decide to fire:

1. "context": exactly 2 lines summarizing what happened and why it matters.
   No emoji. No headers. Just two plain lines.

2. "task": a task to track (same format as hourly bulletin tasks):
   - "title": imperative verb + specific object + who/what's stuck. ≤80 chars.
     MUST contain a proper noun or number. NEVER a raw numeric @ID.
   - "summary": 1-2 sentences on what's needed next.
   - "who_waiting": person blocked or awaiting reply; "" if nobody specific.
   - "waiting_hours": integer hours since the message that needs a response.
   - "deadline": stated deadline verbatim if one exists, else "".
   - "source_chat": "{chat_name}"
   - "source_jid": "{chat_jid}"
   - "urgency": "critical" | "high" | "medium" | "low"
   - "state": "waiting_on_me" | "waiting_on_them" | "needs_decision" | "at_risk"
   - "is_new": true
   - "score": integer (use the same additive rubric: +40 blocked, +30 money,
     +25 deadline ≤24h, +20 unanswered >4h, +15 overdue, +10 fresh, +10 OKR,
     -20 stale >48h, -25 unchanged, -30 chatter)
   - "score_reason": the arithmetic string

3. "options": three quick response options drafted in the user's voice (use persona):
   - "A": Follow-up nudge — soft, polite, grounded in the original ask. 1-2 lines.
   - "B": Clarity ask — ask for timelines, ownership, or missing details. 1-2 lines.
   - "C": Decision/recap — ask for a decision to proceed, or recap context. 1-3 lines.
   Each option must be self-contained — no ambiguous pronouns (this/that/it).
   Never output a raw numeric @ID — resolve to a name or describe the role.

4. "action_type": "pulse" if an alert is warranted, "none" if not.

Rules:
- IGNORE bot output ("🐕 task_dog", "🕒 *Hourly Bulletin*", "*📊 SCOREBOARD*",
  "*✅ MOVED SINCE LAST UPDATE*"). These are this bot's own posts.
- If the trigger is a TAG or QUESTION, the task is about responding.
- If the trigger is QUICK_SUCCESSION, the task is about catching up on what
  the user missed and whether he needs to weigh in.
- "none" means nothing actionable — no alert. Default to "none".

Return ONLY:
{{"action_type": "...", "context": "...", "task": {{...}}, "options": {{"A": "...", "B": "...", "C": "..."}}}}
"""
    try:
        data = _call_json(config.GEMINI_MODEL_FAST, prompt, system, temperature=0.2)
    except Exception as e:
        print(f"[monitor-llm] generate_pulse failed: {e}")
        return {"action_type": "none", "context": "", "task": {}, "options": {}}

    if isinstance(data, dict):
        return {
            "action_type": data.get("action_type", "none"),
            "context": data.get("context", ""),
            "task": data.get("task", {}),
            "options": data.get("options", {}),
        }
    return {"action_type": "none", "context": "", "task": {}, "options": {}}


# ── Task extraction (v4 — 7-step rubric) ────────────────────────────────────

# Module-level storage for last bulletin's tasks (for previous_titles dedup)
_last_bulletin_tasks: list[dict] = []

def set_last_bulletin_tasks(tasks: list[dict]) -> None:
    """Store the tasks from the last bulletin for hour-over-hour freshness."""
    global _last_bulletin_tasks
    _last_bulletin_tasks = list(tasks)


def extract_tasks(okf_index: str, recent_chats: str, offset: int = 0,
                  archived_titles: list[str] = None,
                  chat_name_to_jid: dict = None,
                  previous_titles: str = "(unavailable)") -> list[dict]:
    """
    Extract top-N tasks using the v4 7-step rubric with additive scoring.

    Args:
        okf_index:      business context text (goals/people/open-threads)
        recent_chats:   formatted recent WhatsApp messages (chronological, tail-truncated)
        offset:         0 for first page, N for next page
        archived_titles: task titles to exclude
        chat_name_to_jid: map of chat_name → exact JID
        previous_titles: titles from last bulletin for dedup/is_new
    """
    archived_str = ""
    if archived_titles:
        archived_str = "\n".join(f"- {t}" for t in archived_titles)

    page_start = offset + 1
    page_end = offset + config.TASKS_PER_PAGE

    jid_hint = ""
    if chat_name_to_jid:
        jid_lines = [f'  "{name}": "{jid}"' for name, jid in chat_name_to_jid.items()]
        jid_hint = "\n".join(jid_lines[:200])

    now = datetime.datetime.now(config.IST)
    now_str = now.strftime("%H:%M")
    weekday = now.strftime("%A")
    today = now.strftime("%Y-%m-%d")

    # Tail-truncation (0.1): always keep the most recent data
    okf_index = build_business_context(okf_index)
    recent_chats_tail = recent_chats[-25000:] if len(recent_chats) > 25000 else recent_chats

    system = (
        "You are the Chief of Staff to a busy operator who runs "
        "multiple product lines through ~40+ WhatsApp groups. "
        "They are terse, numbers-first, and impatient with vagueness. They do not "
        "want to be told a topic exists — they want to know what is stuck, who "
        "is sitting on it, and how long it has been sitting. "
        "You return ONLY a valid JSON array. No prose, no markdown, no preamble."
    )

    prompt = f"""Right now it is {now_str} IST ({weekday}). Today is {today}.

=== BUSINESS CONTEXT ===
{okf_index}

=== CHAT ACTIVITY (most recent last) ===
{recent_chats_tail}

=== ALREADY ARCHIVED — NEVER RESURFACE THESE ===
{archived_str or "(none)"}

=== CHAT NAME → JID MAP ===
{jid_hint or "(none)"}

=== SHOWN IN THE LAST BULLETIN (one hour ago) ===
{previous_titles}

────────────────────────────────────────────────────────
STEP 1 — IGNORE THESE ENTIRELY

Do not extract anything from, and do not count as activity:
  · Messages starting with "🐕 task_dog", "🕒 *Hourly Bulletin*", "🆔 Session",
    or containing "*📊 SCOREBOARD*" / "*✅ MOVED SINCE LAST UPDATE*".
    These are this bot's own prior output. Mining them creates feedback loops.
  · One-to-three-word [ME] fragments with no content: "Ok", "Hey", "What", "Y",
    "Call", "No no", "Sad", "Yes".
  · Pure link drops, forwarded articles, emoji-only messages, "ack", "noted",
    "thanks", "sharing", "checking", joining/leaving notices.
  · Anything whose title or substance matches the ARCHIVED list above, even
    loosely reworded. When in doubt that it is a re-run of an archived item,
    drop it.

STEP 2 — FIND OPEN LOOPS

An open loop is a specific thing that is unresolved and has a named human on
one end of it. Scan every non-archived chat for:
  A. A direct question or ask to the user with no [ME] reply after it.
  B. A question or ask to anyone that has gone unanswered in-thread.
  C. A commitment someone made ("will share", "by EOD", "ETA Friday", "sharing
     in a bit") where the deliverable has not landed. Note how long ago.
  D. A decision that is being debated and needs a call — two positions stated,
     no resolution.
  E. A number that moved the wrong way, or a target with no update.
  F. An escalation, outage, or compliance/legal exposure.
  G. Personal / family items (health, kids, travel, home) — these are real and
     must not be filtered out for being non-work.

STEP 3 — MERGE

One task per THREAD, not per message and not per chat. If the same substance
appears in several groups, merge into one task and attribute it to the chat
where the decision will actually be made.
  Example: Alice chasing vendor config in "Merchant onboarding",
  Bob replying about credentials, and Carol asking for the config are ONE task,
  not three. Source chat = "TIDs and merchant onboarding for pay online merchants".
Never return two tasks whose titles share their main object.

STEP 4 — SCORE (additive, show your arithmetic)

  +40  Someone is explicitly blocked or waiting on the user personally
  +30  Money, compliance, outage, or an escalation that has reached a CXO
  +25  A hard deadline inside the next 24h, or one already missed
  +20  An unanswered direct question (any recipient), aged > 4 hours
  +15  A stated commitment now overdue (add +5 per full day overdue, cap +15)
  +15  A decision is deadlocked and only the user can break the tie
  +10  Movement in the last 60 minutes (this is an HOURLY bulletin — reward freshness)
  +10  Ties to a red OKR metric named in BUSINESS CONTEXT
  +10  Family / health / personal, time-bound
  −20  Last activity older than 48h with no new movement
  −25  Already shown in the last bulletin AND nothing has changed since
  −30  It is chatter, opinion, or banter with no deliverable attached

STEP 5 — WRITE THE TITLE (this is the only field the user sees)

Format:  <Imperative verb> <specific object> — <who is blocking / what is stuck>
Rules:
  · ≤ 80 characters. Hard limit.
  · MUST contain at least one proper noun or one number.
  · MUST name a person or a group where one exists.
  · Never a bare topic. Never "Follow up on X" / "Address Y" / "Discuss Z"
    without saying what specifically and with whom.
  · NEVER output a raw numeric WhatsApp ID (e.g. @247265746751488). If you
    cannot resolve it to a human name from context, describe the role or omit.
  · Use the user's register: direct, lowercase-tolerant, no corporate padding.

  BAD:   Address Reddit escalations on choice flow rewards
  GOOD:  Break feature-page deadlock — Bob wants a call, Carol waiting 3h

  BAD:   Follow up on invoice issue
  GOOD:  Chase vendor invoice RCA — promised 22 Jul, 36h overdue

  BAD:   Coin Rush discussion
  GOOD:  Get a who-does-what on Coin Rush — asked twice, only "Anni" so far

STEP 6 — RETURN

Exactly {config.TASKS_PER_PAGE} objects, numbered {page_start}–{page_end}, highest
score first. Each object:

  "task_number":   integer
  "title":         per STEP 5, ≤80 chars
  "summary":       2–3 sentences. What happened, what specifically is needed
                   next, and what happens if it slips. Name people.
  "ask":           the single concrete next action, ≤15 words, starts with a verb
  "who_waiting":   the person blocked or awaiting reply; "" if nobody specific
  "waiting_hours": integer hours since the last message that needed a response
  "deadline":      stated deadline verbatim if one exists, else ""
  "evidence":      the trigger message, quoted or tightly paraphrased, ≤20 words
  "state":         "waiting_on_me" | "waiting_on_them" | "needs_decision" | "at_risk"
  "is_new":        true if this was NOT in the previous bulletin
  "source_chat":   exact chat name
  "source_jid":    the EXACT JID from the map; "" if the chat is not in the map
  "score":         integer, the STEP 4 total
  "score_reason":  the arithmetic, e.g. "40 blocked + 25 deadline + 10 fresh = 75"
  "urgency":       see below

URGENCY — calibrate, do not inflate:
  "critical"  someone senior is blocked right now, or money/compliance/outage
              is live, or a deadline passes today. Expect 1–2 per bulletin.
  "high"      needs the user today; a person is actively waiting. Expect 3–4.
  "medium"    this week; no one blocked at this moment. Expect 3–4.
  "low"       tracking only. Expect 1–2.
  A bulletin with more than 2 "critical" is wrong. Re-rank until it is not.

STEP 7 — SELF-CHECK BEFORE RETURNING

Verify, silently:
  1. No two titles cover the same thread.
  2. No title exceeds 80 chars.
  3. No title contains a numeric @ID.
  4. Every title has a proper noun or a number.
  5. No item matches anything in the ARCHIVED list.
  6. Nothing sourced from a task_dog / Hourly Bulletin message.
  7. At most 2 critical.
  8. At least 3 items have is_new = true. If not, you have under-weighted the
     last few hours of the log — re-scan the tail and re-rank.
  9. Every source_jid is copied character-for-character from the map, or "".

Output ONLY the JSON array.
"""
    data = _call_json(config.GEMINI_MODEL_FAST, prompt, system)
    if isinstance(data, list):
        set_last_bulletin_tasks(data)
        return data
    if isinstance(data, dict) and "tasks" in data:
        tasks = data["tasks"]
        set_last_bulletin_tasks(tasks)
        return tasks
    return []


# ── OKF concept builder ──────────────────────────────────────────────────────

def build_okf_concept(chat_name: str, messages_text: str) -> str:
    """
    Generate a single OKF markdown concept document for one WhatsApp chat.
    Returns markdown with YAML frontmatter (conformant with OKF v0.1 spec).
    """
    today = datetime.date.today().isoformat()
    system = (
        "You are a knowledge architect. You synthesize WhatsApp chat logs "
        "into structured OKF (Open Knowledge Format) markdown documents. "
        "You return ONLY the markdown document, nothing else."
    )

    prompt = f"""Create an OKF concept document for this WhatsApp chat.

Chat Name: {chat_name}
Date: {today}

Chat Log:
{messages_text[:30000]}

Output a markdown file that starts with YAML frontmatter delimited by --- and
followed by a markdown body.  The frontmatter MUST include:
  type: WhatsApp Chat
  title: {chat_name}
  description: one-line summary of the chat's purpose
  tags: [list of relevant tags]
  timestamp: {today}T00:00:00Z

The body should have these sections:
  # Summary
  A 2-3 paragraph overview of what this chat is about and recent activity.

  # Open Items
  Bullet list of pending tasks, open questions, and unresolved decisions.
  Each bullet should include who is involved and what the next step is.

  # Key Decisions
  Bullet list of decisions made, with dates if available.

  # Recent Activity
  Chronological bullet list of significant recent events.

  # Participants
  List of key participants and their roles/involvement.

Use standard markdown. Link to other concepts with [text](relative-path.md)
when referencing other groups or topics. Return ONLY the markdown document.
"""
    return _call(config.GEMINI_MODEL_FAST, prompt, system, temperature=0.3)


# ── Persona builders ─────────────────────────────────────────────────────────

def build_global_persona(transcript: str) -> str:
    """
    Distill the user's global communication style from their outgoing messages.
    Returns a concise persona description used to draft replies in their voice.
    """
    system = (
        "You are a communication analyst. You study a person's WhatsApp "
        "messages and produce a concise persona profile of how they write. "
        "Return ONLY the profile text (no markdown headers)."
    )

    prompt = f"""Below is a transcript of messages written by the user
across multiple WhatsApp groups. Study the style carefully.

=== USER MESSAGES ===
{transcript[:50000]}

Produce a CONCISE persona profile (max 300 words) covering:
1. Tone (casual/formal/mixed?)
2. Typical message length (1-liners? paragraphs?)
3. Punctuation habits (periods? lowercase? ellipsis?)
4. Emoji usage (how many, which kinds, where?)
5. Language mix (English only? Hindi transliteration? abbreviations?)
6. Sign-off habits (do they sign off? how?)
7. Common phrases or catchwords they use
8. How they address people (first name? handles? @-mentions?)
9. Directness level (blunt? diplomatic? questioning?)
10. Any distinctive patterns (e.g. bullet points, numbered lists, ALL CAPS for emphasis)

Format as a short paragraph per point. Be specific with examples from the transcript.
"""
    return _call(config.GEMINI_MODEL_FAST, prompt, system, temperature=0.3)


def build_group_persona(chat_name: str, chat_jid: str,
                        transcript: str, global_persona: str) -> str:
    """
    Build a per-group persona: how the user specifically writes in one group.
    Returns a markdown block headed with the group name + JID.
    """
    system = (
        "You are a communication analyst. You study how a person writes "
        "in one specific WhatsApp group and note what's different from "
        "their global style. Return ONLY the analysis text."
    )

    prompt = f"""=== GLOBAL PERSONA ===
{global_persona}

=== USER MESSAGES IN GROUP: {chat_name} ({chat_jid}) ===
{transcript[:15000]}

Produce a CONCISE per-group persona (max 150 words) for how the user writes
specifically in "{chat_name}". Focus on:
- What's DIFFERENT from their global style here (more formal? more casual?)
- Who they typically address or reply to in this group
- Common phrases or patterns unique to this group
- Language/tone shift (e.g. more Hindi, more business-like, more emoji)

Format as:
## {chat_name}
**JID:** `{chat_jid}`

[analysis paragraph]
"""
    return _call(config.GEMINI_MODEL_FAST, prompt, system, temperature=0.3)


# ── Context / Action ──────────────────────────────────────────────────────────

def get_context(task_title: str, task_summary: str, source_chat: str,
                okf_text: str, recent_chats: str) -> str:
    """Generate a rich contextual update on a specific task using the OKF."""
    system = (
        "You are an AI Chief of Staff. Provide a rich, detailed contextual "
        "update on a specific task using the OKF knowledge base and recent chats. "
        "Format for WhatsApp: use *bold* for key terms, keep paragraphs short. "
        "No markdown headers (##). Return only the message text."
    )

    prompt = f"""TASK: {task_title}
SUMMARY: {task_summary}
SOURCE CHAT: {source_chat}

=== OKF KNOWLEDGE BASE ===
{okf_text[:30000]}

=== RECENT CHATS (last 7 days) ===
{recent_chats[:15000]}

Provide a detailed contextual update on this task. Include:
1. History: how this task started and evolved
2. Current status: where things stand now
3. Key people involved
4. Recent developments (from recent chats if any)
5. What's blocking progress (if anything)
6. Recommended next steps

Format for WhatsApp. Be specific and cite which chat/group information came from.
"""
    return _call(config.GEMINI_MODEL_PRO, prompt, system, temperature=0.3)


def get_hourly_extras(activity_text: str, okf_text: str,
                      task_titles: str = "(none)") -> dict:
    """
    Produce the two extra sections of the hourly bulletin (v4):
      - done_today: 0-5 crisp bullets of substantial things the user closed today
      - left_on_read: 0-5 bullets of messages where the user left people on read

    Returns dict: {"done_today": [str, ...], "left_on_read": [str, ...]}.
    """
    now = datetime.datetime.now(config.IST)
    now_str = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")

    # Tail-truncation (0.1) + bot filter (0.2)
    activity_tail = activity_text[-60000:] if len(activity_text) > 60000 else activity_text
    activity_tail = filter_bot_messages(activity_tail)
    business_ctx = build_business_context(okf_text)

    system = (
        "You are the user's Chief of Staff. You read his WhatsApp activity log and "
        "report what he actually closed today, and who he has left hanging. "
        "He writes in fragments — three-word messages that are often rulings, not "
        "chatter. Your job is to reconstruct the substance from the thread around "
        "them. You return ONLY valid JSON."
    )

    prompt = f"""Right now it is {now_str} IST. Today is {today}.

=== ACTIVITY LOG (most recent last) ===
{activity_tail}

=== BUSINESS CONTEXT ===
{business_ctx}

=== ALREADY LISTED AS OPEN TASKS IN THIS BULLETIN — DO NOT REPEAT ===
{task_titles}

────────────────────────────────────────────────────────
IGNORE ENTIRELY:
  · Messages beginning "🐕 task_dog", "🕒 *Hourly Bulletin*", "🆔 Session", or
    containing "*📊 SCOREBOARD*". These are this bot's own output posted under
    the user's account. They are never accomplishments and never asks.
  · Broadcast copy-paste sent to many chats at once (e.g. the same "exit the
    promotional channel" text to 8 people) — count that as ONE item, not eight.
  · Emoji, acks, link drops, "ok", "thanks", "noted".

────────────────────────────────────────────────────────
LIST 1 — "done_today"

Up to 5 bullets. Only from TODAY ({today}). FEWER IS BETTER — if only two
things genuinely landed, return two. Never pad. If nothing, return [].

A [ME] message counts only if the surrounding thread shows it CHANGED
something: a ruling given, a number set, an approval or rejection issued, a
dispute settled, an escalation opened or closed, a person unblocked, a
standard enforced.

Reconstruct from context, do not transcribe. User's own words are fragments:
  Thread: Alice asks why 125 cashback on a ₹125 product txn as part of a deal.
          [ME] "Size first" … [ME] "cashback on full bill payment and the bill
          amount should be minimum of 125 to avoid abuse" … Alice: "ok, this works."
  BAD  →  "Said size first on product"
  GOOD →  "Set cashback floor at ₹125 min bill to block abuse"

  Thread: [ME] "How did this happen. Need a full rca" / "Very disappointed"
          after vendor invoice error.
  BAD  →  "Expressed disappointment to vendor"
  GOOD →  "Demanded RCA from vendor on invoice automation failure"

Each bullet: ≤12 words, starts with a past-tense action verb, no emoji, no
name-dropping for its own sake, but name the counterparty when it clarifies.
Prefer the item with a number or a name in it over the one without.

────────────────────────────────────────────────────────
LIST 2 — "left_on_read"

Up to 5 bullets. Fewer is better. If none, return [].

Include only where ALL of these hold:
  · Someone sent a question, request, or explicit ask;
  · It was directed at the user — a DM, or a group message tagging him, or a
    group ask that clearly lands on him by ownership;
  · There is no [ME] message in that chat afterwards that addresses it;
  · It is NOT already covered by the open-task list above.

Rank by (seniority of asker × hours waiting). Something a peer asked six hours
ago beats something a vendor asked twenty minutes ago.

Format exactly: "<chat> — <what was asked> (<who>, <time>)"
  GOOD:  "Product Team — wants your POV on funnel vs landing page (Alice, 2:17pm)"
  GOOD:  "Bob Smith — asking if you're back this week (Bob, 9:48am)"
  BAD:   "Some group — a question was asked (someone, earlier)"

≤14 words. No emoji. Never a raw numeric @ID — resolve to a name or describe
the role.

────────────────────────────────────────────────────────
Return ONLY:
{{"done_today": ["...", "..."], "left_on_read": ["...", "..."]}}
"""
    data = _call_json(config.GEMINI_MODEL_FAST, prompt, system, temperature=0.2)
    if isinstance(data, dict):
        return {
            "done_today": data.get("done_today", []) or [],
            "left_on_read": data.get("left_on_read", []) or [],
        }
    return {"done_today": [], "left_on_read": []}


def summarize_hourly_context(diffs: list[dict]) -> str:
    """
    Given a list of per-chat OKF diffs (old vs new concept docs) for chats that
    were active this hour, produce a crisp, well-structured WhatsApp message of
    the KEY NEW CONTEXT absorbed this hour.

    Each diff dict: {"group": <chat name>, "old": <prev md>, "new": <new md>}.

    Rules:
      - ≤ 60 words total.
      - Only call out NEW or CHANGED context (do not restate what was already known).
      - Structure by group / sub-group.
      - If nothing new/changed, return EXACTLY: "No new context absorbed this hour."
    """
    if not diffs:
        return "No new context absorbed this hour."

    system = (
        "You are an AI Chief of Staff. You compare old vs new knowledge-base "
        "notes for chats active in the last hour and report ONLY the new/changed "
        "context in a crisp WhatsApp message. Return ONLY the message text."
    )

    diff_block_parts = []
    for d in diffs:
        group = d.get("group", "?")
        old = (d.get("old") or "")[:4000]
        new = (d.get("new") or "")[:4000]
        if not new:
            continue
        if old == new:
            continue
        diff_block_parts.append(
            f"### GROUP: {group}\n"
            f"--- PREVIOUS ---\n{old}\n\n"
            f"--- NOW ---\n{new}\n"
        )
    if not diff_block_parts:
        return "No new context absorbed this hour."

    diff_block = "\n\n".join(diff_block_parts)[:30000]

    prompt = f"""Below are the OLD and NEW knowledge-base notes for chats that
were active in the last hour.

{diff_block}

Produce a SINGLE WhatsApp message summarizing the KEY NEW CONTEXT absorbed this
hour. Rules:
- ≤ 60 words total. Be ruthless about brevity.
- ONLY call out NEW or CHANGED context. Do not restate things already known.
- Structure by group; within a group, note the sub-topic / what changed.
- Use *bold* for group names. No emoji. No headers (##).
- This message doubles as "what happened this hour and how it is saved in memory".
- If there is genuinely nothing new or changed, reply EXACTLY:
  No new context absorbed this hour.
"""
    return _call(config.GEMINI_MODEL_FAST, prompt, system, temperature=0.2)


def get_action_options(task_title: str, task_summary: str, source_chat: str,
                       source_jid: str, task_number: int,
                       okf_text: str, recent_chats: str,
                       persona_text: str = "") -> str:
    """
    Generate possible responses the user could send to the group for this task.
    Drafts are written in the USER's voice (persona), not the bot's voice.

    Seven options, grouped by intent:
      A. Followup — mild nudge
      B. Followup — assertive, with context
      C. Clarity — clarify timelines
      D. Clarity — clarify who closes what / missing details
      E. Recap — give context of the issue, summarized in one place
      F. Decision — ask to go ahead
      G. Decision — ask to pause
    """
    system = (
        "You are an AI Chief of Staff drafting possible responses for the user "
        "to send to a WhatsApp group. The responses MUST sound like the user "
        "wrote them — match their tone, length, punctuation, emoji, and "
        "language habits from the persona profile. Do NOT sound like an AI "
        "assistant. Every option must move things forward — a nudge, a demand "
        "for update, a decision call, a recap, or a clarity ask. Never offer "
        "help ('happy to help', 'let me know if...'). Return ONLY the "
        "WhatsApp-formatted message."
    )

    persona_block = ""
    if persona_text:
        persona_block = f"""
=== USER PERSONA (match this voice exactly) ===
{persona_text[:8000]}

CRITICAL: Write each option as if the USER typed it. Match their:
- Message length (if they write 1-liners, keep it short)
- Punctuation (lowercase, no periods, ellipsis — whatever they do)
- Emoji usage (same frequency and type)
- Language mix (if they mix Hindi/English, do the same)
- Directness level
"""

    prompt = f"""TASK: {task_title}
SUMMARY: {task_summary}
SOURCE GROUP: {source_chat}
SOURCE JID: {source_jid}
{persona_block}

=== OKF CONTEXT ===
{okf_text[:20000]}

=== RECENT CHATS ===
{recent_chats[:10000]}

Based on the context, draft exactly 7 possible responses the user could send to
the "{source_chat}" group regarding this task. Each option serves a DIFFERENT intent:

⚠️ NO AMBIGUOUS REFERENCES — every message must be self-contained. Never use "this",
"that", "it", "these", "those" without specifying what you're referring to. Always
name the specific document, issue, deadline, person, or decision. The recipient
should understand the message without seeing the task title.

*FOLLOW-UP*
*A. Mild:* a soft, polite nudge for an update — grounded in the original ask, never a bare "any update?".
*B. Assertive:* a direct, context-rich push — cite the specific commitment/deadline and demand closure.

*CLARITY*
*C. Timelines:* ask for exact timelines / when specifically it will be done.
*D. Ownership:* clarify who has to close what, or ask about any missing details.

*RECAP*
*E. Full recap:* give the full context of the issue and summarize everything in one place so everyone is aligned (2-4 lines allowed).

*DECISION*
*F. Go ahead:* ask for a decision to proceed / go ahead.
*G. Pause:* ask for a decision to pause / hold.

⚠️ TONE RULES:
- Every option must move the task forward — no offering help, no "let me know if you need".
- Ground every nudge in the specifics of this task (cite the commitment/deadline/issue explicitly).
- Each option should be 1-2 lines, except E (Recap) which can be 2-4 lines.
- NO ambiguous pronouns (this/that/it/these/those) — always use explicit names.
- Write in the user's natural voice (see persona above).

Format EXACTLY like this:

🎯 *Task {task_number} — {task_title[:50]}*

━━━━━━━━━━━

*FOLLOW-UP*
*A.* [mild follow-up in user's voice]

*B.* [assertive follow-up with context]

*CLARITY*
*C.* [clarify timelines]

*D.* [clarify ownership / missing details]

*RECAP*
*E.* [recap + context in one place]

*DECISION*
*F.* [decision — go ahead]

*G.* [decision — pause]

━━━━━━━━━━━

Reply *send {task_number} A-G*
"""
    return _call(config.GEMINI_MODEL_FAST, prompt, system, temperature=0.4)


def parse_intent(user_text: str, has_task_list: bool) -> dict:
    """
    Parse free-text user input to determine intent.
    Returns dict: {action, task_numbers, task_number, message}

    Actions: archive, context, action, send, get_more, pull, unknown
    """
    system = (
        "You parse user intent from WhatsApp messages. Return ONLY valid JSON."
    )

    task_context = ""
    if has_task_list:
        task_context = "\nThe user has an active task list with numbered tasks 1-10+."

    prompt = f"""Parse the user's intent from this message:
"{user_text}"
{task_context}

Determine the action and extract relevant parameters.

    Actions:
      - "archive": user wants to archive/dismiss tasks (extract task_numbers as list of ints)
      - "context": user wants more context on a task (extract task_number as int)
      - "action": user wants action options for a task (extract task_number as int)
      - "send": user wants to send a drafted option to a group (extract task_number and option_letter A-G)
      - "confirm": user is confirming a pending send (message is "yes", "confirm", "send it", "go", "y", "ok send", etc.)
      - "get_more": user wants more tasks
      - "pull": user typed /pull
      - "unknown": cannot determine intent

Return JSON:
{{
  "action": "archive|context|action|send|confirm|get_more|pull|unknown",
  "task_numbers": [list of ints, for archive],
  "task_number": int or null,
  "option_letter": "A|B|C|D|E|F|G" or null,
  "message": "any free text the user added"
}}

ONLY return the JSON object.
"""
    try:
        data = _call_json(config.GEMINI_MODEL_FAST, prompt, system, temperature=0.0)
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    # Fallback: simple keyword matching
    lower = user_text.lower().strip()
    if lower == "/pull":
        return {"action": "pull", "task_numbers": [], "task_number": None,
                "option_letter": None, "message": ""}
    # Confirm a pending send
    if lower in ("yes", "y", "confirm", "send it", "go", "ok", "ok send",
                  "send", "yep", "yeah", "sure", "do it"):
        return {"action": "confirm", "task_numbers": [], "task_number": None,
                "option_letter": None, "message": ""}
    if "more" in lower:
        return {"action": "get_more", "task_numbers": [], "task_number": None,
                "option_letter": None, "message": ""}
    return {"action": "unknown", "task_numbers": [], "task_number": None,
            "option_letter": None, "message": user_text}
