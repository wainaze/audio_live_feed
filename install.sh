#!/usr/bin/env bash
set -e

# Detect the real user and absolute directory
CURRENT_USER="${SUDO_USER:-$(id -un)}"
APP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[*] Installing for user: $CURRENT_USER in $APP_DIR"

echo "[*] Installing system packages..."
sudo apt update
sudo apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  portaudio19-dev \
  python3-dev \
  network-manager \
  wpasupplicant \
  wireless-regdb \
  iw \
  avahi-daemon \
  rfkill \
  iptables \
  git

# Ensure user is in audio, video, netdev groups
sudo usermod -aG audio,video,netdev "$CURRENT_USER" 2>/dev/null || true

# Configure passwordless sudo for background services
echo "${CURRENT_USER} ALL=(ALL) NOPASSWD: ALL" | sudo tee "/etc/sudoers.d/010_${CURRENT_USER}-nopasswd" >/dev/null
sudo chmod 0440 "/etc/sudoers.d/010_${CURRENT_USER}-nopasswd" 2>/dev/null || true

# Configure Polkit rule for NetworkManager
sudo mkdir -p /etc/polkit-1/rules.d 2>/dev/null || true
sudo bash -c 'cat << "EOF" > /etc/polkit-1/rules.d/50-org.freedesktop.NetworkManager.rules
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0) {
        return polkit.Result.YES;
    }
});
EOF' 2>/dev/null || true

echo "[*] Setting hostname to livefeed..."
sudo hostnamectl set-hostname livefeed 2>/dev/null || echo "livefeed" | sudo tee /etc/hostname >/dev/null

echo "[*] Creating virtual environment..."
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate

echo "[*] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Ensure NetworkManager is unmasked, enabled, and running
sudo systemctl unmask NetworkManager 2>/dev/null || true
sudo systemctl enable NetworkManager 2>/dev/null || true
sudo systemctl start NetworkManager 2>/dev/null || true

# Pre-configure persistent NetworkManager Hotspot connection profile
sudo mkdir -p /etc/NetworkManager/system-connections
sudo bash -c 'cat << "EOF" > /etc/NetworkManager/system-connections/LiveFeedHotspot.nmconnection
[connection]
id=Live Feed Hotspot
uuid=a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d
type=wifi
autoconnect=true
autoconnect-priority=100

[wifi]
mode=ap
ssid=Live Feed
band=bg
channel=6

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
group=ccmp
psk=12345678

[ipv4]
method=shared
address1=10.42.0.1/24

[ipv6]
method=ignore
EOF'
sudo chmod 600 /etc/NetworkManager/system-connections/LiveFeedHotspot.nmconnection 2>/dev/null || true

# Ensure start.sh is executable
chmod +x "$APP_DIR/start.sh"

echo "[*] Generating and installing systemd service..."
sudo bash -c "cat << EOF > /etc/systemd/system/audio-live-feed.service
[Unit]
Description=Live Audio Feed
After=network.target sound.target NetworkManager.service
Wants=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStartPre=-/usr/sbin/iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
ExecStartPre=-/usr/sbin/iptables -I INPUT -p tcp --dport 80 -j ACCEPT
ExecStartPre=-/usr/sbin/iptables -t nat -I PREROUTING -p tcp --dport 80 -j REDIRECT --to-port 8000
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/main.py --no-hotspot
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF"

sudo systemctl daemon-reload
sudo systemctl enable audio-live-feed
sudo systemctl restart audio-live-feed

echo
echo "=================================================="
echo " Installation complete!"
echo " Service audio-live-feed is installed and active."
echo
echo " Connect to:"
echo "   WiFi: Live Feed"
echo "   URL:  http://livefeed.local:8000"
echo "   Backup: http://10.42.0.1:8000"
echo "=================================================="
