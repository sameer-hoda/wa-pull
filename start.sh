#!/bin/bash
# start.sh — start the full wa-pull stack (bridge + bot)
#
# One command to rule them all:
#   ./start.sh
#
# If the bridge isn't paired yet, it shows the QR code and waits.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

BRIDGE_DIR="$SCRIPT_DIR/../wa-slash-commands/bridge"
BRIDGE_BIN="$BRIDGE_DIR/wa-bridge"
STORE_DIR="$SCRIPT_DIR/store"
WHATSAPP_DB="$STORE_DIR/whatsapp.db"
BRIDGE_PORT=8080

# ── Check .env ──────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
    echo "❌ .env not found. Copy .env.example and fill in your keys:"
    echo "  cp .env.example .env"
    exit 1
fi

# Check that GEMINI_API_KEY is not still the placeholder
if grep -q 'your_gemini_api_key_here' .env 2>/dev/null; then
    echo "❌ GEMINI_API_KEY is still the placeholder value."
    echo "   Edit .env and replace it with your actual key:"
    echo "     nano .env"
    echo "   Get a free key at https://aistudio.google.com/apikey"
    exit 1
fi

# ── Check bridge binary exists ──────────────────────────────────────────────
if [ ! -f "$BRIDGE_BIN" ]; then
    echo "❌ Go bridge not found at $BRIDGE_BIN"
    exit 1
fi

# ── Python venv ─────────────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate
echo "📦 Installing dependencies..."
pip install -q -r requirements.txt

# ── Start the Go bridge if not running ──────────────────────────────────────
if lsof -i :$BRIDGE_PORT -sTCP:LISTEN >/dev/null 2>&1; then
    echo "✅ Bridge already running on port $BRIDGE_PORT"
else
    echo "🚀 Starting Go bridge..."

    # Ensure store dir exists
    mkdir -p "$STORE_DIR"

    # Check if bridge DBs already exist (already paired before)
    if [ -s "$WHATSAPP_DB" ] && [ -s "$STORE_DIR/messages.db" ]; then
        echo "📱 Bridge already paired — starting in background..."
        "$BRIDGE_BIN" > /dev/null 2>&1 &
        BRIDGE_PID=$!
    else
        echo ""
        echo "📱 Starting bridge — QR code will appear below."
        echo "   WhatsApp → Settings → Linked Devices → Link a Device"
        echo ""

        # Run bridge in foreground so QR displays properly in terminal
        "$BRIDGE_BIN" &
        BRIDGE_PID=$!

        # Show QR natively and wait for pairing
        for i in $(seq 1 120); do
            sleep 1
            if lsof -i :$BRIDGE_PORT -sTCP:LISTEN >/dev/null 2>&1; then
                echo ""
                echo "✅ Bridge connected!"
                break
            fi
        done
    fi

    echo "  Bridge PID: $BRIDGE_PID"

    # Wait for bridge HTTP server to be ready
    echo ""
    echo "⏳ Waiting for bridge HTTP server..."
    for i in $(seq 1 30); do
        sleep 1
        if curl -s -o /dev/null --connect-timeout 1 http://localhost:$BRIDGE_PORT/api/send 2>/dev/null \
           || lsof -i :$BRIDGE_PORT -sTCP:LISTEN >/dev/null 2>&1; then
            echo "✅ Bridge HTTP ready on port $BRIDGE_PORT"
            break
        fi
        [ $((i % 5)) -eq 0 ] && echo "   ... still waiting ($i s)"
    done

    # Wait for DBs to appear (history sync can take a moment after pairing)
    echo ""
    echo "⏳ Waiting for database sync (history downloading)..."
    for i in $(seq 1 60); do
        sleep 1
        if [ -s "$WHATSAPP_DB" ] && [ -s "$STORE_DIR/messages.db" ]; then
            size_w=$(wc -c < "$WHATSAPP_DB" 2>/dev/null || echo 0)
            size_m=$(wc -c < "$STORE_DIR/messages.db" 2>/dev/null || echo 0)
            if [ "$size_w" -gt 0 ] && [ "$size_m" -gt 0 ]; then
                echo "✅ DBs ready (whatsapp: ${size_w}B, messages: ${size_m}B)"
                break
            fi
        fi
        [ $((i % 5)) -eq 0 ] && echo "   ... still waiting ($i s)"
    done

    if [ ! -s "$WHATSAPP_DB" ] || [ ! -s "$STORE_DIR/messages.db" ]; then
        echo "⚠️  DBs are still empty after 60s. The bridge may need more time."
        echo "   Starting bot anyway — it will retry."
    fi

    echo "✅ Bridge is running and paired."
fi

# ── Start the Python bot ────────────────────────────────────────────────────
BOT_PID_FILE="$SCRIPT_DIR/.bot.pid"

# Check for already-running bot instance
if [ -f "$BOT_PID_FILE" ]; then
    OLD_PID=$(cat "$BOT_PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "❌ Bot already running (PID $OLD_PID). Use 'kill $OLD_PID' to stop it first."
        exit 1
    else
        rm -f "$BOT_PID_FILE"
    fi
fi

echo "🚀 Starting wa-pull bot..."
python3 main.py &
BOT_PID=$!
echo "$BOT_PID" > "$BOT_PID_FILE"
echo "  Bot PID: $BOT_PID"

# Clean up PID file when bot exits
wait $BOT_PID
rm -f "$BOT_PID_FILE"