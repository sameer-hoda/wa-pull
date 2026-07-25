---
type: Project
title: wa-pull — WhatsApp Task Bot v4.0
description: WhatsApp-native AI task bot with hourly auto-sessions, A–G action options, 7-step task extraction rubric, end-of-hour OKF updates, and persona-aware drafting
tags: [wa-pull, whatsapp, ai, tasks, productivity, llm, gemini, okf]
timestamp: 2026-07-25T00:00:00Z
---

# Summary

wa-pull is a WhatsApp-native AI task bot running in `attempt_v5/wa-pull/`. The entire front-end is WhatsApp — after a one-time QR scan, the user communicates with the bot exclusively through their **MeChat** (chat with yourself on WhatsApp).

The bot reads all **non-archived** WhatsApp group chats and DMs via a Go bridge (whatsmeow), uses `gemini-3.1-flash-lite-preview` for all AI calls, and runs **hourly auto-sessions (8 AM–midnight IST)** — no manual `/pull` required. Each hour delivers an "Hourly Bulletin" with a 7-step scored task list, what got done today, and left-on-read. At :50, an incremental OKF update plus wrap-up message summarizes new context absorbed.

## Key Features (v4.0)

- **Hourly Bulletin** (HH:00) — session ID, time IST, top 10 tasks (v4 two-line format: title + meta line with who_waiting · waiting_hours · deadline), done-today (5 bullets), left-on-read (5 bullets)
- **End-of-hour wrap-up** (HH:50) — OKF incrementally updated for active chats, ≤60-word new-context summary per group/sub-group
- **Full OKF + persona rebuild** at first session of the day (8 AM) — ~100+ chats, slow but daily-only
- **Incremental OKF updates** at each wrap-up — only chats with new messages rebuilt, diffs computed from snapshots
- **7 intent-based action variants (A–G)**: followup-mild, followup-assertive, clarity-timelines, clarity-ownership, recap, decision-go, decision-pause
- **60-minute session** with unique traceable session ID (`session_log.json`, capped at 500 entries)
- **Persistent archiving** — tasks archived via `archive_store.py` stay archived forever across sessions and restarts
- **Persona-aware drafts** — action options written in the user's actual voice (global + per-group persona, rebuilt daily at 8 AM)
- **Send confirmation** — "send 5 A" shows confirm prompt with exact group JID, user replies "yes" to send
- **Exact JID match** — LLM receives chat name→JID map, sends use exact `source_jid`, no fuzzy matching
- **Contact resolution** — raw LIDs in LLM output are resolved to human names from `whatsapp.db` (`whatsmeow_contacts` and `whatsmeow_lid_map`)
- **Per-message error recovery** — handler crashes handled per-message, bot never dies on a bad LLM response
- **60s LLM HTTP timeout** — API hangs don't block the polling loop indefinitely

## Real-time Monitor (v4.0)

A separate background thread (`monitor.py: RealTimeMonitor`) watches all non-archived chats every 10s and generates proactive alerts in MeChat:

- **Trigger 1 (`user_sent`)**: User sends a message in any non-archived chat (group or DM, `@g.us` / `@lid` / `@s.whatsapp.net`) → monitor looks for what they should do next (a reply they owe, a follow-up they committed to, info they need)
- **Trigger 2 (`user_tagged`)**: Someone tags the user (`@<own_lid>`) in any non-archived chat → monitor looks for the response they owe

For each trigger the monitor loads that chat's recent context (30 msgs) + OKF concept + persona, then calls `llm.generate_realtime_action` (lightweight LLM) to classify ONE action:

| Action | `content` | Alert? |
|--------|-----------|--------|
| `draft_response` | Draft reply in user's voice (1-3 lines) | ✅ Yes |
| `create_task` | Task title (≤80 chars) + 1-line summary | ✅ Yes |
| `critical_info` | Info (≤30 words) | ✅ Yes |
| `none` | — | ❌ No alert |

- **Per-chat 5-min cooldown** after an alert fires (`MONITOR_COOLDOWN_SECONDS`). "none" results set no cooldown (respects "LLM on every trigger").
- **Standalone alerts** with IDs like `RT-A3F2`, NOT part of the hourly session task list.
- **Bot output filtered** via `llm.is_bot_message()` (no feedback loops). MeChat (`@lid`) excluded from scan.
- **Persistent** alert store at `store/realtime_alerts.json`.

