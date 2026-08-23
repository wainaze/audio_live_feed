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
echo "[*] Setting hostname to livefeed..."
sudo hostnamectl set-hostname livefeed 2>/dev/null || echo "livefeed" | sudo tee /etc/hostname >/dev/null
echo "[*] Creating virtual environment..."
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate
echo "[*] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt
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
ExecStart=${APP_DIR}/start.sh
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
