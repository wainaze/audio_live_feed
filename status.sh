#!/usr/bin/env bash
# status.sh - Human-friendly status dashboard for Live Audio Feed

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo ""
echo "========================================================"
echo "               🎙️  LIVE AUDIO FEED STATUS"
echo "========================================================"

# 1. Check Service Status
if systemctl is-active --quiet audio-live-feed; then
    PID=$(systemctl show --property MainPID --value audio-live-feed)
    echo -e " ● Audio Service:  ${GREEN}🟢 ACTIVE & RUNNING${NC} (PID: $PID)"
else
    echo -e " ● Audio Service:  ${RED}🔴 STOPPED / FAILED${NC}"
fi

# 2. Check Port 8000 / Web Server
if command -v ss >/dev/null 2>&1 && ss -tulpn | grep -q ":8000"; then
    echo -e " ● Web Server:     ${GREEN}🟢 LISTENING ON PORT 8000${NC}"
elif command -v netstat >/dev/null 2>&1 && netstat -tulpn | grep -q ":8000"; then
    echo -e " ● Web Server:     ${GREEN}🟢 LISTENING ON PORT 8000${NC}"
else
    echo -e " ● Web Server:     ${YELLOW}🟡 PORT 8000 NOT DETECTED${NC}"
fi

# 3. Check IP & Access URLs
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
IP=${IP:-"10.42.0.1"}

echo -e " ● Listener URL:   ${BLUE}http://${IP}:8000${NC}"
echo -e " ● Local mDNS:     ${BLUE}http://livefeed.local:8000${NC}"
echo -e " ● Admin Panel:    ${BLUE}http://${IP}:8000/admin${NC}"

# 4. Check Wi-Fi / Hotspot
if command -v nmcli >/dev/null 2>&1; then
    ACTIVE_CON=$(nmcli -t -f NAME,TYPE,STATE connection show --active 2>/dev/null | grep wifi | head -n 1)
    if [ -n "$ACTIVE_CON" ]; then
        CON_NAME=$(echo "$ACTIVE_CON" | cut -d: -f1)
        echo -e " ● Wi-Fi State:    ${GREEN}🟢 BROADCASTING ($CON_NAME)${NC}"
    else
        echo -e " ● Wi-Fi State:    ${YELLOW}🟡 NO ACTIVE WI-FI HOTSPOT DETECTED${NC}"
    fi
fi

# 5. Check Audio Input Devices (Microphone)
if command -v arecord >/dev/null 2>&1; then
    MIC_COUNT=$(arecord -l 2>/dev/null | grep -c "^card" || true)
    if [ "$MIC_COUNT" -gt 0 ]; then
        FIRST_MIC=$(arecord -l 2>/dev/null | grep "^card" | head -n 1 | sed 's/card [0-9]: //')
        echo -e " ● Microphone:     ${GREEN}🟢 DETECTED ($FIRST_MIC)${NC}"
    else
        echo -e " ● Microphone:     ${YELLOW}⚠️  NO USB MICROPHONE FOUND (Plug in USB mic)${NC}"
    fi
fi

echo "========================================================"

# If failed, show recent error log automatically
if ! systemctl is-active --quiet audio-live-feed; then
    echo ""
    echo -e "${RED}⚠️  Recent Error Logs (journalctl):${NC}"
    journalctl -u audio-live-feed -n 5 --no-pager
    echo ""
fi
