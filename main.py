import argparse
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import subprocess
import sys
import time
import threading
from fractions import Fraction
from typing import Optional

import av
import numpy as np
import pyaudio
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from fastapi import FastAPI, Request, Response, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

# =========================================================
# CONFIGURATION MANAGEMENT
# =========================================================

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")


def load_env_file(path=CONFIG_PATH):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val


def save_env_values(updates: dict, path=CONFIG_PATH):
    lines = []
    existing_keys = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _ = stripped.split("=", 1)
            key = key.strip()
            if key in updates:
                new_lines.append(f'{key}="{updates[key]}"\n')
                existing_keys.add(key)
                continue
        new_lines.append(line)

    for key, val in updates.items():
        if key not in existing_keys:
            new_lines.append(f'{key}="{val}"\n')

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


load_env_file()

APP_TITLE = os.getenv("APP_TITLE", "Live Audio Transmission")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(16))

HOTSPOT_ENABLED = os.getenv("HOTSPOT_ENABLED", "true").lower() == "true"
HOTSPOT_NAME = os.getenv("HOTSPOT_NAME", "Live Feed")
HOTSPOT_MODE = os.getenv("HOTSPOT_MODE", "password")
HOTSPOT_PASSWORD = os.getenv("HOTSPOT_PASSWORD", "12345678")

WIFI_INTERFACE = os.getenv("WIFI_INTERFACE", "auto").strip()


def detect_wifi_interface():
    try:
        output = subprocess.check_output(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"],
            text=True,
            timeout=5,
        )
        for line in output.splitlines():
            parts = line.split(":")
            if len(parts) >= 3:
                device, dev_type, state = parts[0], parts[1], parts[2]
                if dev_type == "wifi" and device:
                    print(f"[*] Auto-detected WiFi interface: {device}")
                    return device
    except Exception as e:
        print(f"[!] Could not auto-detect WiFi interface: {e}")

    return "wlan0"


if WIFI_INTERFACE.lower() == "auto":
    WIFI_INTERFACE = detect_wifi_interface()

SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8000"))

AUDIO_RATE = int(os.getenv("AUDIO_RATE", "48000"))
AUDIO_FRAME_SAMPLES = int(os.getenv("AUDIO_FRAME_SAMPLES", "960"))
CHANNELS = int(os.getenv("CHANNELS", "1"))
FORMAT = pyaudio.paInt16

VOLUME_MULTIPLIER = float(os.getenv("VOLUME_MULTIPLIER", "1.0"))
NOISE_GATE = int(os.getenv("NOISE_GATE", "0"))

_selected_input = os.getenv("SELECTED_INPUT_INDEX", "").strip()
SELECTED_INPUT_INDEX: Optional[int] = int(_selected_input) if _selected_input else None

# =========================================================
# LOGGING / GLOBALS
# =========================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("live-feed-webrtc")

app = FastAPI(title=APP_TITLE)
pcs = set()

# =========================================================
# AUTHENTICATION HELPERS
# =========================================================

COOKIE_NAME = "livefeed_admin_session"


def create_session_token(password: str) -> str:
    return hmac.new(SECRET_KEY.encode(), password.encode(), hashlib.sha256).hexdigest()


def is_authenticated(request: Request) -> bool:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return False
    expected = create_session_token(ADMIN_PASSWORD)
    return hmac.compare_digest(token, expected)


# =========================================================
# SYSTEM & NETWORK HELPERS
# =========================================================

def run_cmd(cmd, check=True):
    print(f"[*] Running: {' '.join(cmd)}")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.stdout.strip():
        print("[stdout]", res.stdout.strip())
    if res.stderr.strip():
        print("[stderr]", res.stderr.strip())
    if check and res.returncode != 0:
        raise RuntimeError(
            f"Command failed with code {res.returncode}: {' '.join(cmd)}\n"
            f"STDOUT: {res.stdout.strip()}\n"
            f"STDERR: {res.stderr.strip()}"
        )
    return res


def sudo_cmd(args):
    if os.geteuid() == 0:
        return args
    return ["sudo"] + args


def get_hotspot_ip():
    try:
        output = subprocess.check_output(
            ["nmcli", "-g", "IP4.ADDRESS", "device", "show", WIFI_INTERFACE],
            text=True,
            timeout=3,
        )
        for line in output.splitlines():
            if line.strip():
                return line.split("/")[0].strip()
    except Exception as e:
        logger.debug("Could not detect IP via nmcli: %s", e)

    try:
        output = subprocess.check_output(["hostname", "-I"], text=True, timeout=2)
        ips = output.strip().split()
        if ips:
            return ips[0]
    except Exception:
        pass

    return "10.42.0.1"


def get_wifi_status():
    ifname = WIFI_INTERFACE
    state = "unknown"
    current_ssid = ""
    signal = 0

    try:
        res = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        for line in res.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 4 and parts[0] == ifname:
                state = parts[2]
                current_ssid = parts[3]
                break

        if current_ssid and current_ssid != "--":
            sig_res = subprocess.run(
                ["nmcli", "-t", "-f", "IN-USE,SIGNAL", "dev", "wifi", "list"],
                capture_output=True,
                text=True,
                timeout=4,
            )
            for line in sig_res.stdout.splitlines():
                if line.startswith("*"):
                    sig_parts = line.split(":")
                    if len(sig_parts) >= 2 and sig_parts[1].isdigit():
                        signal = int(sig_parts[1])
                    break
    except Exception as e:
        logger.warning("Wi-Fi status error: %s", e)

    return {
        "interface": ifname,
        "state": state,
        "connected_ssid": current_ssid if current_ssid != "--" else "",
        "signal": signal,
        "ip_address": get_hotspot_ip(),
        "hotspot_enabled": HOTSPOT_ENABLED,
        "hotspot_name": HOTSPOT_NAME,
        "hotspot_mode": HOTSPOT_MODE,
    }


def scan_wifi_networks():
    networks = {}
    try:
        res = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE", "dev", "wifi", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in res.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 4:
                ssid = parts[0].strip()
                if not ssid or ssid == "--":
                    continue
                signal = int(parts[1]) if parts[1].isdigit() else 0
                security = parts[2].strip() or "Open"
                in_use = parts[3].strip() == "*"

                if ssid not in networks or signal > networks[ssid]["signal"]:
                    networks[ssid] = {
                        "ssid": ssid,
                        "signal": signal,
                        "security": security,
                        "in_use": in_use,
                    }
    except Exception as e:
        logger.warning("Wi-Fi scan failed: %s", e)

    return sorted(list(networks.values()), key=lambda x: x["signal"], reverse=True)


