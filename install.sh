#!/usr/bin/env bash
set -e

APP_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[*] Installing system packages..."
sudo apt update
sudo apt install -y \
  python3 \
  python3-venv \
  python3-pip \
  portaudio19-dev \
  python3-dev \
  network-manager \
  avahi-daemon \
  git

echo "[*] Setting hostname to livefeed..."
sudo hostnamectl set-hostname livefeed

echo "[*] Creating virtual environment..."
cd "$APP_DIR"
python3 -m venv venv
source venv/bin/activate

echo "[*] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "[*] Installing systemd service..."
sudo cp audio-live-feed.service /etc/systemd/system/audio-live-feed.service
sudo systemctl daemon-reload
sudo systemctl enable audio-live-feed

echo
echo "=================================================="
echo " Installation complete."
echo
echo " Reboot:"
echo "   sudo reboot"
echo
echo " Then connect to:"
echo "   WiFi: Live Feed"
echo "   URL:  http://livefeed.local:8000"
echo "   Backup: http://10.42.0.1:8000"
echo "=================================================="