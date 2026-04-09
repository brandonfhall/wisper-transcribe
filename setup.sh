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
    warn "ffmpeg not found — installing..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &>/dev/null; then
            brew install ffmpeg
            ok "ffmpeg installed via Homebrew"
        else
            fail "Homebrew not found. Install Homebrew first: https://brew.sh, then re-run this script."
        fi
    else
        if command -v apt-get &>/dev/null; then
            sudo apt-get install -y ffmpeg
            ok "ffmpeg installed via apt"
        elif command -v dnf &>/dev/null; then
            sudo dnf install -y ffmpeg
            ok "ffmpeg installed via dnf"
        else
            warn "Could not auto-install ffmpeg. Install it manually for your distro, then re-run."
        fi
    fi
fi

# ── Apple Silicon: mlx-whisper ────────────────────────────────────────────────
if [[ "$OSTYPE" == "darwin"* ]]; then
    ARCH=$(uname -m)
    if [[ "$ARCH" == "arm64" ]]; then
        if "$PYTHON" -c "import mlx_whisper" &>/dev/null 2>&1; then
            ok "mlx-whisper ready (Apple Silicon GPU/ANE transcription enabled)"
        else
            warn "mlx-whisper not importable despite being installed — transcription will fall back to CPU"
        fi
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Activate venv:     source .venv/bin/activate"
echo "  2. Run setup wizard:  wisper setup   (configures HF token + pre-downloads models)"
echo "  3. First session:     wisper transcribe session01.mp3 --enroll-speakers"
echo ""