def connect_to_wifi(ssid: str, password: str = ""):
    ifname = WIFI_INTERFACE

    # 1. Try immediate live connection if network is broadcasting
    cmd = ["nmcli", "dev", "wifi", "connect", ssid]
    if password:
        cmd.extend(["password", password])
    if ifname:
        cmd.extend(["ifname", ifname])

    res = subprocess.run(sudo_cmd(cmd), capture_output=True, text=True, timeout=12)
    if res.returncode == 0:
        return {"success": True, "message": f"Connected to {ssid}"}

    # 2. If network is currently off (e.g. phone hotspot), save profile offline with autoconnect
    subprocess.run(sudo_cmd(["nmcli", "connection", "delete", ssid]), capture_output=True, text=True)

    add_cmd = [
        "nmcli", "connection", "add",
        "type", "wifi",
        "con-name", ssid,
        "ifname", ifname,
        "ssid", ssid,
        "autoconnect", "yes",
    ]
    if password:
        add_cmd.extend(["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password])
    else:
        add_cmd.extend(["wifi-sec.key-mgmt", "none"])

    add_res = subprocess.run(sudo_cmd(add_cmd), capture_output=True, text=True, timeout=10)
    if add_res.returncode == 0:
        return {
            "success": True,
            "message": f"Saved Wi-Fi profile for '{ssid}'. When you turn on your Personal Hotspot, the server will connect automatically!",
        }

    return {"success": False, "error": res.stderr.strip() or res.stdout.strip()}


def validate_hotspot_config():
    mode = HOTSPOT_MODE.lower().strip()
    if mode not in ("open", "password"):
        raise ValueError('HOTSPOT_MODE must be "open" or "password".')
    if mode == "password" and len(HOTSPOT_PASSWORD) < 8:
        raise ValueError("HOTSPOT_PASSWORD must be at least 8 characters.")


def wifi_password_label():
    if HOTSPOT_MODE.lower().strip() == "password":
        return HOTSPOT_PASSWORD
    return "No password"


def create_open_hotspot():
    run_cmd(sudo_cmd([
        "nmcli", "connection", "add",
        "type", "wifi",
        "ifname", WIFI_INTERFACE,
        "con-name", HOTSPOT_NAME,
        "autoconnect", "no",
        "ssid", HOTSPOT_NAME,
    ]))
    run_cmd(sudo_cmd([
        "nmcli", "connection", "modify", HOTSPOT_NAME,
        "802-11-wireless.mode", "ap",
        "802-11-wireless.band", "bg",
        "ipv4.method", "shared",
        "ipv6.method", "ignore",
    ]))
    run_cmd(sudo_cmd([
        "nmcli", "connection", "modify", HOTSPOT_NAME,
        "802-11-wireless-security.key-mgmt", "",
    ]))
    run_cmd(sudo_cmd(["nmcli", "connection", "up", HOTSPOT_NAME]))


def create_password_hotspot():
    run_cmd(sudo_cmd([
        "nmcli", "device", "wifi", "hotspot",
        "ifname", WIFI_INTERFACE,
        "ssid", HOTSPOT_NAME,
        "password", HOTSPOT_PASSWORD,
    ]))


def setup_hotspot():
    if not HOTSPOT_ENABLED:
        print("[*] Hotspot disabled. Skipping WiFi setup.")
        return True

    try:
        validate_hotspot_config()
        print()
        print("=" * 60)
        print(" Setting up WiFi hotspot")
        print("=" * 60)
        print(f"SSID:      {HOTSPOT_NAME}")
        print(f"Mode:      {HOTSPOT_MODE}")
        print(f"Interface: {WIFI_INTERFACE}")
        print("=" * 60)
        print()

        run_cmd(sudo_cmd(["nmcli", "connection", "down", HOTSPOT_NAME]), check=False)
        run_cmd(sudo_cmd(["nmcli", "connection", "delete", HOTSPOT_NAME]), check=False)
        run_cmd(sudo_cmd(["nmcli", "radio", "wifi", "on"]), check=False)

        if HOTSPOT_MODE.lower().strip() == "open":
            create_open_hotspot()
        else:
            create_password_hotspot()

        time.sleep(2)
        run_cmd(["nmcli", "connection", "show"], check=False)
        run_cmd(["nmcli", "device", "status"], check=False)
        return True

    except Exception as e:
        print()
        print("[!] Hotspot setup failed:")
        print(e)
        print()
        return False


# =========================================================
# SHARED MICROPHONE SOURCE & AUDIO ENGINE
# =========================================================

class SharedMicrophoneSource:
    """
    Opens the microphone ONCE and broadcasts frames to all connected listeners.
    Also computes live Peak/RMS metrics for the admin level meter.
    """

    def __init__(self):
        self._pyaudio = None
        self._stream = None
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._subscribers = set()
        self._running = False
        self._last_peak = 0.0
        self._last_rms = 0.0

    def register(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        with self._lock:
            self._subscribers.add((loop, queue))
            logger.info("Audio subscriber added. Total: %s", len(self._subscribers))
            if not self._running:
                self._start_locked()

    def unregister(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        with self._lock:
            self._subscribers.discard((loop, queue))
            logger.info("Audio subscriber removed. Total: %s", len(self._subscribers))
            if not self._subscribers:
                self._stop_locked()

    def _start_locked(self):
        self._stop_event.clear()
        self._pyaudio = pyaudio.PyAudio()

        try:
            if SELECTED_INPUT_INDEX is None:
                mic = self._pyaudio.get_default_input_device_info()
            else:
                mic = self._pyaudio.get_device_info_by_index(SELECTED_INPUT_INDEX)

            print()
            print("=" * 60)
            print(" Audio Device Selected")
            print("=" * 60)
            print(f"Index:        {mic['index']}")
            print(f"Name:         {mic['name']}")
            print(f"Channels:     {mic['maxInputChannels']}")
            print(f"Sample Rate:  {mic['defaultSampleRate']}")
            print("=" * 60)
            print()
        except Exception as e:
            logger.warning("Could not display microphone info: %s", e)

        logger.info(
            "Opening shared microphone: device=%s rate=%s frame_samples=%s",
            SELECTED_INPUT_INDEX,
            AUDIO_RATE,
            AUDIO_FRAME_SAMPLES,
        )

        self._stream = self._pyaudio.open(
            format=FORMAT,
            channels=CHANNELS,
            rate=AUDIO_RATE,
            input=True,
            input_device_index=SELECTED_INPUT_INDEX,
            frames_per_buffer=AUDIO_FRAME_SAMPLES,
        )

        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._running = True
        self._thread.start()

    def _stop_locked(self):
        logger.info("Stopping shared microphone")
        self._stop_event.set()
        self._running = False
        self._last_peak = 0.0
        self._last_rms = 0.0

        thread = self._thread
        stream = self._stream
        pyaudio_instance = self._pyaudio

        self._thread = None
        self._stream = None
        self._pyaudio = None

        try:
            if thread and thread.is_alive():
                thread.join(timeout=1.0)
        except Exception as e:
            logger.warning("Error stopping mic thread: %s", e)

        try:
            if stream:
                stream.stop_stream()
                stream.close()
        except Exception as e:
            logger.warning("Error closing mic stream: %s", e)

        try:
            if pyaudio_instance:
                pyaudio_instance.terminate()
        except Exception as e:
            logger.warning("Error terminating PyAudio: %s", e)

    def _capture_loop(self):
        global VOLUME_MULTIPLIER, NOISE_GATE
        while not self._stop_event.is_set():
            try:
                data = self._stream.read(AUDIO_FRAME_SAMPLES, exception_on_overflow=False)
                audio_data = np.frombuffer(data, dtype=np.int16).copy()

                if len(audio_data) > 0:
                    float_data = audio_data.astype(np.float32)
                    self._last_peak = float(np.max(np.abs(float_data))) / 32768.0
                    self._last_rms = float(np.sqrt(np.mean(float_data**2))) / 32768.0

                if NOISE_GATE > 0:
                    audio_data[np.abs(audio_data) < NOISE_GATE] = 0

                if VOLUME_MULTIPLIER != 1.0:
                    audio_data = np.clip(
                        audio_data.astype(np.float32) * VOLUME_MULTIPLIER,
                        -32768,
                        32767,
                    ).astype(np.int16)

                final_data = audio_data.tobytes()

                with self._lock:
                    subscribers = list(self._subscribers)

                for loop, queue in subscribers:
                    loop.call_soon_threadsafe(self._push_frame, queue, final_data)

            except Exception as e:
                logger.exception("Microphone capture error: %s", e)
                time.sleep(0.05)

    @staticmethod
    def _push_frame(queue: asyncio.Queue, data: bytes):
        try:
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(data)
        except Exception:
            pass

    def get_meter(self):
        peak_pct = int(min(self._last_peak * 100, 100))
        db = 20 * np.log10(max(self._last_peak, 1e-4))
        return {
            "peak_pct": peak_pct,
            "db": round(float(db), 1),
            "active": self._running,
        }

    def update_audio_settings(
        self,
        volume: Optional[float] = None,
        noise_gate: Optional[int] = None,
        input_index: Optional[int] = -1,
    ):
        global VOLUME_MULTIPLIER, NOISE_GATE, SELECTED_INPUT_INDEX
        if volume is not None:
            VOLUME_MULTIPLIER = float(volume)
        if noise_gate is not None:
            NOISE_GATE = int(noise_gate)

        if input_index != -1:
            SELECTED_INPUT_INDEX = input_index
            with self._lock:
                if self._running:
                    self._stop_locked()
                    self._start_locked()

    def stop_all(self):
        with self._lock:
            self._subscribers.clear()
            if self._running:
                self._stop_locked()


shared_microphone = SharedMicrophoneSource()


class MicrophoneAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.sample_rate = AUDIO_RATE
        self.frame_samples = AUDIO_FRAME_SAMPLES
        self._timestamp = 0
        self._queue = asyncio.Queue(maxsize=1)  # Minimal buffer for ultra-low latency
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        shared_microphone.register(self._loop, self._queue)

    async def recv(self):
        data = await self._queue.get()
        audio_data = np.frombuffer(data, dtype=np.int16).copy().reshape(1, -1)

        frame = av.AudioFrame.from_ndarray(audio_data, format="s16", layout="mono")
        frame.sample_rate = self.sample_rate
        frame.pts = self._timestamp
        frame.time_base = Fraction(1, self.sample_rate)

        self._timestamp += self.frame_samples
        return frame

    def stop(self):
        if not self._stopped:
            self._stopped = True
            logger.info("Stopping client audio track")
            shared_microphone.unregister(self._loop, self._queue)
        super().stop()


# =========================================================
# ACCESSIBLE SENIOR-FRIENDLY LISTENER HTML
# =========================================================

INDEX_HTML = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{{APP_TITLE}}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            align-items: center;
            padding: 24px 16px;
            text-align: center;
            -webkit-font-smoothing: antialiased;
        }

        .container {
            width: 100%;
            max-width: 440px;
            display: flex;
            flex-direction: column;
            align-items: center;
            flex-grow: 1;
            justify-content: center;
        }

        .header {
            margin-bottom: 24px;
        }

        h1 {
            font-size: 26px;
            font-weight: 800;
            letter-spacing: -0.5px;
            color: #ffffff;
            margin-bottom: 6px;
        }

        .subtitle {
            font-size: 16px;
            color: #94a3b8;
        }

        .headphone-banner {
            background: rgba(30, 41, 59, 0.8);
            border: 1px solid #334155;
            border-radius: 999px;
            padding: 10px 20px;
            font-size: 15px;
            color: #cbd5e1;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 32px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.2);
        }

        /* Giant Main Action Button */
        .btn-wrapper {
            position: relative;
            margin: 16px 0 28px 0;
        }

        .pulse-ring {
            position: absolute;
            top: -12px;
            left: -12px;
            right: -12px;
            bottom: -12px;
            border-radius: 50%;
            background: rgba(34, 197, 94, 0.25);
            opacity: 0;
            pointer-events: none;
            transition: all 0.3s;
        }

        .pulse-ring.active {
            animation: pulse 2s infinite;
        }

        @keyframes pulse {
            0% { transform: scale(0.95); opacity: 0.8; }
            50% { transform: scale(1.15); opacity: 0.1; }
            100% { transform: scale(0.95); opacity: 0.8; }
        }

        #actionBtn {
            width: 210px;
            height: 210px;
            border-radius: 50%;
            border: 5px solid rgba(255, 255, 255, 0.15);
            background: #16a34a;
            color: #ffffff;
            font-size: 22px;
            font-weight: 800;
            cursor: pointer;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            gap: 10px;
            box-shadow: 0 10px 30px rgba(22, 163, 74, 0.45), inset 0 2px 4px rgba(255,255,255,0.3);
            transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            user-select: none;
            -webkit-tap-highlight-color: transparent;
        }

        #actionBtn:active {
            transform: scale(0.96);
            box-shadow: 0 4px 15px rgba(22, 163, 74, 0.3);
        }

        #actionBtn.connecting {
            background: #d97706;
            box-shadow: 0 10px 30px rgba(217, 119, 6, 0.45);
        }

        #actionBtn.playing {
            background: #dc2626;
            box-shadow: 0 10px 30px rgba(220, 38, 38, 0.45);
        }

        .btn-icon {
            font-size: 48px;
            line-height: 1;
        }

        .btn-text {
            font-size: 19px;
            letter-spacing: 0.5px;
        }

        /* Status & Sound Wave */
        .status-pill {
            font-size: 17px;
            font-weight: 600;
            color: #cbd5e1;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .status-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #64748b;
        }

        .status-dot.live {
            background: #22c55e;
            box-shadow: 0 0 12px #22c55e;
        }

        .wave-container {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 5px;
            height: 24px;
            margin-bottom: 28px;
            opacity: 0;
            transition: opacity 0.3s;
        }

        .wave-container.visible {
            opacity: 1;
        }

        .wave-bar {
            width: 5px;
            background: #22c55e;
            border-radius: 999px;
            animation: wave 1.2s ease-in-out infinite alternate;
        }

        .wave-bar:nth-child(1) { height: 10px; animation-delay: 0.1s; }
        .wave-bar:nth-child(2) { height: 18px; animation-delay: 0.3s; }
        .wave-bar:nth-child(3) { height: 26px; animation-delay: 0.2s; }
        .wave-bar:nth-child(4) { height: 16px; animation-delay: 0.4s; }
        .wave-bar:nth-child(5) { height: 10px; animation-delay: 0.15s; }

        @keyframes wave {
            0% { transform: scaleY(0.4); opacity: 0.5; }
            100% { transform: scaleY(1.2); opacity: 1; }
        }

        /* Large Volume Control */
        .volume-card {
            width: 100%;
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 20px;
            padding: 18px 24px;
            display: flex;
            flex-direction: column;
            gap: 12px;
            box-shadow: 0 4px 15px rgba(0,0,0,0.2);
        }

        .volume-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 15px;
            font-weight: 600;
            color: #94a3b8;
        }

        .volume-slider-row {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .vol-icon {
            font-size: 24px;
            color: #94a3b8;
            user-select: none;
        }

        input[type=range] {
            flex-grow: 1;
            height: 10px;
            border-radius: 999px;
            background: #334155;
            outline: none;
            -webkit-appearance: none;
        }

        input[type=range]::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 32px;
            height: 32px;
            border-radius: 50%;
            background: #38bdf8;
            cursor: pointer;
            box-shadow: 0 2px 8px rgba(0,0,0,0.4);
        }

        /* Footer */
        footer {
            margin-top: 24px;
            font-size: 14px;
            color: #64748b;
            display: flex;
            justify-content: space-between;
            align-items: center;
            width: 100%;
            max-width: 440px;
        }

        footer a {
            color: #94a3b8;
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 6px 10px;
            border-radius: 8px;
            transition: all 0.2s;
        }

        footer a:hover {
            color: #f8fafc;
            background: rgba(255,255,255,0.08);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{{APP_TITLE}}</h1>
            <div class="subtitle">Live Assistive Listening</div>
        </div>

        <div class="headphone-banner">
            <span>🎧</span> Put in your earbuds or headphones
        </div>

        <div class="btn-wrapper">
            <div class="pulse-ring" id="pulseRing"></div>
            <button id="actionBtn" type="button">
                <span class="btn-icon" id="btnIcon">▶️</span>
                <span class="btn-text" id="btnText">TAP TO LISTEN</span>
            </button>
        </div>

        <div class="status-pill">
            <span class="status-dot" id="statusDot"></span>
            <span id="statusText">Ready to listen</span>
        </div>

        <div class="wave-container" id="waveContainer">
            <div class="wave-bar"></div>
            <div class="wave-bar"></div>
            <div class="wave-bar"></div>
            <div class="wave-bar"></div>
            <div class="wave-bar"></div>
        </div>

        <div class="volume-card">
            <div class="volume-header">
                <span>Listening Volume</span>
                <span id="volValue">100%</span>
            </div>
            <div class="volume-slider-row">
                <span class="vol-icon">🔉</span>
                <input id="volSlider" type="range" min="0" max="100" value="100">
                <span class="vol-icon">🔊</span>
            </div>
        </div>

        <audio id="audioEl" autoplay playsinline style="display:none"></audio>
    </div>

    <footer>
        <span>Need help? Ask an usher</span>
        <a href="/admin">⚙️ Admin</a>
    </footer>

    <script>
        const btn = document.getElementById("actionBtn");
        const btnIcon = document.getElementById("btnIcon");
        const btnText = document.getElementById("btnText");
        const pulseRing = document.getElementById("pulseRing");
        const statusDot = document.getElementById("statusDot");
        const statusText = document.getElementById("statusText");
        const waveContainer = document.getElementById("waveContainer");
        const volSlider = document.getElementById("volSlider");
        const volValue = document.getElementById("volValue");
        const audioEl = document.getElementById("audioEl");

        let pc = null;
        let wakeLock = null;
        let isPlaying = false;

        async function acquireWakeLock() {
            try {
                if ('wakeLock' in navigator) {
                    wakeLock = await navigator.wakeLock.request('screen');
                }
            } catch (err) {
                console.log("WakeLock error:", err);
            }
        }

        function releaseWakeLock() {
            if (wakeLock) {
                wakeLock.release().catch(() => {});
                wakeLock = null;
            }
        }

        volSlider.oninput = function() {
            const val = this.value;
            volValue.textContent = val + "%";
            audioEl.volume = val / 100.0;
        };

        btn.onclick = async function() {
            if (navigator.vibrate) navigator.vibrate(40);

            if (isPlaying) {
                stopPlayback();
            } else {
                startPlayback();
            }
        };

        async function startPlayback() {
            btn.className = "connecting";
            btnIcon.textContent = "⏳";
            btnText.textContent = "CONNECTING...";
            statusText.textContent = "Connecting to audio...";
            statusDot.className = "status-dot";

            try {
                await acquireWakeLock();
                await startWebRTC();
            } catch (err) {
                console.error(err);
                stopPlayback();
                statusText.textContent = "Could not connect. Tap to retry.";
            }
        }

        function stopPlayback() {
            isPlaying = false;
            releaseWakeLock();

            if (pc) {
                pc.close();
                pc = null;
            }

            audioEl.srcObject = null;
            btn.className = "";
            btnIcon.textContent = "▶️";
            btnText.textContent = "TAP TO LISTEN";
            pulseRing.className = "pulse-ring";
            statusDot.className = "status-dot";
            statusText.textContent = "Ready to listen";
            waveContainer.className = "wave-container";
        }

        async function startWebRTC() {
            if (pc) {
                pc.close();
                pc = null;
            }

            pc = new RTCPeerConnection({ iceServers: [] });

            pc.ontrack = function (event) {
                audioEl.srcObject = event.streams[0];
                audioEl.volume = volSlider.value / 100.0;
                audioEl.play().catch(e => console.log("Audio play caught:", e));
            };

            pc.onconnectionstatechange = function () {
                if (pc.connectionState === "connected") {
                    isPlaying = true;
                    btn.className = "playing";
                    btnIcon.textContent = "⏹️";
                    btnText.textContent = "TAP TO STOP";
                    pulseRing.className = "pulse-ring active";
                    statusDot.className = "status-dot live";
                    statusText.textContent = "● Sound is Live";
                    waveContainer.className = "wave-container visible";
                } else if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
                    stopPlayback();
                }
            };

            pc.addTransceiver("audio", { direction: "recvonly" });

            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);

            const response = await fetch("/offer", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    sdp: pc.localDescription.sdp,
                    type: pc.localDescription.type
                })
            });

            if (!response.ok) {
                throw new Error("Server error: " + response.status);
            }

            const answer = await response.json();
            await pc.setRemoteDescription(answer);
        }
    </script>
