#!/usr/bin/env python3
"""
wa-pull — Main entry point.

A WhatsApp bot that runs entirely through the user's MeChat (chat with themselves).
After the Go bridge is running and paired via QR scan, this bot:
  1. Polls the MeChat for new messages
  2. Routes /pull and session interactions
  3. Runs a daily OKF rebuild + auto /pull at 8 AM

Usage:
  python3 main.py

Prerequisites:
  - The Go bridge (wa-slash-commands/bridge) must be running and paired
  - .env must have GEMINI_API_KEY and OWNER_PHONE_NUMBER
"""

import os
import sys
import time
import datetime
import signal
from pathlib import Path

# Add this directory to path
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

# Load .env
from dotenv import load_dotenv
load_dotenv(SCRIPT_DIR / ".env")

import config
import db
import sender
import handlers
import okf_reader
from scheduler import scheduler
from monitor import monitor as realtime_monitor


def check_prerequisites():
    """Verify DBs exist and bridge is reachable. Wait up to 60s if not ready."""
    for attempt in range(60):
        msgs_ok = os.path.exists(config.MESSAGES_DB) and os.path.getsize(config.MESSAGES_DB) > 0
        wa_ok = os.path.exists(config.WHATSAPP_DB) and os.path.getsize(config.WHATSAPP_DB) > 0
        if msgs_ok and wa_ok:
            break
        if attempt == 0:
            print("⏳ Waiting for bridge to write DBs...")
        time.sleep(1)
    else:
        print("❌ DBs not ready after 60s. Is the bridge running?")
        print(f"  {config.MESSAGES_DB} — {'FOUND' if os.path.exists(config.MESSAGES_DB) else 'MISSING'}")
        print(f"  {config.WHATSAPP_DB} — {'FOUND' if os.path.exists(config.WHATSAPP_DB) else 'MISSING'}")
        print("Run: cd ../wa-slash-commands/bridge && ./wa-bridge")
        sys.exit(1)

    if not config.GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY not set in .env")
        sys.exit(1)


def print_banner(own_jid: str, own_phone: str):
    """Print startup banner."""
    print()
    print("=" * 60)
    print("  hourlyB — WhatsApp Task Bot")
    print("=" * 60)
    print(f"  Owner JID:   {own_jid}")
    print(f"  Owner Phone: {own_phone}")
    print(f"  MeChat:      {own_jid} (chat with yourself)")
    print(f"  Bridge:      {config.BRIDGE_URL}")
    print(f"  OKF Dir:     {config.OKF_DIR}")
    print(f"  Sessions:    Hourly {config.IST_START_HOUR:02d}:00–23:00 IST")
    print(f"  Wrap-up:     :{config.WRAPUP_MINUTE:02d} of each active hour")
    print(f"  Session:     {config.SESSION_TIMEOUT_SECONDS}s timeout")
    print(f"  Monitor:     {'ON' if config.MONITOR_ENABLED else 'OFF'} · "
          f"poll {config.MONITOR_POLL_INTERVAL}s · "
          f"cooldown {config.MONITOR_COOLDOWN_SECONDS}s/chat")
    print("=" * 60)
    print()
    print("Bot is running. Hourly bulletins are sent automatically every hour.")
    print("You can also type /pull from your WhatsApp MeChat for an on-demand one.")
    print("Press Ctrl+C to stop.")
    print()


def main():
    check_prerequisites()

    # Detect own JID
    try:
        own_jid = db.get_own_jid()
        own_phone = db.get_own_phone()
    except Exception as e:
        print(f"❌ Could not detect own JID: {e}")
        print("Is the bridge paired? Run the Go bridge and scan QR first.")
        sys.exit(1)

    print_banner(own_jid, own_phone)

    # Skip OKF + persona build on startup — too slow with 100+ chats.
    # The /pull command works directly from chat data without OKF.
    # OKF + persona will be built on the daily schedule (8 AM by default).

    # Send a welcome message to MeChat
    sender.send_to_mechat(
        "🤖 *hourlyB is online!*\n\n"
        f"Hourly bulletins auto-start every hour, "
        f"{config.IST_START_HOUR:02d}:00–23:00 IST.\n"
        "Type */pull* to generate one on demand.\n"
        "_hourly bulletins · free text interaction · self learning_"
    )

    # Start the daily scheduler
    scheduler.start()

    # Start the real-time monitor (watches all non-archived chats for triggers)
    realtime_monitor.start()

    # ── MeChat polling loop ──────────────────────────────────────────────
    # Start polling from 30s ago so we catch messages sent during boot
    last_seen_time = datetime.datetime.now(datetime.timezone.utc)
    print(f"[poll] Started polling MeChat every {config.MECHAT_POLL_INTERVAL}s...")

    running = [True]

    def handle_sigint(sig, frame):
        print("\n[bot] Shutting down...")
        running[0] = False
        scheduler.stop()
        realtime_monitor.stop()
        sender.send_to_mechat("🤖 _hourlyB is going offline._")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    while running[0]:
        try:
            # Get new MeChat messages since last check
            new_messages = db.get_mechat_messages_since(since=last_seen_time)

            if new_messages:
                # Update last_seen to the latest message time
                last_seen_time = new_messages[-1]["time"]

                for msg in new_messages:
                    # Only process messages FROM the user (is_from_me=True)
                    # The bridge stores outgoing messages too, so we need to
                    # only react to messages the user typed in their MeChat.
                    if not msg["is_from_me"]:
                        continue

                    content = msg["content"]
                    # Skip messages sent by the bot itself (they start with emojis we use
                    # or with "pulse @" for real-time monitor alerts)
                    if content.startswith(("🤖", "🔄", "📋", "✅", "⚠️", "📭",
                                          "📦", "📖", "💡", "🎯", "📨", "🤔",
                                          "☀️", "🌅", "👋", "📤", "⏰", "🆔",
                                          "🕒", "🧠", "📥", "⚡", "📝")):
                        continue
                    if content.lower().startswith("pulse @"):
                        continue

                    print(f"[mechat] {msg['time'].strftime('%H:%M:%S')}: {content[:80]}")
                    try:
                        handlers.handle_message(content)
                    except Exception as e:
                        print(f"[mechat] Handler error: {e}")
                        sender.send_to_mechat(f"❌ Error processing message: {e}")

        except Exception as e:
            print(f"[poll] Error: {e}")

        time.sleep(config.MECHAT_POLL_INTERVAL)


if __name__ == "__main__":
    main()
