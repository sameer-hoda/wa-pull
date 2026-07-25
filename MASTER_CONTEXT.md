# wa-pull — Master Context Document

**Last Updated**: 2026-07-25
**Version**: 4.0
**Location**: `wa-pull/`

---

## 1. What is wa-pull?

wa-pull is a WhatsApp-native AI task bot. The entire front-end is WhatsApp — after a one-time QR scan, the user communicates with the bot exclusively through their **MeChat** (the chat you have with yourself on WhatsApp).

The bot reads all non-archived WhatsApp group chats and DMs, uses Gemini to extract the top 10 pending follow-ups across ALL chats using a 7-step additive scoring rubric, and presents them as an interactive numbered task list in MeChat. **Sessions start automatically at the top of every hour (8 AM–midnight IST)** and include an "Hourly Bulletin" with the task list (two-line v4 format), what got done today (5 crisp bullets), and messages where the user left people on read (5 bullets). The user can archive tasks, get rich context, draft persona-aware response options (7 variants: A–G), and send messages to the exact source group with confirmation — all through free-text in MeChat.

Ten minutes before the next hour (:50), the OKF is updated for chats active this hour, and a wrap-up message summarizes the new context absorbed (≤ 60 words). `/pull` is still available as an on-demand manual trigger.

A **real-time monitor** (added v4.0) runs in a separate background thread, watching all non-archived chats (groups and DMs, including `@lid`-based chats) every 10s. When the user sends a message in any non-archived chat, or someone tags the user, the monitor loads that chat's context + OKF concept + persona, calls a lightweight LLM to classify the needed action, and — if actionable — sends a standalone alert to MeChat (`RT-XXXX` ID). The user can `reply <id>` (send a draft), `context <id>` (richer detail), or `archive <id>` (dismiss).

---

## 2. Product Requirements (as specified by Sameer Hoda)

### Core Functions (v3.0 → v4.0)