### Alert commands (work without an active session)

| Command | Effect |
|---------|--------|
| `reply RT-XXXX` | For drafts: confirm prompt → `yes` → send to source chat's exact JID → auto-archive |
| `context RT-XXXX` | Richer OKF-backed context on the alert |
| `archive RT-XXXX` | Dismiss the alert |

The realtime command regex (`^(reply|context|archive)\s+RT-[A-F0-9]{4,6}$`) is checked at the top of `handle_message`, before session logic. Monitor pending-send confirmation is checked before the session's to avoid cross-state confusion.

## Task Extraction (v4 — 7-Step Additive Scoring Rubric)

Task extraction uses a 7-step rubric in `llm.extract_tasks()`:

| Step | Name | Purpose |
|------|------|---------|
| 1 | Ignore | Filter bot output, fragments, forwarded articles, acks, archived titles |
| 2 | Find open loops | Unanswered questions, unfulfilled commitments, deadlocked decisions, at-risk metrics, personal/family items |
| 3 | Merge | One task per thread, not per message. Cross-group duplicates merged into one |
| 4 | Score (additive) | +40 blocked, +30 money/compliance/outage, +25 deadline ≤24h, +20 unanswered >4h, +15 overdue commitment, +15 deadlocked decision, +10 freshness, +10 red OKR metric, +10 personal time-bound, −20 stale >48h, −25 unchanged from last bulletin, −30 chatter |
| 5 | Write title | ≤80 chars, must contain proper noun or number, names a person/group, imperative verb |
| 6 | Return JSON | task_number, title, summary, ask, who_waiting, waiting_hours, deadline, evidence, state, is_new, source_chat, source_jid, score, score_reason, urgency |
| 7 | Self-check | No duplicate threads, no >80 char titles, no raw @IDs, ≤2 critical, ≥3 is_new=true |

Each task object has:
- `task_number`, `title` (≤80 chars), `summary` (2-3 sentences), `ask` (≤15-word concrete next action)
- `who_waiting` (person blocked), `waiting_hours` (integer), `deadline` (verbatim or "")
- `evidence` (≤20-word quoted trigger), `state` (waiting_on_me/waiting_on_them/needs_decision/at_risk)
- `is_new` (true if not in previous bulletin), `score` (integer), `score_reason` (arithmetic)
- `urgency` (critical/high/medium/low), `source_chat`, `source_jid`

### Two-line Rendering Format

```
*1.* 🔴⚡ *Break CM-page deadlock — Anoop wants a call, Manesh waiting 3h*
     _Offer visibility on CM page_  ∙  Anoop · 3h · by EOD
```

## Architecture

```
WhatsApp (phone) ←→ Go Bridge (whatsmeow) ←→ wa-pull Bot (Python)
                     · QR auth (terminal)      · hourly scheduler (8 AM–midnight IST)
                     · REST API :8080          · session mgmt (60min expiry)
                     · SQLite store/           · 7-step task extraction (v4 rubric)
                                               · OKF builder (full + incremental)
                                               · Persona builder (global + per-group)
                                               · Contact resolution (LID→name)
                                               · Persistent archive store
```

## Components

| Component | Language | Role |
|-----------|----------|------|
| Go Bridge | Go | WhatsApp WebSocket, QR auth, message storage, send API |
| wa-pull Bot | Python | MeChat polling (3s), hourly scheduling, session management, LLM integration, OKF + persona builder, incremental OKF diffing, contact resolution, persistent archiving |

## File Map

