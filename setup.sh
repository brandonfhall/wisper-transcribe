#!/usr/bin/env bash
# First-time setup for wisper-transcribe on Mac/Linux.
# Creates a virtual environment, installs the package, and checks ffmpeg.
#
# Usage:  bash setup.sh

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
GRAY='\033[0;90m'
NC='\033[0m'

step() { echo -e "\n${CYAN}>> $1${NC}"; }
ok()   { echo -e "   ${GREEN}OK  : $1${NC}"; }
warn() { echo -e "   ${YELLOW}WARN: $1${NC}"; }
fail() { echo -e "   ${RED}FAIL: $1${NC}"; exit 1; }

echo ""
echo "wisper-transcribe setup (Mac/Linux)"
echo "====================================="

# ── Python ────────────────────────────────────────────────────────────────────
step "Checking Python..."
if command -v python3 &>/dev/null; then
    VER=$(python3 -c "import sys; v=sys.version_info; print(f'{v.major}.{v.minor}.{v.micro}')")
    MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
    MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
    if [ "$MAJOR" -lt 3 ] || ([ "$MAJOR" -eq 3 ] && [ "$MINOR" -lt 10 ]); then
        fail "Python $VER found but 3.10+ is required. Install from https://python.org"
    fi
    ok "Python $VER"
else
    fail "Python 3 not found. Install from https://python.org or: brew install python"
fi

# ── Virtual environment ───────────────────────────────────────────────────────
step "Setting up virtual environment..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
    ok "Created .venv"
else
    ok ".venv already exists"
fi

PIP=".venv/bin/pip"
PYTHON=".venv/bin/python"

# ── Install package ───────────────────────────────────────────────────────────
step "Installing wisper-transcribe..."
"$PIP" install -e . -q
ok "wisper-transcribe installed"

# ── ffmpeg ────────────────────────────────────────────────────────────────────
step "Checking ffmpeg..."
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg found"
else
    warn "ffmpeg not found."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo -e "   Install with: ${YELLOW}brew install ffmpeg${NC}"
    else
        echo -e "   Install with: ${YELLOW}sudo apt install ffmpeg${NC}  (or your distro's equivalent)"
    fi
fi

# ── Apple Silicon note ────────────────────────────────────────────────────────
if [[ "$OSTYPE" == "darwin"* ]]; then
    ARCH=$(uname -m)
    if [[ "$ARCH" == "arm64" ]]; then
        echo ""
        echo -e "${GRAY}   Note: Apple Silicon detected (MPS). wisper uses CPU mode by default${NC}"
        echo -e "${GRAY}   (MPS is unreliable for faster-whisper and pyannote — CPU gives stable results)${NC}"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Activate venv:     source .venv/bin/activate"
echo "  2. Accept HF licenses (one-time, free) — see README.md"
echo "  3. Store HF token:    wisper config set hf_token hf_xxxxxxx"
echo "  4. First session:     wisper transcribe session01.mp3 --enroll-speakers"
echo ""
