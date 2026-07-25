#!/bin/bash
set -e

# hourlyB — 1-line installer
# curl -sSL https://raw.githubusercontent.com/sameer-hoda/wa-pull/main/install.sh | bash

REPO="https://github.com/sameer-hoda/wa-pull"
INSTALL_DIR="$HOME/wa-pull"

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║         hourlyB — WhatsApp AI Bot        ║"
echo "  ║           one-line installer             ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# ── Check prerequisites ──────────────────────────────────────────────────────
command -v python3 >/dev/null 2>&1 || {
    echo "❌ python3 is required but not installed."
    echo "   macOS:   brew install python@3"
    echo "   Ubuntu:  sudo apt install python3 python3-venv python3-pip"
    echo "   Arch:    sudo pacman -S python python-pip"
    exit 1
}

PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MINOR=$(echo "$PYTHON_VER" | cut -d. -f2)
if [ "$PY_MINOR" -lt 10 ]; then
    echo "❌ Python 3.10+ required (found $PYTHON_VER)"
    exit 1
fi
echo "✅ Python $PYTHON_VER"

command -v git >/dev/null 2>&1 || {
    echo "❌ git is required but not installed."
    echo "   macOS:   xcode-select --install"
    echo "   Ubuntu:  sudo apt install git"
    exit 1
}
echo "✅ git"

# ── Clone or update ───────────────────────────────────────────────────────────
if [ -d "$INSTALL_DIR" ]; then
    echo "📦 Updating hourlyB..."
    cd "$INSTALL_DIR"
    git pull --ff-only origin main
else
    echo "📦 Cloning hourlyB..."
    git clone "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── Virtual environment ───────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
    echo "🐍 Creating virtual environment..."
    python3 -m venv venv
fi
source venv/bin/activate

# ── Install dependencies ──────────────────────────────────────────────────────
echo "📦 Installing Python dependencies..."
pip install -q --upgrade pip
pip install -q -r requirements.txt

# ── Create .env if it doesn't exist ───────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "📝 Created .env from .env.example"
fi

# ── Create directories ────────────────────────────────────────────────────────
mkdir -p store okf_bundle/groups okf_bundle/contacts okf_bundle/project_docs

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║         ✅ hourlyB is installed!         ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit ~/wa-pull/.env and paste your Gemini API key:"
echo "     nano $INSTALL_DIR/.env"
echo ""
echo "     Change this line:"
echo '       GEMINI_API_KEY="your_gemini_api_key_here"'
echo ""
echo "     To (paste your real key between the quotes):"
echo '       GEMINI_API_KEY="AIza..."'
echo ""
echo "     Get a free key at https://aistudio.google.com/apikey"
echo ""
echo "  2. Build and run the WhatsApp bridge:"
echo "     git clone https://github.com/sameer-hoda/wa-slash-commands"
echo "     cd wa-slash-commands/bridge && go build -o wa-bridge"
echo "     ./wa-bridge"
echo "       (scan QR in WhatsApp → Settings → Linked Devices)"
echo ""
echo "  3. Start hourlyB:"
echo "     cd $INSTALL_DIR && ./start.sh"
echo ""