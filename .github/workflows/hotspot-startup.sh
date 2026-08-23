#!/usr/bin/env bash
# hotspot-startup.sh - Ensures the Wi-Fi hotspot is 100% unblocked and broadcasting on boot

set -e

echo "[*] Initializing Wi-Fi Hardware & Regulatory Domain..."
rfkill unblock all 2>/dev/null || true
rfkill unblock wifi 2>/dev/null || true
command -v iw >/dev/null 2>&1 && iw reg set US 2>/dev/null || true

echo "[*] Ensuring Wi-Fi radio is enabled in NetworkManager..."
nmcli radio wifi on 2>/dev/null || true
sleep 2

echo "[*] Activating 'Live Feed Hotspot'..."
if ! nmcli connection up "Live Feed Hotspot" 2>/dev/null; then
    echo "[*] Activating via dynamic hotspot fallback..."
    nmcli device wifi hotspot ssid "Live Feed" password "12345678" 2>/dev/null || true
fi

echo "[*] Wi-Fi Hotspot setup complete."