| File | Purpose |
|------|---------|
| `main.py` | Entry point: MeChat polling loop (3s), per-message error recovery, SIGINT handler, emoji-prefix bot-message filter (🤖🔄📋✅⚠️📭📦📖💡🎯📨🤔☀️🌅👋📤⏰🆔🕒🧠📥⚡📝) |
| `config.py` | IST timezone (+05:30), LLM models, hourly window (8–24 IST), :50 wrap-up, 10 tasks/page, 3600s session, 14d OKF lookback, persistent state paths |
| `db.py` | SQLite queries: own JID (LID resolution via whatsmeow_lid_map), MeChat JID, non-archived chats, message fetching, recent-activity (for done-today/left-on-read), active-chats (for wrap-up), IST-timestamp-aware comparison |
| `sender.py` | WhatsApp send via bridge POST `/api/send` with 3-retry, `send_to_mechat()` helper |
| `session.py` | `Session` + `SessionManager`: unique 12-char hex session_id, pending_send confirmation state, archived_titles tracking, 60min inactivity expiry with auto-notification |
| `session_log.py` | Persistent session ID log (JSON, newest-first, capped at 500) for traceability |
| `archive_store.py` | Persistent archive keyed by `title::source_chat` (JSON), survives sessions and restarts |
| `contact_resolution.py` | `ContactResolver`: LID/JID → human name via whatsmeow_contacts + whatsmeow_lid_map, cached, with `resolve_text()` for bulk LID replacement |
| `llm.py` | Gemini wrapper: 7-step task extraction (v4 rubric), OKF concepts, persona builders (global + per-group), context, hourly extras (done-today + left-on-read), hourly context wrap-up, 7-option action drafts (A–G), intent parsing, bot-message filter, business-context builder |
| `okf_builder.py` | Full + incremental OKF bundle builder, persona.md (global + per-group voice), `concept_path_for()`, `read_concept_md()`, `incremental_update()` with per-chat diffing |
| `okf_reader.py` | Read/search OKF bundle, `get_recent_chats_text()`, `read_concept()` by fuzzy chat name |
| `monitor.py` | Real-time monitor: daemon thread polls all non-archived chats (incl. `@lid`) every 10s, triggers on user-sent-in-any-chat / user-tagged, lightweight LLM classifies action (draft/task/info/none), sends standalone `RT-XXXX` alerts to MeChat, 5-min per-chat cooldown, persisted alert store, reply/context/archive command handling |
| `task_extractor.py` | Context loading (`load_context()`), task extraction with persistent archive filtering, `get_more_tasks()`, `format_task_lines()` (v4 two-line), `format_task_list()` |
| `handlers.py` | All message handlers: pull (with progress at 50%/90%), hourlyb (full bulletin), archive, context, action (A–G persona-aware + contact-resolved), send+confirm (exact JID), more, no-session handler |
| `scheduler.py` | `HourlyScheduler`: HH:00 sessions (8–24 IST), HH:50 wrap-ups (OKF update + new-context message), 8 AM full rebuild (OKF + persona), background thread with 20s check interval |
| `dump_prompts_and_chats.py` | Dev tool: dumps all prompts + last 48h chat data + OKF context + archived tasks into `prompt_lab.md` for offline algorithm testing |
| `requirements.txt` | Dependencies: google-genai≥1.0.0, python-dotenv≥1.0.0, requests≥2.31.0 |
| `start.sh` | One-command stack launcher: venv setup, Go bridge start (with QR-wait), Python bot start (.bot.pid) |
| `MASTER_CONTEXT.md` | Human-readable comprehensive project documentation (architecture, data flow, gotchas, debug guide) |
| `tests/test_handlers.py` | Unit tests: task formatting, option extraction (A–G), compact format |
| `tests/test_session.py` | Unit tests: session creation, archiving, expiry, touch, pending_send |
| `tests/test_okf.py` | Unit tests: filename sanitization, slug uniqueness, fallback concept, index/log generation, reader operations |
| `tests/test_monitor.py` | Unit tests: alert ID format/uniqueness, alert store (create/get/archive), pending-send lifecycle, command regex, cooldown |

## Session Lifecycle

```
HH:00 (8 AM–midnight IST)
  → Full OKF + persona rebuild (8 AM only)
  → Hourly Bulletin (session ID, time IST, top 10 tasks in v4 two-line format)
  → ✅ What got done today (5 bullets, past-tense action verbs, ≤12 words)
  → 📥 Left on read (5 bullets, format: "<chat> — <what was asked> (<who>, <time>)")
       ├─ archive N,M  → remove (persistent via archive_store.py)
       ├─ context N     → rich OKF context (LLM-powered)
       ├─ action N      → A–G drafts (persona-aware, contact-resolved, no ambiguous pronouns)
       ├─ send N A      → confirm prompt with exact JID → reply "yes" → send
       ├─ more          → more tasks (skips archived + current, previous-titles dedup)
       └─ 60min inactivity → "Session ended" with next-hour info

HH:50
  → Incremental OKF rebuild for chats active this hour (via get_chats_active_since)
  → Diffs between old and new concept docs
  → ≤60-word new-context wrap-up by group/sub-group (via summarize_hourly_context)
  → "✅ Saved to OKF memory"
  or
  → "No new context absorbed this hour. ✅ OKF memory unchanged"
```

