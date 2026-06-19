# Live Audio Feed

A lightweight WebRTC audio streaming server that creates its own WiFi hotspot and streams live microphone audio to multiple listeners through a browser.

The goal is simple:

```txt
Start the app
Connect to the WiFi hotspot
Open the web page
Tap LISTEN LIVE
```

No mobile app required.

---

## Features

- Live microphone audio streaming
- Multiple simultaneous listeners
- One shared microphone source for all listeners
- WebRTC-based low-latency audio
- Browser-based listening page
- Automatic WiFi hotspot creation
- Automatic WiFi interface detection
- Raspberry Pi compatible
- Optional auto-start on boot with systemd
- Configurable through `config.env`

---

## Quick Start: Ubuntu / Linux Laptop

### 1. Clone the repository

```bash
git clone https://github.com/wainaze/audio_live_feed.git
cd audio_live_feed
```

### 2. Install system dependencies

```bash
sudo apt update

sudo apt install -y \
    python3 \
    python3-venv \
    python3-pip \
    portaudio19-dev \
    python3-dev \
    network-manager
```

### 3. Create a virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 4. Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Check or create `config.env`

Example:

```env
APP_TITLE="Live Audio Transmission"

HOTSPOT_ENABLED=true
HOTSPOT_NAME="Live Feed"
HOTSPOT_MODE=password
HOTSPOT_PASSWORD=12345678

WIFI_INTERFACE=auto

SERVER_HOST=0.0.0.0
SERVER_PORT=8000

AUDIO_RATE=48000
AUDIO_FRAME_SAMPLES=960
CHANNELS=1

VOLUME_MULTIPLIER=1.0
NOISE_GATE=0
SELECTED_INPUT_INDEX=
```

### 6. Start the app

```bash
python main.py
```

The app will create a WiFi hotspot.

Default listener details:

```txt
WiFi: Live Feed
Password: 12345678
URL: http://10.42.0.1:8000
```

Then connect from a phone, tablet, or laptop and tap:

```txt
LISTEN LIVE
```

---

## Run Without Hotspot

For local testing on an existing WiFi network:

```bash
python main.py --no-hotspot
```

Then open:

```txt
http://YOUR_LOCAL_IP:8000
```

---

## Raspberry Pi Installation

This setup is designed to make the Raspberry Pi work like a small audio appliance:

```txt
Plug in Raspberry Pi
It boots
It creates the WiFi hotspot
It starts the audio server
Users connect and listen
```

### Recommended Raspberry Pi OS

Use:

```txt
Raspberry Pi OS Lite 64-bit
```

Desktop also works, but Lite is cleaner for an appliance-style device.

### 1. Clone the repository

On the Raspberry Pi:

```bash
git clone https://github.com/wainaze/audio_live_feed.git
cd audio_live_feed
```

### 2. Check `config.env`

Recommended Raspberry Pi config:

```env
HOTSPOT_ENABLED=true
HOTSPOT_NAME="Live Feed"
HOTSPOT_MODE=password
HOTSPOT_PASSWORD=12345678
WIFI_INTERFACE=auto
SERVER_PORT=8000
```

`WIFI_INTERFACE=auto` should detect the Pi WiFi interface automatically.

The usual Raspberry Pi WiFi interface is:

```txt
wlan0
```

If auto-detection fails, set:

```env
WIFI_INTERFACE=wlan0
```

### 3. Run the installer

```bash
bash install.sh
```

The installer should:

- install system packages
- create the Python virtual environment
- install Python dependencies
- install the systemd service
- enable auto-start at boot
- enable local access through `livefeed.local`

### 4. Reboot

```bash
sudo reboot
```

After reboot, connect to:

```txt
WiFi: Live Feed
Password: 12345678
URL: http://livefeed.local:8000
Backup URL: http://10.42.0.1:8000
```

---

## Raspberry Pi Service Commands

Check status:

```bash
sudo systemctl status audio-live-feed
```

Start:

```bash
sudo systemctl start audio-live-feed
```

Stop:

```bash
sudo systemctl stop audio-live-feed
```

Restart:

```bash
sudo systemctl restart audio-live-feed
```

View logs:

```bash
journalctl -u audio-live-feed -f
```

Update from GitHub:

```bash
git pull
sudo systemctl restart audio-live-feed
```

---

## Useful App Pages

Main listener page:

```txt
http://10.42.0.1:8000
```

or on Raspberry Pi:

```txt
http://livefeed.local:8000
```

Microphone list:

```txt
http://10.42.0.1:8000/mics
```

Stats:

```txt
http://10.42.0.1:8000/stats
```

Button test:

```txt
http://10.42.0.1:8000/button-test
```

---

## Configuration Reference

All user-facing settings should live in `config.env`.

### App

```env
APP_TITLE="Live Audio Transmission"
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
```

### Hotspot

```env
HOTSPOT_ENABLED=true
HOTSPOT_NAME="Live Feed"
HOTSPOT_MODE=password
HOTSPOT_PASSWORD=12345678
WIFI_INTERFACE=auto
```

`HOTSPOT_MODE` can be:

```txt
password
open
```

If using password mode, the password must be at least 8 characters.

### Audio

```env
AUDIO_RATE=48000
AUDIO_FRAME_SAMPLES=960
CHANNELS=1
VOLUME_MULTIPLIER=1.0
NOISE_GATE=0
SELECTED_INPUT_INDEX=
```

Recommended low-latency setting:

```env
AUDIO_RATE=48000
AUDIO_FRAME_SAMPLES=960
```

Alternative:

```env
AUDIO_RATE=44100
AUDIO_FRAME_SAMPLES=882
```

---

## Architecture

```txt
Microphone
    ↓
PyAudio / PortAudio
    ↓
Shared microphone capture source
    ↓
aiortc MediaStreamTrack
    ↓
WebRTC peer connection
    ↓
Browser audio playback
```

The server also handles browser signaling through FastAPI:

```txt
Browser creates WebRTC offer
    ↓
POST /offer
    ↓
FastAPI receives offer
    ↓
aiortc creates answer
    ↓
Browser receives audio track
```

---

## Multiple Listener Design

Earlier versions opened the microphone once per connected listener.

The current design uses one shared microphone source:

```txt
Microphone opened once
    ↓
Audio frames captured continuously
    ↓
Each connected listener receives a copy
```

This is better because:

- the microphone is not opened repeatedly
- multiple clients are more reliable
- CPU and audio device usage are lower
- the app behaves more like a broadcast source

---

## Recommended Raspberry Pi Hardware

### Best overall

```txt
Raspberry Pi 4 Model B, 4GB
```

### Also excellent

```txt
Raspberry Pi 5, 4GB or 8GB
```

### Budget option

```txt
Raspberry Pi 4 Model B, 2GB
```

### Not recommended unless experimenting

```txt
Raspberry Pi Zero 2 W
```

It may work, but WebRTC, WiFi hotspot, and live audio are more demanding than simple scripts.

---

## Recommended Power Supply

Use an official power supply.

For Raspberry Pi 4:

```txt
Official USB-C 5V 3A power supply
```

For Raspberry Pi 5:

```txt
Official Raspberry Pi 5 USB-C power supply
```

Weak power supplies can cause:

- WiFi instability
- audio glitches
- random disconnects
- reboot loops

---

## Recommended Microphones

Raspberry Pi boards do not have a built-in microphone. Use a USB microphone or USB audio interface.

### Simple USB microphones

Good for quick testing:

- Fifine USB microphone
- Blue Snowball
- Logitech USB conference microphone
- Any class-compliant USB microphone

### Better USB microphones

Better audio quality:

- Rode NT-USB Mini
- Audio-Technica ATR2500x-USB
- Samson Q2U
- Shure MV7 USB

### Best flexible setup

Use a USB audio interface:

- Focusrite Scarlett Solo
- Behringer UMC22
- PreSonus AudioBox USB

Then connect an XLR microphone.

---

## Recommended Reliable Setup

For best reliability:

```txt
Raspberry Pi 4 or Raspberry Pi 5
Official power supply
USB microphone
Ethernet for admin access
Built-in WiFi used as hotspot
```

This gives the cleanest setup:

```txt
Ethernet = remote admin / SSH
WiFi = listener hotspot
USB = microphone input
```

---

## Troubleshooting

### PyAudio installation fails

Install PortAudio development headers:

```bash
sudo apt update
sudo apt install -y portaudio19-dev python3-dev
pip install pyaudio
```

### No microphone detected

Open:

```txt
http://10.42.0.1:8000/mics
```

or:

```txt
http://livefeed.local:8000/mics
```

Then set a specific microphone in `config.env`:

```env
SELECTED_INPUT_INDEX=2
```

Restart:

```bash
sudo systemctl restart audio-live-feed
```

### Hotspot fails

Check WiFi devices:

```bash
nmcli device status
```

Check only device/type/state:

```bash
nmcli -t -f DEVICE,TYPE,STATE device
```

Restart NetworkManager:

```bash
sudo systemctl restart NetworkManager
```

Restart the app:

```bash
sudo systemctl restart audio-live-feed
```

### Cannot open `livefeed.local`

Use the backup URL:

```txt
http://10.42.0.1:8000
```

Make sure Avahi is installed and running:

```bash
sudo systemctl status avahi-daemon
```

Restart it:

```bash
sudo systemctl restart avahi-daemon
```

### Page loads but the button does not work

Open:

```txt
http://10.42.0.1:8000/button-test
```

If the button test works, check app logs:

```bash
journalctl -u audio-live-feed -f
```

### Audio is choppy

Try:

```env
AUDIO_RATE=48000
AUDIO_FRAME_SAMPLES=960
```

or:

```env
AUDIO_RATE=44100
AUDIO_FRAME_SAMPLES=882
```

Then restart.

On Raspberry Pi:

```bash
sudo systemctl restart audio-live-feed
```

On laptop:

```bash
python main.py
```

### Background noise is too loud

First, use a better microphone.

Then optionally try:

```env
NOISE_GATE=300
```

Be careful: a high noise gate can make speech sound chopped.

### Volume is too low

Try:

```env
VOLUME_MULTIPLIER=1.5
```

Avoid setting this too high or the audio may distort.

---

## Git Workflow

After editing:

```bash
git add .
git commit -m "Update live audio feed"
git push
```

On Raspberry Pi, pull updates:

```bash
git pull
sudo systemctl restart audio-live-feed
```

---

## Security Notes

This app is designed for local WiFi usage.

Do not expose it directly to the public internet without adding:

- HTTPS
- authentication
- access control
- deployment hardening
- TURN/STUN configuration for WebRTC over the internet

For local hotspot use, the current design is intentionally simple.

---

## Roadmap Ideas

Possible future improvements:

- HTTPS support
- Internet streaming mode
- Cloud relay mode
- Admin dashboard
- QR code for listener URL
- Audio quality presets
- Recording option
- Battery-powered Raspberry Pi build
- Push-to-talk mode
- Listener count display on main page
- Automatic microphone selection
