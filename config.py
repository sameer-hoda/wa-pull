"""
Configuration for wa-pull bot.
All paths and tunables live here.
"""

import os
import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# ── Timezone ──────────────────────────────────────────────────────────────────
# All scheduling runs in IST.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# ── Databases (produced by the Go bridge) ────────────────────────────────────
MESSAGES_DB = os.getenv("MESSAGES_DB_PATH") or str(SCRIPT_DIR / "store" / "messages.db")
WHATSAPP_DB = os.getenv("WHATSAPP_DB_PATH") or str(SCRIPT_DIR / "store" / "whatsapp.db")

# ── Bridge HTTP API ──────────────────────────────────────────────────────────
BRIDGE_URL = os.getenv("WA_API_URL") or "http://localhost:8080"

# ── Owner / MeChat ───────────────────────────────────────────────────────────
# Auto-detected from whatsmeow_device at startup if not set.
OWNER_PHONE = os.getenv("OWNER_PHONE_NUMBER", "").strip().replace("+", "")

# ── LLM ──────────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Use fastest available model for all calls — critical for UX (sub-30s responses).
GEMINI_MODEL_FAST = os.getenv("GEMINI_MODEL_FAST", "gemini-3.1-flash-lite-preview")
GEMINI_MODEL_PRO = os.getenv("GEMINI_MODEL_PRO", "gemini-3.1-flash-lite-preview")

# ── OKF Bundle ───────────────────────────────────────────────────────────────
OKF_DIR = SCRIPT_DIR / "okf_bundle"

# ── Scheduler ────────────────────────────────────────────────────────────────
# An hourly session starts at the top of every hour, 8 AM–midnight IST.
# 10 minutes before the next hour (:50), the OKF is updated and an
# "hour wrap-up" message is sent with new context absorbed this hour.
IST_START_HOUR = int(os.getenv("IST_START_HOUR", "8"))   # 8 AM
IST_END_HOUR = int(os.getenv("IST_END_HOUR", "24"))      # midnight (inclusive)
WRAPUP_MINUTE = int(os.getenv("WRAPUP_MINUTE", "50"))    # 10 min before next hour

# Full OKF + persona rebuild happens once a day at the first session hour.
FULL_REBUILD_HOUR = IST_START_HOUR

# Lookback window for OKF (days)
OKF_LOOKBACK_DAYS = int(os.getenv("OKF_LOOKBACK_DAYS", "14"))

# ── Session ──────────────────────────────────────────────────────────────────
# Sessions are created automatically every hour; keep them alive for the
# full hour so the user can act on tasks until the next hourly session.
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_SECONDS", "3600"))

# ── Task extraction ──────────────────────────────────────────────────────────
# The hourly bulletin shows the top 10 tasks to check.
TASKS_PER_PAGE = int(os.getenv("TASKS_PER_PAGE", "20"))

# ── Polling ──────────────────────────────────────────────────────────────────
MECHAT_POLL_INTERVAL = 3  # seconds between polls

# ── Real-time monitor (pulse) ─────────────────────────────────────────────────
# Watches all non-archived chats for triggers (user sends a message, user gets
# tagged, someone asks the user a question, 4+ messages in quick succession)
# and generates proactive "pulse" alerts in MeChat with task + A/B/C options.
MONITOR_ENABLED = os.getenv("MONITOR_ENABLED", "1") != "0"
MONITOR_POLL_INTERVAL = int(os.getenv("MONITOR_POLL_INTERVAL", "10"))   # seconds
MONITOR_COOLDOWN_SECONDS = int(os.getenv("MONITOR_COOLDOWN_SECONDS", "300"))  # 5 min/chat
MONITOR_CONTEXT_MESSAGES = int(os.getenv("MONITOR_CONTEXT_MESSAGES", "30"))  # msgs to LLM

# ── Persistent state (survives restarts) ─────────────────────────────────────
STORE_DIR = SCRIPT_DIR / "store"
ARCHIVE_FILE = STORE_DIR / "archived_tasks.json"      # titles that stay archived
SESSION_LOG_FILE = STORE_DIR / "session_log.json"     # traceable session IDs
PERSONA_FILE = SCRIPT_DIR / "okf_bundle" / "persona.md"  # global + per-group voice
