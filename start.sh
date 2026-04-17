#!/usr/bin/env bash
# wisper-transcribe — Linux launcher
# Run:  bash start.sh
# The first run will set up the virtual environment automatically (~5 min).

set -e
cd "$(dirname "$0")"

GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

echo ""
echo -e "${CYAN}wisper-transcribe${NC}"
echo "================="

# ── First-time setup ──────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    echo "First run — setting up wisper-transcribe (this takes a few minutes)..."
    echo ""
    bash setup.sh
fi

# ── Start server ──────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}Starting wisper at http://localhost:8080${NC}"
echo "Press Ctrl+C to stop."
echo ""

# Open the browser after the server has had a moment to start
(sleep 2 && xdg-open "http://localhost:8080" 2>/dev/null || true) &

source .venv/bin/activate
exec wisper server --host 127.0.0.1 --port 8080
