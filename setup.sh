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

# Runs a pip install in the background while showing a spinner.
# Usage: pip_with_spinner "Description" install -e . -q
# Captures output; on failure prints it then exits.
pip_with_spinner() {
    local desc="$1"; shift
    local log; log=$(mktemp)
    "$PIP" "$@" > "$log" 2>&1 &
    local pid=$!
    local spinstr='|/-\'
    local i=0
    printf "   "
    while kill -0 "$pid" 2>/dev/null; do
        local spin_char="${spinstr:$((i % 4)):1}"
        printf "\r   %s  %s..." "$spin_char" "$desc"
        sleep 0.4
        i=$((i + 1))
    done
    printf "\r   \033[K"   # clear spinner line
    wait "$pid"
    local exit_code=$?
    if [ "$exit_code" -ne 0 ]; then
        cat "$log"
        rm -f "$log"
        fail "$desc failed (exit code $exit_code)"
    fi
    rm -f "$log"
}

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
step "Installing wisper-transcribe (this may take several minutes)..."
pip_with_spinner "Installing wisper-transcribe" install -e . -q
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

# ── Java 25 (Discord recording bot) ──────────────────────────────────────────
step "Checking Java 25+ (needed for Discord recording bot)..."
if ! command -v java &>/dev/null; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
        warn "Java 25+ not found — Discord recording bot unavailable."
        warn "Install: brew install openjdk@25   or download from https://adoptium.net"
    else
        warn "Java 25+ not found — Discord recording bot unavailable."
        warn "Install: sudo apt-get install openjdk-25-jre-headless   or download from https://adoptium.net"
    fi
elif java -version 2>&1 | head -1 | grep -qE '"25\.'; then
    ok "Java 25 found"
else
    JAVA_VER=$(java -version 2>&1 | head -1)
    if [[ "$OSTYPE" == "darwin"* ]]; then
        warn "Java 25+ required for Discord recording bot (found: $JAVA_VER)."
        warn "Install: brew install openjdk@25   or download from https://adoptium.net"
    else
        warn "Java 25+ required for Discord recording bot (found: $JAVA_VER)."
        warn "Install: sudo apt-get install openjdk-25-jre-headless   or download from https://adoptium.net"
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

# ── LLM Post-processing setup ─────────────────────────────────────────────────
WISPER=".venv/bin/wisper"

step "LLM Post-processing setup (wisper refine / wisper summarize)"
echo ""
echo -e "   ${GRAY}Vocabulary correction and campaign notes need an LLM provider.${NC}"
echo -e "   ${GRAY}Local providers (Ollama / LM Studio) need no API key.${NC}"
echo ""