</body>
</html>
"""

# =========================================================
# ADMIN CONSOLE HTML
# =========================================================

ADMIN_HTML = """
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{{APP_TITLE}} — Admin Console</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background: #0f172a;
            color: #f8fafc;
            padding: 20px;
            display: flex;
            justify-content: center;
        }

        .admin-card {
            width: 100%;
            max-width: 680px;
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 20px;
            padding: 24px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid #334155;
            padding-bottom: 18px;
            margin-bottom: 20px;
        }

        h1 { font-size: 22px; font-weight: 800; }
        .back-link { color: #38bdf8; text-decoration: none; font-size: 14px; font-weight: 600; }

        /* Tabs */
        .tabs {
            display: flex;
            gap: 8px;
            border-bottom: 1px solid #334155;
            margin-bottom: 24px;
        }

        .tab-btn {
            padding: 10px 18px;
            background: transparent;
            border: none;
            color: #94a3b8;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            border-bottom: 2px solid transparent;
            transition: all 0.2s;
        }

        .tab-btn.active {
            color: #38bdf8;
            border-bottom-color: #38bdf8;
        }

        .tab-content { display: none; }
        .tab-content.active { display: block; }

        /* Cards & Sections */
        .section {
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 14px;
            padding: 18px;
            margin-bottom: 20px;
        }

        .section-title {
            font-size: 16px;
            font-weight: 700;
            margin-bottom: 14px;
            color: #f1f5f9;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .row {
            display: flex;
            justify-content: space-between;
            margin-bottom: 10px;
            font-size: 14px;
            color: #cbd5e1;
        }

        .row strong { color: #f8fafc; }

        /* Forms & Buttons */
        button.btn {
            padding: 10px 18px;
            border-radius: 10px;
            border: none;
            font-weight: 700;
            font-size: 14px;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-primary { background: #0284c7; color: white; }
        .btn-primary:hover { background: #0369a1; }
        .btn-success { background: #16a34a; color: white; }
        .btn-danger { background: #dc2626; color: white; }
        .btn-secondary { background: #334155; color: #f8fafc; }

        .form-group {
            margin-bottom: 16px;
        }

        label {
            display: block;
            font-size: 13px;
            font-weight: 600;
            color: #94a3b8;
            margin-bottom: 6px;
        }

        select, input[type=text], input[type=password], input[type=number] {
            width: 100%;
            padding: 10px 14px;
            background: #1e293b;
            border: 1px solid #475569;
            border-radius: 10px;
            color: #f8fafc;
            font-size: 14px;
            outline: none;
        }

        select:focus, input:focus { border-color: #38bdf8; }

        /* VU Meter */
        .meter-container {
            width: 100%;
            height: 24px;
            background: #1e293b;
            border: 1px solid #475569;
            border-radius: 12px;
            overflow: hidden;
            margin-bottom: 8px;
            position: relative;
        }

        .meter-fill {
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, #22c55e 65%, #eab308 85%, #ef4444 95%);
            transition: width 0.1s linear;
        }

        /* Wi-Fi Table */
        .wifi-list {
            margin-top: 12px;
            max-height: 220px;
            overflow-y: auto;
        }

        .wifi-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px 12px;
            border-bottom: 1px solid #1e293b;
            font-size: 14px;
        }

        .wifi-item:last-child { border-bottom: none; }
        .wifi-ssid { font-weight: 600; display: flex; align-items: center; gap: 8px; }

        /* Login Modal */
        #loginCard {
            text-align: center;
            padding: 30px 10px;
        }
    </style>
</head>
<body>
    <div class="admin-card">
        <div class="header">
            <h1>⚙️ Live Feed Console</h1>
            <a href="/" class="back-link">🎧 Listener View</a>
        </div>

        <div id="loginSection" style="display:none;">
            <div id="loginCard">
                <h2 style="margin-bottom:12px">Admin Authentication</h2>
                <p style="color:#94a3b8; font-size:14px; margin-bottom:20px">Enter your administrator password to configure settings.</p>
                <div style="max-width:320px; margin:auto;">
                    <input type="password" id="adminPassInput" placeholder="Password" style="margin-bottom:14px">
                    <button class="btn btn-primary" style="width:100%" onclick="login()">Log In</button>
                    <div id="loginError" style="color:#ef4444; font-size:13px; margin-top:10px;"></div>
                </div>
            </div>
        </div>

        <div id="adminSection">
            <div class="tabs">
                <button class="tab-btn active" onclick="showTab('wifiTab', this)">📶 Wi-Fi & Network</button>
                <button class="tab-btn" onclick="showTab('audioTab', this)">🎙️ Audio Settings</button>
                <button class="tab-btn" onclick="showTab('systemTab', this)">📊 System</button>
            </div>

            <!-- TAB 1: WI-FI -->
            <div id="wifiTab" class="tab-content active">
                <div class="section" style="background: linear-gradient(135deg, #1e293b, #0f172a); border-color: #0284c7;">
                    <div class="section-title" style="color: #38bdf8; margin-bottom: 12px;">
                        <span>📍 Broadcast Access URLs</span>
                    </div>
                    
                    <div style="background: #0f172a; padding: 12px 14px; border-radius: 10px; margin-bottom: 10px; border: 1px solid #334155;">
                        <div style="font-size: 12px; color: #94a3b8; margin-bottom: 4px; font-weight: 600;">🎧 LISTENER URL (Share with audience):</div>
                        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px;">
                            <strong id="listenerUrlDisplay" style="color: #22c55e; font-size: 16px; word-break: break-all;">http://--:8000</strong>
                            <button class="btn btn-secondary" style="padding: 4px 10px; font-size: 12px;" onclick="copyUrl('listenerUrlDisplay')">📋 Copy</button>
                        </div>
                        <div style="font-size: 12px; color: #64748b; margin-top: 5px;">On Raspberry Pi: <span style="color:#94a3b8">http://livefeed.local:8000</span></div>
                    </div>

                    <div style="background: #0f172a; padding: 10px 14px; border-radius: 10px; border: 1px solid #334155;">
                        <div style="font-size: 12px; color: #94a3b8; margin-bottom: 4px; font-weight: 600;">⚙️ ADMIN CONSOLE URL:</div>
                        <div style="display: flex; justify-content: space-between; align-items: center; gap: 8px;">
                            <strong id="adminUrlDisplay" style="color: #38bdf8; font-size: 14px; word-break: break-all;">http://--:8000/admin</strong>
                            <button class="btn btn-secondary" style="padding: 4px 10px; font-size: 12px;" onclick="copyUrl('adminUrlDisplay')">📋 Copy</button>
                        </div>
                    </div>
                </div>

                <div class="section">
                    <div class="section-title">Current Connection</div>
                    <div class="row"><span>Status:</span> <strong id="wfState">Loading...</strong></div>
                    <div class="row"><span>Connected SSID:</span> <strong id="wfSsid">--</strong></div>
                    <div class="row"><span>Local IP Address:</span> <strong id="wfIp">--</strong></div>
                    <div class="row"><span>Signal Strength:</span> <strong id="wfSignal">--</strong></div>
                </div>

                <div class="section">
                    <div class="section-title">
                        <span>Nearby Wi-Fi Networks</span>
                        <div style="display:flex; gap:6px;">
                            <button class="btn btn-secondary" style="padding:6px 12px; font-size:13px;" onclick="openManualWifiModal()">➕ Add Manual</button>
                            <button class="btn btn-primary" style="padding:6px 12px; font-size:13px;" onclick="scanWifi()">🔍 Scan</button>
                        </div>
                    </div>
                    <div class="wifi-list" id="wifiList">
                        <div style="color:#94a3b8; text-align:center; padding:15px;">Tap Scan to detect nearby Wi-Fi networks</div>
                    </div>
                </div>

                <div class="section">
                    <div class="section-title">Hotspot Access Point</div>
                    <div class="row"><span>Hotspot Enabled:</span> <strong id="hsEnabled">--</strong></div>
                    <div class="row"><span>Hotspot Name (SSID):</span> <strong id="hsName">--</strong></div>
                    <div class="row"><span>Hotspot Mode:</span> <strong id="hsMode">--</strong></div>
                </div>
            </div>

            <!-- TAB 2: AUDIO -->
            <div id="audioTab" class="tab-content">
                <div class="section">
                    <div class="section-title">Live Audio Level (VU Meter)</div>
                    <div class="meter-container">
                        <div class="meter-fill" id="vuBar"></div>
                    </div>
                    <div class="row"><span>Peak Level:</span> <strong id="vuPeak">0%</strong></div>
                    <div class="row"><span>Decibels:</span> <strong id="vuDb">-inf dB</strong></div>
                </div>

                <div class="section">
                    <div class="section-title">Microphone & Processing</div>
                    <div class="form-group">
                        <label>Input Device (Microphone):</label>
                        <select id="audioDeviceSelect"></select>
                    </div>

                    <div class="form-group">
                        <label>Volume Boost Multiplier (<span id="volBoostLabel">1.0x</span>):</label>
                        <input type="range" id="volBoostSlider" min="0.5" max="3.0" step="0.1" value="1.0" oninput="document.getElementById('volBoostLabel').textContent=this.value+'x'">
                    </div>

                    <div class="form-group">
                        <label>Noise Gate Threshold (<span id="gateLabel">0</span>):</label>
                        <input type="range" id="gateSlider" min="0" max="800" step="25" value="0" oninput="document.getElementById('gateLabel').textContent=this.value">
                    </div>

                    <button class="btn btn-success" style="width:100%" onclick="saveAudioSettings()">💾 Save Audio Settings</button>
                </div>
            </div>

            <!-- TAB 3: SYSTEM -->
            <div id="systemTab" class="tab-content">
                <div class="section">
                    <div class="section-title">Broadcast Statistics</div>
                    <div class="row"><span>Active Listeners:</span> <strong id="sysClients">0</strong></div>
                    <div class="row"><span>Sample Rate:</span> <strong id="sysRate">48000 Hz</strong></div>
                    <div class="row"><span>Frame Buffer:</span> <strong id="sysFrame">960 samples (20ms)</strong></div>
                </div>

                <div class="section">
                    <div class="section-title">System Actions</div>
                    <div style="display:flex; gap:10px; flex-direction:column;">
                        <button class="btn btn-secondary" onclick="restartService()">🔄 Restart Audio Service</button>
                        <button class="btn btn-danger" onclick="rebootPi()">🔌 Reboot Raspberry Pi</button>
                        <button class="btn btn-secondary" onclick="logout()">🚪 Log Out</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Wi-Fi Connect Modal -->
    <div id="wifiModal" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.7); justify-content:center; align-items:center; z-index:100;">
        <div style="background:#1e293b; border:1px solid #475569; border-radius:16px; padding:24px; width:90%; max-width:380px;">
            <h3 style="margin-bottom:14px" id="modalTitle">Connect to Wi-Fi</h3>
            
            <div class="form-group">
                <label>Wi-Fi Network Name (SSID):</label>
                <input type="text" id="modalSsidInput" placeholder="e.g. Waina's iPhone or Venue-5G" style="margin-bottom:12px">
            </div>

            <div class="form-group">
                <label>Password (leave blank if open):</label>
                <input type="password" id="modalPassword" placeholder="Wi-Fi Password" style="margin-bottom:14px">
            </div>

            <div style="font-size:12px; color:#94a3b8; margin-bottom:16px; line-height:1.4; background:#0f172a; padding:8px 10px; border-radius:8px;">
                💡 <strong>Tip for Phone Hotspots:</strong> Enter your hotspot name and password here, click <em>Connect</em>, then turn your phone's Personal Hotspot <strong>ON</strong>.
            </div>

            <div style="display:flex; gap:10px;">
                <button class="btn btn-secondary" style="flex:1" onclick="closeWifiModal()">Cancel</button>
                <button class="btn btn-primary" style="flex:1" onclick="submitWifiConnect()">Connect</button>
            </div>
        </div>
    </div>

    <script>
        function showTab(tabId, el) {
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            el.classList.add('active');
        }

        async function checkAuth() {
            const res = await fetch("/api/admin/check");
            const data = await res.json();
            if (data.authenticated) {
                document.getElementById("loginSection").style.display = "none";
                document.getElementById("adminSection").style.display = "block";
                loadDashboardData();
            } else {
                document.getElementById("loginSection").style.display = "block";
                document.getElementById("adminSection").style.display = "none";
            }
        }

        async function login() {
            const pass = document.getElementById("adminPassInput").value;
            const res = await fetch("/api/admin/login", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({password: pass})
            });
            const data = await res.json();
            if (data.success) {
                checkAuth();
            } else {
                document.getElementById("loginError").textContent = data.error || "Invalid password";
            }
        }

        async function logout() {
            await fetch("/api/admin/logout", {method: "POST"});
            checkAuth();
        }

        async function loadDashboardData() {
            loadWifiStatus();
            loadAudioStatus();
            loadStats();
        }

        function copyUrl(elementId) {
            const text = document.getElementById(elementId).textContent.trim();
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(() => {
                    alert("Copied to clipboard:\\n" + text);
                }).catch(() => {
                    prompt("Copy URL:", text);
                });
            } else {
                prompt("Copy URL:", text);
            }
        }

        async function loadWifiStatus() {
            try {
                const res = await fetch("/api/wifi/status");
                const d = await res.json();
                const ip = d.ip_address || window.location.hostname;
                const port = window.location.port ? window.location.port : "8000";
                
                document.getElementById("listenerUrlDisplay").textContent = "http://" + ip + ":" + port;
                document.getElementById("adminUrlDisplay").textContent = "http://" + ip + ":" + port + "/admin";
                
                document.getElementById("wfState").textContent = d.state;
                document.getElementById("wfSsid").textContent = d.connected_ssid || "None (Hotspot / Ethernet)";
                document.getElementById("wfIp").textContent = d.ip_address;
                document.getElementById("wfSignal").textContent = d.signal ? (d.signal + "%") : "--";
                document.getElementById("hsEnabled").textContent = d.hotspot_enabled ? "Yes" : "No";
                document.getElementById("hsName").textContent = d.hotspot_name;
                document.getElementById("hsMode").textContent = d.hotspot_mode;
            } catch(e) { console.error(e); }
        }

        async function scanWifi() {
            const list = document.getElementById("wifiList");
            list.innerHTML = '<div style="color:#38bdf8; text-align:center; padding:15px;">Scanning for networks...</div>';
            try {
                const res = await fetch("/api/wifi/scan");
                const nets = await res.json();
                if (nets.length === 0) {
                    list.innerHTML = '<div style="color:#94a3b8; text-align:center; padding:15px;">No networks found</div>';
                    return;
                }
                list.innerHTML = "";
                nets.forEach(n => {
                    const row = document.createElement("div");
                    row.className = "wifi-item";
                    row.innerHTML = `
                        <div class="wifi-ssid">
                            <span>${n.in_use ? '● ' : ''}${n.ssid}</span>
                            <small style="color:#64748b; font-weight:normal">(${n.signal}%)</small>
                        </div>
                        <button class="btn btn-primary" style="padding:4px 10px; font-size:12px;" onclick="openWifiModal('${n.ssid}')">Connect</button>
                    `;
                    list.appendChild(row);
                });
            } catch(e) {
                list.innerHTML = '<div style="color:#ef4444; text-align:center; padding:15px;">Scan failed</div>';
            }
        }

        function openWifiModal(ssid) {
            document.getElementById("modalTitle").textContent = "Connect to Wi-Fi";
            document.getElementById("modalSsidInput").value = ssid;
            document.getElementById("modalPassword").value = "";
            document.getElementById("wifiModal").style.display = "flex";
            document.getElementById("modalPassword").focus();
        }

        function openManualWifiModal() {
            document.getElementById("modalTitle").textContent = "Add Custom / Hotspot Wi-Fi";
            document.getElementById("modalSsidInput").value = "";
            document.getElementById("modalPassword").value = "";
            document.getElementById("wifiModal").style.display = "flex";
            document.getElementById("modalSsidInput").focus();
        }

        function closeWifiModal() {
            document.getElementById("wifiModal").style.display = "none";
        }

        async function submitWifiConnect() {
            const ssid = document.getElementById("modalSsidInput").value.trim();
            const pass = document.getElementById("modalPassword").value;
            if (!ssid) {
                alert("Please enter a Wi-Fi Network Name (SSID).");
                return;
            }
            closeWifiModal();
            alert("Attempting to connect to " + ssid + ". If this is a phone hotspot, please make sure your hotspot is ON.");
            const res = await fetch("/api/wifi/connect", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({ssid: ssid, password: pass})
            });
            const data = await res.json();
            alert(data.success ? data.message : "Connection attempt returned: " + (data.error || "failed"));
            loadWifiStatus();
        }

        async function loadAudioStatus() {
            try {
                const res = await fetch("/api/audio/status");
                const d = await res.json();
                const sel = document.getElementById("audioDeviceSelect");
                sel.innerHTML = "";
                d.devices.forEach(m => {
                    const opt = document.createElement("option");
                    opt.value = m.index;
                    opt.textContent = `[#${m.index}] ${m.name}`;
                    if (m.index === d.selected_input_index) opt.selected = true;
                    sel.appendChild(opt);
                });
                document.getElementById("volBoostSlider").value = d.volume_multiplier;
                document.getElementById("volBoostLabel").textContent = d.volume_multiplier + "x";
                document.getElementById("gateSlider").value = d.noise_gate;
                document.getElementById("gateLabel").textContent = d.noise_gate;
            } catch(e) { console.error(e); }
        }

        async function saveAudioSettings() {
            const deviceIdx = parseInt(document.getElementById("audioDeviceSelect").value);
            const vol = parseFloat(document.getElementById("volBoostSlider").value);
            const gate = parseInt(document.getElementById("gateSlider").value);

            const res = await fetch("/api/audio/settings", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({selected_input_index: deviceIdx, volume_multiplier: vol, noise_gate: gate})
            });
            const d = await res.json();
            alert(d.success ? "Audio settings saved!" : "Save error: " + d.error);
        }

        async function loadStats() {
            try {
                const res = await fetch("/stats");
                const d = await res.json();
                document.getElementById("sysClients").textContent = d.connected_clients;
                document.getElementById("sysRate").textContent = d.audio_rate + " Hz";
                document.getElementById("sysFrame").textContent = d.frame_samples + " samples (" + Math.round(d.frame_samples/d.audio_rate*1000) + "ms)";
            } catch(e) {}
        }

        // Live VU Meter Polling
        setInterval(async () => {
            if (document.getElementById("audioTab").classList.contains("active")) {
                try {
                    const res = await fetch("/api/audio/meter");
                    const d = await res.json();
                    document.getElementById("vuBar").style.width = d.peak_pct + "%";
                    document.getElementById("vuPeak").textContent = d.peak_pct + "%";
                    document.getElementById("vuDb").textContent = d.db + " dB";
                } catch(e) {}
            }
        }, 150);

        async function restartService() {
            if (confirm("Restart audio service now?")) {
                await fetch("/api/system/restart", {method: "POST"});
                alert("Service restart initiated.");
            }
        }

        async function rebootPi() {
            if (confirm("Are you sure you want to reboot the device?")) {
                await fetch("/api/system/reboot", {method: "POST"});
                alert("Reboot initiated. Device will be offline for ~45 seconds.");
            }
        }

        checkAuth();
    </script>
</body>
</html>
"""

# =========================================================
# ROUTES
# =========================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML.replace("{{APP_TITLE}}", APP_TITLE)


@app.get("/button-test", response_class=HTMLResponse)
async def button_test():
    return HTMLResponse("<h1>Button Test OK</h1>")


@app.get("/admin", response_class=HTMLResponse)
async def admin():
    return ADMIN_HTML.replace("{{APP_TITLE}}", APP_TITLE)


# --- AUTH APIS ---

@app.get("/api/admin/check")
async def api_admin_check(request: Request):
    return JSONResponse({"authenticated": is_authenticated(request)})


@app.post("/api/admin/login")
async def api_admin_login(request: Request, response: Response):
    data = await request.json()
    password = data.get("password", "")
    if hmac.compare_digest(password, ADMIN_PASSWORD):
        token = create_session_token(password)
        res = JSONResponse({"success": True})
        res.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", max_age=86400 * 7)
        return res
    return JSONResponse({"success": False, "error": "Incorrect password"})


@app.post("/api/admin/logout")
async def api_admin_logout(response: Response):
    res = JSONResponse({"success": True})
    res.delete_cookie(COOKIE_NAME)
    return res


# --- WI-FI APIS ---

@app.get("/api/wifi/status")
async def api_wifi_status(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return JSONResponse(get_wifi_status())


@app.get("/api/wifi/scan")
async def api_wifi_scan(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    networks = scan_wifi_networks()
    return JSONResponse(networks)


@app.post("/api/wifi/connect")
async def api_wifi_connect(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    data = await request.json()
    ssid = data.get("ssid", "").strip()
    password = data.get("password", "").strip()
    if not ssid:
        return JSONResponse({"success": False, "error": "SSID is required"})
    result = connect_to_wifi(ssid, password)
    return JSONResponse(result)


# --- AUDIO APIS ---

@app.get("/api/audio/status")
async def api_audio_status(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    p = pyaudio.PyAudio()
    devices = []
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            devices.append({
                "index": i,
                "name": info.get("name"),
                "channels": info.get("maxInputChannels"),
                "defaultSampleRate": info.get("defaultSampleRate"),
            })
    p.terminate()

    return JSONResponse({
        "devices": devices,
        "selected_input_index": SELECTED_INPUT_INDEX,
        "volume_multiplier": VOLUME_MULTIPLIER,
        "noise_gate": NOISE_GATE,
        "audio_rate": AUDIO_RATE,
        "frame_samples": AUDIO_FRAME_SAMPLES,
    })


@app.get("/api/audio/meter")
async def api_audio_meter():
    return JSONResponse(shared_microphone.get_meter())


@app.post("/api/audio/settings")
async def api_audio_settings(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    data = await request.json()
    vol = data.get("volume_multiplier")
    gate = data.get("noise_gate")
    input_idx = data.get("selected_input_index")

    shared_microphone.update_audio_settings(
        volume=vol,
        noise_gate=gate,
        input_index=input_idx if input_idx is not None else -1,
    )

    updates = {}
    if vol is not None:
        updates["VOLUME_MULTIPLIER"] = str(vol)
    if gate is not None:
        updates["NOISE_GATE"] = str(gate)
    if input_idx is not None:
        updates["SELECTED_INPUT_INDEX"] = str(input_idx)

    save_env_values(updates)
    return JSONResponse({"success": True})


# --- SYSTEM APIS ---

@app.get("/mics")
async def mics():
    p = pyaudio.PyAudio()
    devices = []
    for i in range(p.get_device_count()):
        info = p.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            devices.append({
                "index": i,
                "name": info.get("name"),
                "channels": info.get("maxInputChannels"),
                "defaultSampleRate": info.get("defaultSampleRate"),
            })
    p.terminate()
    return JSONResponse(devices)


@app.get("/stats")
async def stats():
    return JSONResponse({
        "connected_clients": len(pcs),
        "audio_rate": AUDIO_RATE,
        "frame_samples": AUDIO_FRAME_SAMPLES,
        "shared_microphone": True,
    })


@app.post("/api/system/restart")
async def api_system_restart(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    subprocess.Popen(["sudo", "systemctl", "restart", "audio-live-feed"])
    return JSONResponse({"success": True})


@app.post("/api/system/reboot")
async def api_system_reboot(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    subprocess.Popen(["sudo", "reboot"])
    return JSONResponse({"success": True})


@app.post("/offer")
async def offer(request: Request):
    logger.info("POST /offer received")
    params = await request.json()

    offer_desc = RTCSessionDescription(
        sdp=params["sdp"],
        type=params["type"],
    )

    pc = RTCPeerConnection()
    pcs.add(pc)
    logger.info("Created peer connection. Total clients: %s", len(pcs))

    audio_track = MicrophoneAudioTrack()
    pc.addTrack(audio_track)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        logger.info("Connection state is %s", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            audio_track.stop()
            await pc.close()
            pcs.discard(pc)
            logger.info("Peer removed. Total clients: %s", len(pcs))

    await pc.setRemoteDescription(offer_desc)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return JSONResponse({
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
    })


@app.on_event("shutdown")
async def on_shutdown():
    coros = [pc.close() for pc in pcs]
    if coros:
        await asyncio.gather(*coros)
    pcs.clear()
    shared_microphone.stop_all()


# =========================================================
# MAIN
# =========================================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=SERVER_HOST)
    parser.add_argument("--port", default=SERVER_PORT, type=int)
    parser.add_argument("-n", "--no-hotspot", "--no_hotspot", "-no-hotspot", dest="no_hotspot", action="store_true", help="Run without setting up a WiFi hotspot")
    args = parser.parse_args()

    if args.no_hotspot:
        print("[*] --no-hotspot specified. Skipping hotspot setup.")
    else:
        ok = setup_hotspot()
        if not ok:
            print("[!] Hotspot setup failed. Exiting.")
            sys.exit(1)

    hotspot_ip = get_hotspot_ip()

    print()
    print("=" * 60)
    print(" Live Audio Transmission Server Running")
    print("=" * 60)
    print(f"Listener URL:    http://{hotspot_ip}:{args.port}")
    print(f"Admin Console:   http://{hotspot_ip}:{args.port}/admin")
    print(f"Audio rate:      {AUDIO_RATE} Hz")
    print(f"Frame samples:   {AUDIO_FRAME_SAMPLES} ({round(AUDIO_FRAME_SAMPLES/AUDIO_RATE*1000)}ms latency)")
    print("=" * 60)
    print()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
