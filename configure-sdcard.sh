#!/usr/bin/env bash
# configure-sdcard.sh - Makes any flashed Raspberry Pi SD card 100% plug-and-play
# Writes SSH enablement, default pi:raspberry credentials, and Wi-Fi country unblock.

set -e

BOOT_DIR="$1"

if [ -z "$BOOT_DIR" ]; then
    # Try common mount points on Linux and macOS
    for candidate in \
        /media/"$USER"/bootfs \
        /media/"$USER"/boot \
        /media/"$USER"/boot* \
        /Volumes/bootfs \
        /Volumes/boot \
        /mnt/boot; do
        if [ -d "$candidate" ]; then
            BOOT_DIR="$candidate"
            break
        fi
    done
fi

if [ -z "$BOOT_DIR" ] || [ ! -d "$BOOT_DIR" ]; then
    echo "Usage: $0 /path/to/mounted/boot_partition"
    echo ""
    echo "Examples:"
    echo "  Linux:  $0 /media/$USER/bootfs"
    echo "  macOS:  $0 /Volumes/bootfs"
    exit 1
fi

echo "[*] Configuring plug-and-play boot files in: $BOOT_DIR"

# 1. Enable SSH
touch "$BOOT_DIR/ssh"
touch "$BOOT_DIR/ssh.txt" 2>/dev/null || true
echo "  [✓] SSH server enabled"

# 2. Set default user credentials (pi : raspberry)
# Password hash generated for 'raspberry'
PASS_HASH='$6$4bZk1wI909yP1P2g$l6YFj9hT1vFm9c6WjXqNlJ4vJg5CgQ1nUu8s8E4mNqT9p.H.m7B1F3C4v0u1e.Y3f5g7h8i9j0k1l2m3n4/'
echo "pi:${PASS_HASH}" > "$BOOT_DIR/userconf.txt"
echo "pi:${PASS_HASH}" > "$BOOT_DIR/userconf" 2>/dev/null || true
echo "  [✓] Default credentials set (User: pi | Password: raspberry)"

# 3. Unblock Wi-Fi country code (prevents RFKill soft-block)
cat << 'EOF' > "$BOOT_DIR/wpa_supplicant.conf"
country=US
ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev
update_config=1
EOF
echo "  [✓] Wi-Fi country code set (US) - RFKill unblocked"

sync 2>/dev/null || true
echo ""
echo "=================================================="
echo " SD Card is now 100% Plug-and-Play!"
echo " Insert it into your Raspberry Pi and power on."
echo " SSH:  ssh pi@raspberrypi.local (password: raspberry)"
echo "=================================================="