# Probe Ollama — outputs "count\nmodel1\nmodel2\n..." or nothing
OLLAMA_RUNNING=false; OLLAMA_MODELS=""
_ollama_raw=$(curl -sf --max-time 2 http://localhost:11434/api/tags 2>/dev/null) || true
if [ -n "$_ollama_raw" ]; then
    _parsed=$(echo "$_ollama_raw" | python3 -c "
import json,sys
ms=[m['name'] for m in json.load(sys.stdin).get('models',[])]
print(len(ms))
for m in ms: print(m)
" 2>/dev/null) || true
    if [ -n "$_parsed" ]; then
        OLLAMA_RUNNING=true
        _ollama_count=$(echo "$_parsed" | head -1)
        OLLAMA_MODELS=$(echo "$_parsed" | tail -n +2)
        ollama_tag="${GREEN}[running — ${_ollama_count} model(s) available]${NC}"
    fi
fi
$OLLAMA_RUNNING || ollama_tag="${GRAY}[not running]${NC}"

# Probe LM Studio
LM_RUNNING=false; LM_MODELS=""
_lm_raw=$(curl -sf --max-time 2 http://localhost:1234/v1/models 2>/dev/null) || true
if [ -n "$_lm_raw" ]; then
    _parsed=$(echo "$_lm_raw" | python3 -c "
import json,sys
ms=[m['id'] for m in json.load(sys.stdin).get('data',[])]
print(len(ms))
for m in ms: print(m)
" 2>/dev/null) || true
    if [ -n "$_parsed" ]; then
        LM_RUNNING=true
        _lm_count=$(echo "$_parsed" | head -1)
        LM_MODELS=$(echo "$_parsed" | tail -n +2)
        lm_tag="${GREEN}[running — ${_lm_count} model(s) loaded]${NC}"
    fi
fi
$LM_RUNNING || lm_tag="${GRAY}[not running]${NC}"

echo    "   LOCAL — no API key needed:"
echo -e "     o) Ollama     — localhost:11434  $ollama_tag"
echo -e "     l) LM Studio  — localhost:1234   $lm_tag"
echo ""
echo    "   CLOUD — requires API key:"
echo    "     a) Anthropic (Claude)"
echo    "     b) OpenAI (GPT)"
echo    "     c) Google (Gemini)"
echo    "     d) All three cloud SDKs"
echo ""
echo    "     s) Skip — configure later with: wisper config llm"
echo ""

# pick_model <provider> <newline-separated model list>
# Sets PICKED_MODEL to the chosen model name, or "" if skipped.
pick_model() {
    local provider="$1" model_list="$2"
    PICKED_MODEL=""
    if [ -z "$model_list" ]; then
        warn "$provider is running but has no models — load one then run: wisper config llm"
        return
    fi
    echo ""
    echo -e "   ${GRAY}Available $provider models:${NC}"
    local i=1
    while IFS= read -r m; do
        local hint=""; [ $i -eq 1 ] && hint="  <- suggested"
        echo "     $i) $m$hint"
        i=$((i + 1))
    done <<< "$model_list"
    echo ""
    read -r -p "   Pick a number [1] or Enter to configure later: " _pick
    [ -z "$_pick" ] && _pick="1"
    if [[ "$_pick" =~ ^[0-9]+$ ]]; then
        local sel; sel=$(echo "$model_list" | sed -n "${_pick}p")
        [ -n "$sel" ] && PICKED_MODEL="$sel"
    fi
}

read -r -p "   Choice [o/l/a/b/c/d/s]: " LLM_CHOICE
case "$(echo "$LLM_CHOICE" | tr '[:upper:]' '[:lower:]')" in
    o)
        if $OLLAMA_RUNNING; then
            pick_model "Ollama" "$OLLAMA_MODELS"
            if [ -n "$PICKED_MODEL" ]; then
                "$WISPER" config set llm_provider ollama
                "$WISPER" config set llm_model "$PICKED_MODEL"
                ok "Ollama configured — model: $PICKED_MODEL"
            else
                ok "Ollama selected — run 'wisper config llm' to set a model"
            fi
        else
            echo ""
            echo -e "   ${GRAY}Ollama is not running. To use it:${NC}"
            echo -e "   ${GRAY}  1. Install from https://ollama.com${NC}"
            echo -e "   ${GRAY}  2. Pull a model: ollama pull llama3.2${NC}"
            echo -e "   ${GRAY}  3. Configure:    wisper config llm${NC}"
            ok "Skipped — configure later with 'wisper config llm'"
        fi
        ;;
    l)
        if $LM_RUNNING; then
            pick_model "LM Studio" "$LM_MODELS"
            if [ -n "$PICKED_MODEL" ]; then
                "$WISPER" config set llm_provider lmstudio
                "$WISPER" config set llm_model "$PICKED_MODEL"
                ok "LM Studio configured — model: $PICKED_MODEL"
            else
                ok "LM Studio selected — run 'wisper config llm' to set a model"
            fi
        else
            echo ""
            echo -e "   ${GRAY}LM Studio is not running. To use it:${NC}"
            echo -e "   ${GRAY}  1. Install from https://lmstudio.ai${NC}"
            echo -e "   ${GRAY}  2. Download a model and start the local server${NC}"
            echo -e "   ${GRAY}  3. Configure: wisper config llm${NC}"
            ok "Skipped — configure later with 'wisper config llm'"
        fi
        ;;
    a) pip_with_spinner "Installing Anthropic SDK"    install -e ".[llm-anthropic]" -q && ok "anthropic SDK installed" ;;
    b) pip_with_spinner "Installing OpenAI SDK"       install -e ".[llm-openai]"    -q && ok "openai SDK installed" ;;
    c) pip_with_spinner "Installing Google Genai SDK" install -e ".[llm-google]"    -q && ok "google-genai SDK installed" ;;
    d) pip_with_spinner "Installing all LLM SDKs"    install -e ".[llm-all]"        -q && ok "all LLM SDKs installed" ;;
    *) ok "Skipped — configure later with 'wisper config llm'" ;;
esac

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Setup complete!${NC}"
echo ""
echo "Next steps:"
echo "  1. Activate venv:       source .venv/bin/activate"
echo "  2. Run setup wizard:    wisper setup   (HF token, model download, LLM config)"
echo "  3. First session:       wisper transcribe session01.mp3 --enroll-speakers"
echo "  4. LLM config (later):  wisper config llm"
echo ""