| # | Requirement | Implemented |
|---|------------|-------------|
| 1 | Identify user's own MeChat as primary communication channel | ✅ `db.get_mechat_chat_jid()` |
| 2 | Access only non-archived chats | ✅ `db.get_non_archived_chats()` |
| 3 | Hourly auto-session every hour 8 AM–10 PM IST (replaces manual /pull) | ✅ `scheduler.HourlyScheduler` |
| 4 | Hourly Bulletin: session ID, time IST, top 10 tasks, done-today (5 bullets), left-on-read (5 bullets) | ✅ `scheduler._build_bulletin()` + `llm.get_hourly_extras()` |
| 5 | Full OKF wiki + persona rebuild at first session of the day (8 AM) | ✅ `okf_builder.py` + `scheduler.py` |
| 6 | End-of-hour wrap-up at :50: update OKF, send ≤60 word new-context summary | ✅ `okf_builder.incremental_update()` + `llm.summarize_hourly_context()` |
| 7 | Session system — 60min inactivity timeout with unique traceable session ID | ✅ `session.py` + `session_log.py` |
| 8 | Session interactions: archive, context, action options (A–G), send to group, more tasks | ✅ `handlers.py` |
| 9 | Free-text input → AI intent parsing for task numbers + confirmation parsing | ✅ `llm.parse_intent()` |
| 10 | Send with confirmation: "send 5 A" → confirm prompt → "yes" → actually sends | ✅ `handlers._handle_send()` + `_handle_confirm_send()` |
| 11 | Session expiry notification with session ID + next hourly session info | ✅ `session.SessionManager` |
| 12 | Persistent archiving (tasks stay archived across sessions and restarts) | ✅ `archive_store.py` |
| 13 | Persona-aware action drafts (global voice + per-group voice), 7 variants A–G | ✅ `okf_builder.build_persona()` + `llm.get_action_options()` |
| 14 | Exact JID match for sending (from LLM-provided source_jid, no fuzzy matching) | ✅ `handlers.py` + `llm.py` JID map |
| 15 | Contact name resolution (raw LIDs/JIDs → human names) | ✅ `contact_resolution.py` |
| 16 | Progress messages during task generation | ✅ `handlers._handle_pull()` |
| 17 | Per-message error recovery (handler crashes don't kill the bot) | ✅ `main.py` try/except per message |

### Action Option Variants (v3.0)

| Option | Intent | Description |
|--------|--------|-------------|
| A | Followup (mild) | Soft, polite nudge grounded in original ask |
| B | Followup (assertive) | Direct, context-rich push citing specific commitment/deadline |
| C | Clarity (timelines) | Ask for exact timelines / when it will be done |
| D | Clarity (ownership) | Clarify who closes what / missing details |
| E | Recap | Full context + summary in one place |
| F | Decision (go ahead) | Ask to proceed / go ahead |
| G | Decision (pause) | Ask to pause / hold |

### Edge Cases Handled

- **No task list but session active**: Auto-generates task list
- **`/pull` during active session**: Warns user; an hourly session is already active
- **Session timeout (60min)**: Sends "Session ended" with next schedule info
- **Old `/pull` replays**: DB timestamp format matches exactly to prevent re-processing
- **Non-/pull messages without session**: Suggests waiting for next hourly or typing `/pull`
- **LLM returns >10 tasks**: Hard-truncated to TASKS_PER_PAGE
- **LLM API hangs**: 60s HTTP timeout — bot recovers automatically
- **Handler throws exception**: Caught per-message, bot continues running
- **Send without confirmation**: Stored in `session.pending_send`, any non-"yes" reply cancels
- **Missing source_jid**: Falls back to exact chat name match (no fuzzy, no guesswork)
- **No active chats this hour (wrap-up)**: "No new context absorbed this hour."
- **No done-today items early in the day**: "Nothing substantial yet today."

---

## 3. Architecture

```
┌─────────────────────┐          ┌──────────────────────┐         ┌─────────────────────┐
│  WhatsApp (phone)   │◄────────►│  Go Bridge            │◄────────►│  wa-pull bot        │
│                     │          │  (whatsmeow)          │  poll    │  (Python)           │
│  • MeChat (self)    │WebSocket │                       │  MeChat  │                     │
│  • Groups           │          │  • QR auth (terminal)  │  (3s)    │  • hourly bulletin  │
│  • DMs              │          │  • REST API :8080      │          │  • session mgmt     │
│                     │          │  • SQLite store/       │          │  • LLM extraction   │
└─────────────────────┘          └──────────────────────┘          │  • OKF + persona     │
                                                                    │  • wrap-up diffing   │
                                                                    │  • contact resolve   │
                                                                    │  • archive store     │
                                                                    └─────────────────────┘
```

### Component Roles

| Component | Language | Role |
|-----------|----------|------|
| Go Bridge | Go | WhatsApp WebSocket, QR auth, message storage, send API |
| wa-pull Bot | Python | MeChat polling, hourly session scheduling, session management, LLM integration, OKF + persona building, incremental OKF updates + diffing, contact resolution, persistent archiving |

### Data Flow

1. **Bridge starts** → QR on terminal → user scans via WhatsApp Linked Devices
2. **Bridge syncs** chat history to `store/messages.db` and `store/whatsapp.db`
3. **Bot starts** → detects own JID → starts hourly scheduler → polls MeChat every 3s for new messages
4. **Hourly (HH:00, 8 AM–10 PM IST)** → builds Hourly Bulletin (top 10 tasks, done-today, left-on-read) → creates session → sends to MeChat
5. **End of hour (HH:50)** → finds chats active this hour → incremental OKF rebuild → diffs old vs new → sends ≤60-word new-context wrap-up
6. **Message arrives** → `db.get_mechat_messages_since()` returns new messages
7. **Handler routes** → `/pull` creates on-demand session, free-text routes to intent parser
8. **LLM processes** → `gemini-3.1-flash-lite-preview` for all calls (60s timeout)
9. **Response sent** → `sender.send_to_mechat()` → `POST /api/send` → bridge → WhatsApp

---

## 4. File Map

| File | Purpose |
|------|---------|
| `main.py` | Entry point: startup, MeChat polling loop with per-message error recovery, signal handling |
| `config.py` | All paths, LLM models, IST timezone, hourly window (8–22), wrap-up minute, 10 tasks/page, 3600s session |
| `db.py` | SQLite queries: own JID, MeChat JID, non-archived chats, message fetching, recent-activity for done-today/left-on-read, active-chats for wrap-up |
| `sender.py` | WhatsApp send via bridge HTTP API with retry |
| `session.py` | Session + SessionManager: unique session_id, pending_send confirmation state, 60min timeout |
| `session_log.py` | Persistent log of all session IDs for traceability (JSON file) |
| `archive_store.py` | Persistent archive — tasks stay archived forever across sessions/restarts |
| `contact_resolution.py` | Resolve raw LIDs/JIDs from LLM output to human-readable contact names |
| `llm.py` | Gemini wrapper: 7-step task extraction (v4 rubric, 10 tasks, all-chat scope), OKF concepts, persona builders, context, hourly extras (done-today + left-on-read), hourly context summarization, 7-action-option drafting (A–G, persona-aware), intent parsing, bot-message filter, business-context filter |
| `okf_builder.py` | Full + incremental OKF bundle builds, persona.md (global + per-group voice), concept doc snapshot/diff helpers |
| `okf_reader.py` | Read/search OKF bundle, get recent chats text |
| `monitor.py` | Real-time monitor: watches all non-archived chats (incl. `@lid`) every 10s, triggers on user-sent-in-any-chat or user-tagged, lightweight LLM classifies action (draft/task/info/none), sends standalone `RT-XXXX` alerts to MeChat with reply/context/archive commands, per-chat 5-min cooldown, persisted alert store |
| `task_extractor.py` | Context loading (separate for extraction), task extraction with persistent archive filtering, post-extraction dedup safety filter, compact 1-line format + shared `format_task_lines` (v4 two-line) |
| `handlers.py` | Message handlers: pull, archive, context, action (persona-aware + contact resolved), send (confirmation flow + exact JID, A–G options), more |
| `scheduler.py` | `HourlyScheduler`: HH:00 sessions (8–24 IST), HH:50 wrap-ups (OKF update + new-context message), 8 AM full rebuild |
| `dump_prompts_and_chats.py` | Dev tool: dumps prompts + last 48h chats + OKF context + archived tasks to `prompt_lab.md` for offline algorithm testing |
| `tests/test_session.py` | Session unit tests (creation, archiving, expiry, pending_send) |
| `tests/test_handlers.py` | Handler formatting, option extraction (A–G), compact format tests |
| `tests/test_okf.py` | OKF builder/reader unit tests |
| `tests/test_monitor.py` | Real-time monitor unit tests (alert IDs, store, command regex, cooldown) |

---

## 5. Key Technical Decisions & Gotchas

### 5.1 MeChat JID (LID vs Phone JID)

**The Bridge stores messages using LID-based JIDs, not phone-based JIDs.**

- Phone JID: `919967151186@s.whatsapp.net`
- LID JID (what bridge actually uses as chat_jid): `219541632213229@lid`

The mapping is in `whatsapp.db`:
- `whatsmeow_device.lid` → `219541632213229:16@lid`
- `whatsmeow_lid_map` → maps LID ↔ phone number

**Fix**: `db.get_mechat_chat_jid()` resolves the LID from the device/lid_map tables. Both polling and sending use the LID JID.

### 5.2 Timestamp Format (Critical Bug — Fixed)

**The DB stores timestamps as `2026-07-23 14:55:48+05:30` (IST with timezone suffix).**

When comparing with `timestamp > ?` in SQLite (string comparison):
- `2026-07-23 15:16:26+05:30` > `2026-07-23 15:16:26` = **TRUE** (because `+` > nothing)
- This caused the same message to be returned EVERY poll cycle → infinite loop

**Fix**: `db.get_mechat_messages_since()` now formats `since` as `2026-07-23 15:16:26+05:30` to exactly match the DB format. Comparison works correctly now.

### 5.3 Bridge DBs Location

The Go bridge creates DBs in `./store/` relative to its working directory. The `start.sh` script runs from `wa-pull/`, so DBs are at `wa-pull/store/`. Config defaults reflect this.

### 5.4 Emoji Filter

The bot's own responses are stored in the DB (sent via bridge API) but filtered out by checking if the message starts with known bot emojis (`🤖`, `🔄`, `📋`, `✅`, `⚠️`, `📭`, `📦`, `📖`, `💡`, `🎯`, `📨`, `🤔`, `🕒`, `🧠`, `📥`, `☀️`, `🌅`, `👋`, `📤`, `⏰`, `🆔`, `⚡`, `📝`). This prevents the bot from processing its own responses (bulletin, wrap-up, real-time alerts) as user commands.

### 5.5 Persistent State Files

All in `store/`:
- `archived_tasks.json` — tasks archived across sessions (keyed by `title::source_chat`)
- `session_log.json` — traceable session ID history (newest first, capped at 500)
- `realtime_alerts.json` — active real-time monitor alerts (survive restarts)

### 5.6 Persona File

`okf_bundle/persona.md` — built at the first session of the day (8 AM) alongside the full OKF rebuild. Contains:
- **Global Voice**: tone, length, punctuation, emoji, language mix, sign-off habits
- **Per-Group Voice**: how the user writes in each major group (differences from global style)

Used by `llm.get_action_options()` to draft 7 response variants (A–G) in the user's natural voice.

### 5.7 IST Timezone

All scheduling runs in IST (`+05:30`). `config.IST` is a `datetime.timezone` object used by the scheduler and bulletin/wrap-up messages. The DB already stores timestamps in IST, so comparisons are consistent.

---

## 6. LLM Integration

### Models Used

| Model | Purpose | Temperature |
|-------|---------|-------------|
| `gemini-3.1-flash-lite-preview` | ALL calls — task extraction, OKF concepts, persona builders, context, hourly extras (done-today/left-on-read), hourly context wrap-up, action options (A–G), intent parsing | 0.2-0.4 |

HTTP timeout: **60 seconds** per call. If the API hangs, the bot logs an error and recovers.

### Task Extraction (v4 — 7-Step Rubric)

Task extraction uses a 7-step rubric with additive scoring in `llm.extract_tasks()`:

| Step | Name | Purpose |
|------|------|---------|
| 1 | Ignore | Filter bot output, fragments, forwarded articles, acks, archived titles |
| 2 | Find open loops | Unanswered questions, unfulfilled commitments, deadlocked decisions, at-risk metrics, personal/family items |
| 3 | Merge | One task per thread, not per message. Cross-group duplicates merged into one |
| 4 | Score (additive) | +40 blocked, +30 money/compliance/outage, +25 deadline ≤24h, +20 unanswered >4h, +15 overdue commitment, +15 deadlocked decision, +10 freshness, +10 red OKR metric, +10 personal time-bound, −20 stale >48h, −25 unchanged from last bulletin, −30 chatter |
| 5 | Write title | ≤80 chars, must contain proper noun or number, names a person/group, imperative verb |
| 6 | Return JSON | task_number, title, summary, ask, who_waiting, waiting_hours, deadline, evidence, state, is_new, source_chat, source_jid, score, score_reason, urgency |
| 7 | Self-check | No duplicate threads, no >80 char titles, no raw @IDs, ≤2 critical, ≥3 is_new=true |

Each task object now has:
- `task_number`, `title` (≤80 chars), `summary` (2-3 sentences), `ask` (≤15-word concrete next action)
- `who_waiting` (person blocked), `waiting_hours` (integer), `deadline` (verbatim or "")
- `evidence` (≤20-word quoted trigger), `state` (waiting_on_me | waiting_on_them | needs_decision | at_risk)
- `is_new` (true if not in previous bulletin), `score` (integer), `score_reason` (arithmetic)
- `urgency` (critical | high | medium | low), `source_chat`, `source_jid`

Tasks render in a two-line format:
```
*1.* 🔴⚡ *Break CM-page deadlock — Anoop wants a call, Manesh waiting 3h*
     _Offer visibility on CM page_  ∙  Anoop · 3h · by EOD
```

Previous bulletins' task titles are stored in `llm._last_bulletin_tasks` for hour-over-hour freshness (unchanged tasks get −25 score penalty).

### Hourly Extras

`llm.get_hourly_extras()` takes a timestamped, direction-flagged activity log (outgoing tagged `[ME]`) and produces:
- **done_today**: 5 crisp bullets of substantial things the user completed (decisions made, code/docs shipped, reports shared, approvals given, etc.). Excludes trivial acks, scheduling, "had a chat", etc.
- **left_on_read**: 5 crisp bullets of incoming questions/asks the user has NOT yet responded to (per chat, with who asked and when).

### Action Options (A–G)

Seven intent-based variants drafted in the user's voice:
| Option | Intent |
|--------|--------|
| A | Followup — mild |
| B | Followup — assertive with context |
| C | Clarity — clarify timelines |
| D | Clarity — ownership / missing details |
| E | Recap — full context + summary |
| F | Decision — go ahead |
| G | Decision — pause |

**Persona-aware**: Drafted in the user's voice (global + per-group).
**Contact resolved**: Raw LIDs in drafted text are replaced with human names.

### Hourly Context Wrap-Up

`llm.summarize_hourly_context()` takes old-vs-new concept doc diffs for chats active this hour and produces a ≤60-word WhatsApp message structured by group/sub-group, only calling out NEW or CHANGED context. Returns "No new context absorbed this hour." when nothing changed.

### Intent Parsing

Actions: `archive`, `context`, `action`, `send`, `confirm`, `get_more`, `pull`, `unknown`

`confirm` action: triggered by "yes", "y", "confirm", "send it", "go", "ok", "sure", etc. Used to confirm a pending send.

Fallback: simple keyword matching if LLM fails.

---

## 7. Session Lifecycle

```
Hourly (HH:00, 8 AM–midnight IST):
  8 AM: Full OKF + persona rebuild → Hourly Bulletin
  Other hours: Hourly Bulletin directly

Hourly Bulletin:
  🕒 Hourly Bulletin · HH:MM IST · Month DD
  🆔 Session <ID>
  📋 Top 10 Tasks to check (v4 two-line format: title + who_waiting · age · deadline)
  ✅ What got done today (5 bullets)
  📥 Left on read (5 bullets)
       │
       ├─ archive 1,3,5 → remove tasks (persisted to archive_store.json)
       │     └─ resets timer
       ├─ context 2 → read OKF, generate rich update
       │     └─ resets timer
       ├─ action 6 → draft A–G (persona-aware, contact-resolved)
       │     └─ resets timer
       ├─ send 6 A → confirm prompt:
       │     "📤 Confirm send — To: <group> (<JID>) — Task 6 · Option A"
       │     "<message text> — Reply yes to send"
       │     └─ yes → actually send to exact source JID
       │     └─ anything else → cancel
       │     └─ resets timer
       ├─ more → get more tasks (skips archived)
       │     └─ resets timer
       └─ 60min inactivity → session expires → "Session ended" message

Hour wrap-up (HH:50):
  🧠 Hour wrap-up · HH:50 IST
  <≤60 word new-context summary, by group/sub-group>
  ✅ Saved to OKF memory

  Or: "No new context absorbed this hour. ✅ OKF memory unchanged"

/ (next hourly session at the top of the next hour)
```

---

## 8. OKF (Open Knowledge Format) Integration

wa-pull generates an OKF v0.1 conformant bundle. Each non-archived WhatsApp chat becomes one concept document:

```
okf_bundle/
├── index.md              # Directory listing
├── log.md                # Update history
├── persona.md            # User communication style (global + per-group)
├── project_docs/         # Project-specific documentation
│   └── *.md
├── groups/               # One .md per group chat
│   └── *.md
└── contacts/             # One .md per DM
    └── *.md
```

Each concept has YAML frontmatter (`type`, `title`, `description`, `tags`, `timestamp`) and a markdown body with sections: Summary, Open Items, Key Decisions, Recent Activity, Participants.

The OKF is:
- **Fully rebuilt** at the first session of the day (8 AM), including persona.
- **Incrementally updated** at each hour's wrap-up (:50) — only chats that were active in the last hour are rebuilt.
- Used by context lookups, task extraction, action drafting, the hourly context wrap-up, and the real-time monitor (per-chat concept lookup).

---

## 8b. Real-time Monitor (v4.0)

A separate background thread (`monitor.py: RealTimeMonitor`) that watches **all non-archived chats** (not just MeChat) every 10s and generates proactive alerts.

### Triggers

| Trigger | Condition | Scope |
|---------|-----------|-------|
| `user_sent` | `is_from_me=True` in any non-archived chat (group or DM, `@g.us` / `@lid` / `@s.whatsapp.net`) | The user just engaged — monitor looks for follow-ups, replies owed, info needed |
| `user_tagged` | `is_from_me=False` AND content contains `@<own_lid>` | Someone tagged the user — monitor looks for the response they owe |

Bot's own output (task_dog scoreboards, Hourly Bulletins, `*📊 SCOREBOARD*`) is filtered via `llm.is_bot_message()` so the monitor never feedback-loops on itself. MeChat (`@lid`, excluded via `exclude_jid`) is skipped. The DB query in `get_new_messages_across_chats` now includes `@lid` JIDs alongside `@g.us` and `@s.whatsapp.net`.

### Per-chat Cooldown

After an alert fires for a chat, a 5-minute cooldown (`MONITOR_COOLDOWN_SECONDS`) blocks further alerts for that same chat. Cooldown is set **only when an alert actually fires** — if the LLM returns `none`, no cooldown, so the next message is still checked (respects "LLM on every trigger").

### LLM Classification (`llm.generate_realtime_action`)

For each trigger, the monitor loads:
- Last 30 messages from that chat (`db.get_chat_messages`)
- That chat's OKF concept (`okf_builder.read_concept_md`)
- The user's persona (`okf_bundle/persona.md`, for drafting in their voice)

Then calls `gemini-3.1-flash-lite-preview` to classify ONE action:

| Action | Meaning | `content` field |
|--------|---------|------------------|
| `draft_response` | Someone asked a question or a reply is owed | Draft reply in user's voice, 1-3 lines, self-contained |
| `create_task` | A follow-up/commitment/deadline/decision to track | Task title (≤80 chars) + one-line summary |
| `critical_info` | Important context the user needs now (moved metric, blocker) | The info, ≤30 words |
| `none` | Nothing actionable | (no alert sent) |

If `action_type == "none"`, no alert. Otherwise, an alert is created and sent to MeChat.

### Alert Format

```
⚡ *Real-time Alert* · HH:MM IST
📍 <chat name>
🆔 `RT-XXXX`

<emoji> *<type label>*
<summary>

<content>

━━━━━━━━━━━━
• reply RT-XXXX — send this draft    (draft_response only)
• context RT-XXXX — more context
• archive RT-XXXX — dismiss
```

### Alert Commands (standalone, no session needed)

| Command | Effect |
|---------|--------|
| `reply <id>` | For `draft_response` alerts: shows a confirm prompt → reply `yes` → sends the draft to the source chat's exact JID, then auto-archives the alert |
| `context <id>` | Calls `llm.get_context` with the alert as the "task" and sends richer OKF-backed context |
| `archive <id>` | Dismisses the alert (removed from active store, persisted as archived) |

The realtime command regex (`^(reply|context|archive)\s+RT-[A-F0-9]{4,6}$`) is checked at the top of `handlers.handle_message`, BEFORE session logic — so these commands work even without an active hourly session. The monitor's pending-send confirmation is checked before the session's pending-send to avoid cross-state confusion.

### Persistence

Active alerts are saved to `store/realtime_alerts.json` so they survive restarts. Archived alerts are dropped from the active store.

---

## 9. Running the Project

### Quick Start

```bash
cd wa-pull
cp .env.example .env   # Edit — add GEMINI_API_KEY
./start.sh             # Starts bridge + bot
```

### Commands (in WhatsApp MeChat)

| Command | Result |
|---------|--------|
| Hourly (auto) | Hourly Bulletin every hour 8 AM–midnight IST |
| `/pull` | On-demand top 10 tasks |
| `/hourlyb` | Full hourly bulletin on demand |
| `archive 1, 3` | Dismiss tasks (persistent) |
| `context 2` | Deep dive from OKF |
| `action 6` | Draft A–G responses (persona-aware) |
| `send 6 A` | Confirm prompt → reply `yes` to send |
| `more` | More tasks |
| `reply RT-XXXX` | Send a real-time alert's draft to its source chat |
| `context RT-XXXX` | Get richer context on a real-time alert |
| `archive RT-XXXX` | Dismiss a real-time alert |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GEMINI_API_KEY` | — | Required |
| `GEMINI_MODEL_FAST` | `gemini-3.1-flash-lite-preview` | Model for all LLM calls |
| `OWNER_PHONE_NUMBER` | auto | Auto-detected from bridge |
| `MESSAGES_DB_PATH` | `store/messages.db` | DB path override |
| `WHATSAPP_DB_PATH` | `store/whatsapp.db` | DB path override |
| `WA_API_URL` | `http://localhost:8080` | Bridge HTTP API |
| `IST_START_HOUR` | 8 | First hourly session (IST) |
| `IST_END_HOUR` | 22 | Last hourly session (IST, inclusive; default .env.example = 22, default code = 24) |
| `WRAPUP_MINUTE` | 50 | Minute for end-of-hour wrap-up |
| `SESSION_TIMEOUT_SECONDS` | 3600 | Session inactivity timeout (60 min) |
| `TASKS_PER_PAGE` | 10 | Tasks per bulletin |
| `OKF_LOOKBACK_DAYS` | 14 | Days of history for OKF |
| `MONITOR_ENABLED` | 1 | Real-time monitor on/off (`1`/`0`) |
| `MONITOR_POLL_INTERVAL` | 10 | Seconds between scans of all chats |
| `MONITOR_COOLDOWN_SECONDS` | 300 | Per-chat cooldown after an alert fires |
| `MONITOR_CONTEXT_MESSAGES` | 30 | Recent msgs the monitor LLM sees per trigger |

---

## 10. Known Issues & TODOs

1. **OKF full build is slow** — 100+ chats × LLM calls = minutes. Runs only at 8 AM. Incremental updates at :50 handle only active chats.
2. **Single user only** — Owner protection is hard-coded to one device. Multi-user would need session-per-user.
3. **Persona build is slow** — 25 chats × 2 LLM calls each = 50 LLM calls. Daily-only at 8 AM. Not rebuilt hourly.
4. **7-step task extraction prompt is large** — The v4 rubric prompt is ~320 lines. This increases latency but dramatically improves task quality.
5. **Post-extraction dedup** — `task_extractor.extract_tasks_from_context()` applies substring + word-overlap dedup after LLM returns. Belt-and-braces safety filter. Occasionally too aggressive on very similar but distinct tasks.
6. **LID-based chat JIDs** — The bridge stores some chats as `@lid` JIDs (not just `@g.us`/`@s.whatsapp.net`). The monitor's `get_new_messages_across_chats` includes `@lid`, but `get_non_archived_chats` does not (by design — avoids adding 413 LID chats to the OKF build). The monitor handles `@lid` DMs correctly; LID-based groups are rare but would be treated as contacts by `concept_path_for`.

---

## 11. Key Debugging Notes

- Bridge not responding: `lsof -i :8080`, `curl localhost:8080/api/send`
- Messages not appearing: Check `sqlite3 store/messages.db` for correct `chat_jid` (LID vs phone)
- Polling loop stuck: Check timestamp format in DB vs `since` comparison. LLM timeout is 60s.
- Bridge DBs empty: Bridge needs history sync after pairing — wait 60s
- LLM calls failing: Verify model name `gemini-3.1-flash-lite-preview` is available from your API key
- Session IDs: Check `store/session_log.json` for traceability
- Archived tasks: Check `store/archived_tasks.json` for persistent archive state
- Contact resolution: Test with `python3 -c "import contact_resolution; print(contact_resolution.resolve_contact('<LID>'))"`
- Hourly not firing: Check `config.IST` timezone and `IST_START_HOUR`/`IST_END_HOUR` vs current IST time
- Wrap-up not sending: Check `db.get_chats_active_since()` returns chats that were active in the last hour
