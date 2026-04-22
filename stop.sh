#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# discord-mitm: stop & rollback
# Stops mitmproxy, restarts Discord normally.
# Your system goes back to how it was.
# ─────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$SCRIPT_DIR/.mitmproxy.pid"
DISCORD_BIN="/usr/bin/discord"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}[discord-mitm]${NC} Stopping and rolling back..."

# ── Stop mitmproxy ───────────────────────────────────────
if [ -f "$PIDFILE" ]; then
    PID="$(cat "$PIDFILE")"
    if kill -0 "$PID" 2>/dev/null; then
        echo -e "${YELLOW}[1/2]${NC} Stopping mitmproxy (PID $PID)..."
        kill "$PID" 2>/dev/null || true
        sleep 1
        kill -9 "$PID" 2>/dev/null || true
        echo "  Stopped."
    else
        echo -e "${YELLOW}[1/2]${NC} mitmproxy not running (stale PID)."
    fi
    rm -f "$PIDFILE"
else
    echo -e "${YELLOW}[1/2]${NC} No mitmproxy PID file found. Killing any mitmdump..."
    pkill -f mitmdump 2>/dev/null || true
fi

# ── Restart Discord normally ─────────────────────────────
echo -e "${YELLOW}[2/2]${NC} Restarting Discord normally..."
pkill -f '/usr/share/discord/Discord' 2>/dev/null || true
pkill -x Discord 2>/dev/null || true
sleep 2
"$DISCORD_BIN" &>/dev/null &
disown

echo ""
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Rollback complete! Everything is back to normal.${NC}"
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo ""