## Action Option Variants (A–G)

| Option | Intent | Description |
|--------|--------|-------------|
| A | Followup — mild | Soft, polite nudge grounded in original ask. Never a bare "any update?" |
| B | Followup — assertive | Direct, context-rich push citing specific commitment/deadline. Demands closure |
| C | Clarity — timelines | Ask for exact timelines / when specifically it will be done |
| D | Clarity — ownership | Clarify who closes what, or ask about missing details |
| E | Recap | Full context + summary in one place (2-4 lines). Gets everyone aligned |
| F | Decision — go ahead | Ask for a decision to proceed / go ahead |
| G | Decision — pause | Ask for a decision to pause / hold |

All options are drafted in the user's voice (global + per-group persona) with no ambiguous pronouns (this/that/it/these/those). Raw LIDs are resolved to contact names.

## Data Flow

1. **Bridge starts** → QR on terminal → user scans via WhatsApp Linked Devices
2. **Bridge syncs** chat history to `store/messages.db` and `store/whatsapp.db`
3. **Bot starts** → detects own JID (LID-based) → starts hourly scheduler → polls MeChat every 3s for new messages
4. **Hourly (HH:00, 8 AM–midnight IST)** → [8 AM: full OKF + persona rebuild] → loads context → extracts top 10 tasks (v4 rubric) → builds hourly extras (done-today + left-on-read) → creates session → sends bulletin to MeChat
5. **End of hour (HH:50)** → finds chats active this hour → incremental OKF rebuild → diffs old vs new concept docs → sends ≤60-word new-context wrap-up
6. **Message arrives** → `db.get_mechat_messages_since()` returns new messages (timestamp-format-aware)
7. **Handler routes** → free-text → `llm.parse_intent()` → archive/context/action/send/get_more/pull/unknown
8. **LLM processes** → `gemini-3.1-flash-lite-preview` for all calls (temperature 0.0–0.4, 60s timeout)
9. **Response sent** → `sender.send_to_mechat()` → `POST /api/send` → bridge → WhatsApp

## Key Technical Gotchas

1. **MeChat JID (LID vs Phone)**: Bridge stores messages using LID-based JIDs (`219541632213229@lid`), not phone JIDs (`919967151186@s.whatsapp.net`). `db.get_mechat_chat_jid()` resolves from `whatsmeow_lid_map` → `whatsmeow_device.lid`. Both polling and sending must use the LID JID.

2. **Timestamp Format**: DB stores `2026-07-23 14:55:48+05:30` (IST with timezone). SQLite string `>` comparison requires exact format match in `get_mechat_messages_since()`. Without the `+05:30` suffix, messages were re-processed in an infinite loop.

3. **Emoji Bot-Message Filter**: The bot checks if incoming messages start with 22 known bot emojis (🤖🔄📋✅⚠️📭📦📖💡🎯📨🤔☀️🌅👋📤⏰🆔🕒🧠📥⚡📝) to skip its own prior output — prevents feedback loops on bulletins, wrap-ups, and real-time alerts.

4. **IST Scheduling**: All scheduling uses `config.IST` (`datetime.timezone(timedelta(hours=5, minutes=30))`), independent of server local time. The scheduler checks every 20s using `datetime.now(config.IST)`.

5. **Incremental OKF Diffing**: `okf_builder.incremental_update()` snapshots current concept docs before rebuild, then compares post-rebuild to detect actual content changes. Only chats with diffs get included in the hourly context wrap-up.

6. **LLM Task Truncation**: Despite prompt specifying exactly 10, `task_extractor.extract_tasks_from_context()` applies a post-extraction safety filter: removes tasks matching archived titles or current-page titles (substring and high word-overlap matching).

7. **Hour-over-Hour Freshness**: `llm.extract_tasks()` receives `previous_titles` from the last bulletin via module-level `_last_bulletin_tasks`. Tasks unchanged get a −25 score penalty. New tasks get `is_new: true` and a ⚡ flag in the UI.

