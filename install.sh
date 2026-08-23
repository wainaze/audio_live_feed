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

# Ensure start.sh is executable
chmod +x "$APP_DIR/start.sh"

echo "[*] Generating and installing systemd service..."
sudo bash -c "cat << EOF > /etc/systemd/system/audio-live-feed.service
[Unit]
Description=Live Audio Feed
After=network-manager.service sound.target
Wants=network-manager.service

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${APP_DIR}
ExecStart=/bin/bash ${APP_DIR}/start.sh
Restart=always
RestartSec=5

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
