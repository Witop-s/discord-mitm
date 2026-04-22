#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────
# discord-mitm: start
# Starts mitmproxy and relaunches Discord through the proxy.
# Run stop.sh to undo everything.
# ─────────────────────────────────────────────────────────
set -euo pipefail

PROXY_PORT=8080
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ADDON="$SCRIPT_DIR/addon.py"
CERT_SRC="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
PIDFILE="$SCRIPT_DIR/.mitmproxy.pid"
LOGFILE="$SCRIPT_DIR/mitm.log"
DISCORD_BIN="/usr/bin/discord"
BG=true

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Parse flags
for arg in "$@"; do
    case "$arg" in
        --bg|-b) BG=true ;;
    esac
done

echo -e "${GREEN}[discord-mitm]${NC} Starting up..."

# ── Sanity check ─────────────────────────────────────────
if [ ! -f "$DISCORD_BIN" ]; then
    echo -e "${RED}Error:${NC} Discord not found at $DISCORD_BIN"
    echo "  Install the .deb package first: sudo dpkg -i /tmp/discord.deb"
    exit 1
fi

# ── Step 1: Generate mitmproxy CA cert if needed ─────────
if [ ! -f "$CERT_SRC" ]; then
    echo -e "${YELLOW}[1/3]${NC} Generating mitmproxy CA certificate..."
    timeout 2 mitmdump --quiet 2>/dev/null || true
    if [ ! -f "$CERT_SRC" ]; then
        echo -e "${RED}Error:${NC} Failed to generate CA cert. Try running 'mitmdump' manually once."
        exit 1
    fi
    echo "  CA cert generated at $CERT_SRC"
else
    echo -e "${YELLOW}[1/3]${NC} CA certificate already exists."
fi

# ── Step 2: Start mitmproxy ─────────────────────────────
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo -e "${YELLOW}[2/3]${NC} mitmproxy already running (PID $(cat "$PIDFILE"))."
else
    echo -e "${YELLOW}[2/3]${NC} Starting mitmproxy on port $PROXY_PORT..."
    if $BG; then
        mitmdump --listen-port "$PROXY_PORT" \
                 --set console_eventlog_verbosity=warn \
                 -s "$ADDON" >> "$LOGFILE" 2>&1 &
    else
        mitmdump --listen-port "$PROXY_PORT" \
                 --set console_eventlog_verbosity=warn \
                 -s "$ADDON" &
    fi
    MITM_PID=$!
    echo "$MITM_PID" > "$PIDFILE"
    sleep 1

    if kill -0 "$MITM_PID" 2>/dev/null; then
        echo "  mitmproxy started (PID $MITM_PID)."
    else
        echo -e "${RED}Error:${NC} mitmproxy failed to start. Check port $PROXY_PORT."
        rm -f "$PIDFILE"
        exit 1
    fi
fi

# ── Step 3: Restart Discord through the proxy ────────────
echo -e "${YELLOW}[3/3]${NC} Restarting Discord through proxy..."
# Kill Discord but not this script (whose path contains "discord-mitm")
pkill -f '/usr/share/discord/Discord' 2>/dev/null || true
pkill -x Discord 2>/dev/null || true
sleep 2

# --proxy-server: route all traffic through mitmproxy
# --ignore-certificate-errors: trust mitmproxy's TLS interception
#   (Chromium doesn't use NODE_EXTRA_CA_CERTS — it has its own cert store)
"$DISCORD_BIN" \
    --proxy-server="http://127.0.0.1:$PROXY_PORT" \
    --ignore-certificate-errors \
    &>/dev/null &
disown

echo ""
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo -e "${GREEN}  discord-mitm is running!${NC}"
echo -e "${GREEN}════════════════════════════════════════════════${NC}"
echo ""
echo "  Proxy:    http://127.0.0.1:$PROXY_PORT"
echo "  Addon:    $ADDON"
echo ""
echo "  To stop:  $SCRIPT_DIR/stop.sh"
echo ""
echo -e "${YELLOW}  Tip: edit addon.py ALERT_KEYWORDS to highlight specific words${NC}"
echo -e "${YELLOW}  Tip: set LOG_READS=True in addon.py to see incoming messages${NC}"
echo ""

if $BG; then
    echo "  Logs:     $LOGFILE"
    echo "  Live:     tail -f $LOGFILE"
    echo ""
    disown -a
else
    echo "  Logs:     this terminal (Ctrl+C to detach)"
    echo ""
    # Keep the script running so the user sees mitmproxy output
    wait
fi