8. **Bot Output Filter in LLM**: `llm.filter_bot_messages()` strips lines matching `BOT_PREFIXES` (task_dog, Hourly Bulletin, SCOREBOARD, MOVED SINCE LAST UPDATE) from the activity log before passing to `get_hourly_extras()` — prevents the bot from treating its own scoreboard posts as user accomplishments.

9. **Business Context Filter**: `llm.build_business_context()` checks if the OKF text is mostly the bot's own project docs (signal words: wa-pull, Go Bridge, whatsmeow, etc.). If so, returns "(no business context available)" to avoid confusing the LLM.

## Running

```bash
cd wa-pull && cp .env.example .env   # Edit — add GEMINI_API_KEY
./start.sh                           # Starts bridge + bot
# Bot auto-sends hourly bulletins to your MeChat via WhatsApp
# Type /pull in MeChat for an on-demand bulletin
# Type /hourlyb for a full hourly bulletin on demand
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | **Required** |
| `GEMINI_MODEL_FAST` | `gemini-3.1-flash-lite-preview` | Model for all LLM calls |
| `GEMINI_MODEL_PRO` | `gemini-3.1-flash-lite-preview` | Model for context/action calls |
| `OWNER_PHONE_NUMBER` | auto | Auto-detected from bridge DB |
| `MESSAGES_DB_PATH` | `store/messages.db` | Messages DB override |
| `WHATSAPP_DB_PATH` | `store/whatsapp.db` | WhatsApp DB override |
| `WA_API_URL` | `http://localhost:8080` | Bridge HTTP API |
| `IST_START_HOUR` | 8 | First hourly session (IST) |
| `IST_END_HOUR` | 24 | Last hourly session (IST, inclusive) |
| `WRAPUP_MINUTE` | 50 | Minute for end-of-hour wrap-up |
| `SESSION_TIMEOUT_SECONDS` | 3600 | Session inactivity timeout (60 min) |
| `TASKS_PER_PAGE` | 10 | Tasks per bulletin |
| `OKF_LOOKBACK_DAYS` | 14 | Days of history for OKF |
| `MONITOR_ENABLED` | 1 | Real-time monitor on/off |
| `MONITOR_POLL_INTERVAL` | 10 | Seconds between all-chats scans |
| `MONITOR_COOLDOWN_SECONDS` | 300 | Per-chat cooldown after an alert fires |
| `MONITOR_CONTEXT_MESSAGES` | 30 | Recent msgs the monitor LLM sees per trigger |

## Persistent State

| File | Format | Purpose |
|------|--------|---------|
| `store/archived_tasks.json` | JSON, keyed by `title::source_chat` | Tasks archived forever across sessions/restarts |
| `store/session_log.json` | JSON, newest-first, capped 500 | Traceable session ID history |
| `store/realtime_alerts.json` | JSON, list of active alerts | Real-time monitor alerts (survive restarts) |
| `okf_bundle/persona.md` | Markdown | Global + per-group user communication style (rebuilt daily at 8 AM) |
| `okf_bundle/` | OKF v0.1 bundle | Full knowledge base: index.md, log.md, groups/*.md, contacts/*.md |

## Quick Debug

- Bridge not responding: `lsof -i :8080`
- Messages not appearing: check `chat_jid` in `sqlite3 store/messages.db` (LID vs phone)
- Polling loop stuck: check timestamp format in DB vs `since` comparison; LLM timeout is 60s
- Bridge DBs empty: bridge needs history sync after pairing — wait 60s
- LLM calls failing: verify model `gemini-3.1-flash-lite-preview` is available from API key
- Bot PID: `cat .bot.pid`, kill with `kill $(cat .bot.pid)`
- Session IDs: `cat store/session_log.json | python3 -m json.tool`
- Archived tasks: `cat store/archived_tasks.json | python3 -m json.tool`
- Contact resolution: `python3 -c "import contact_resolution; print(contact_resolution.resolve_contact('<LID>'))"`
- Hourly not firing: check `config.IST` timezone and `IST_START_HOUR`/`IST_END_HOUR` vs current IST time
- Wrap-up not sending: check `db.get_chats_active_since()` returns chats active in the last hour
- Dump context for testing: `python3 dump_prompts_and_chats.py` → `prompt_lab.md`
- Run tests: `python3 -m pytest tests/ -v`
