import argparse
import asyncio
import logging
import os
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
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# =========================================================
# CONFIGURATION
# =========================================================

APP_TITLE = "Live Audio Transmission"

HOTSPOT_ENABLED = True
HOTSPOT_NAME = "Live Feed"

# "open" or "password"
HOTSPOT_MODE = "password"
HOTSPOT_PASSWORD = "12345678"

WIFI_INTERFACE = "wlp0s20f3"

SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000

AUDIO_RATE = 48000
AUDIO_FRAME_SAMPLES = 960  # 20 ms at 48 kHz

CHANNELS = 1
FORMAT = pyaudio.paInt16

VOLUME_MULTIPLIER = 1.0
NOISE_GATE = 0

SELECTED_INPUT_INDEX: Optional[int] = None

# =========================================================
# LOGGING / GLOBALS
# =========================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("live-feed-webrtc")

app = FastAPI(title=APP_TITLE)
pcs = set()


# =========================================================
# HOTSPOT HELPERS
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
        "ssid", HOTSPOT_NAME
    ]))

    run_cmd(sudo_cmd([
        "nmcli", "connection", "modify", HOTSPOT_NAME,
        "802-11-wireless.mode", "ap",
        "802-11-wireless.band", "bg",
        "ipv4.method", "shared",
        "ipv6.method", "ignore"
    ]))

    run_cmd(sudo_cmd([
        "nmcli", "connection", "modify", HOTSPOT_NAME,
        "802-11-wireless-security.key-mgmt", ""
    ]))

    run_cmd(sudo_cmd(["nmcli", "connection", "up", HOTSPOT_NAME]))


def create_password_hotspot():
    run_cmd(sudo_cmd([
        "nmcli", "device", "wifi", "hotspot",
        "ifname", WIFI_INTERFACE,
        "ssid", HOTSPOT_NAME,
        "password", HOTSPOT_PASSWORD
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


def get_hotspot_ip():
    try:
        output = subprocess.check_output(
            ["nmcli", "-g", "IP4.ADDRESS", "device", "show", WIFI_INTERFACE],
            text=True
        )

        for line in output.splitlines():
            if line.strip():
                return line.split("/")[0].strip()

    except Exception as e:
        print(f"[!] Could not detect hotspot IP automatically: {e}")

    return "10.42.0.1"


# =========================================================
# SHARED MICROPHONE SOURCE
# =========================================================

class SharedMicrophoneSource:
    """
    Opens the microphone ONCE and broadcasts frames to all connected listeners.
    """

    def __init__(self):
        self._pyaudio = None
        self._stream = None
        self._thread = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._subscribers = set()
        self._running = False

    def register(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        with self._lock:
            self._subscribers.add((loop, queue))
            logger.info("Audio subscriber added. Total subscribers: %s", len(self._subscribers))

            if not self._running:
                self._start_locked()

    def unregister(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        with self._lock:
            self._subscribers.discard((loop, queue))
            logger.info("Audio subscriber removed. Total subscribers: %s", len(self._subscribers))

            if not self._subscribers:
                self._stop_locked()

    def _start_locked(self):
        logger.info(
            "Opening shared microphone: device=%s rate=%s frame_samples=%s",
            SELECTED_INPUT_INDEX,
            AUDIO_RATE,
            AUDIO_FRAME_SAMPLES,
        )

        self._stop_event.clear()
        self._pyaudio = pyaudio.PyAudio()

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

        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:
            pass

        try:
            if self._pyaudio:
                self._pyaudio.terminate()
        except Exception:
            pass

        self._stream = None
        self._pyaudio = None
        self._thread = None

    def _capture_loop(self):
        while not self._stop_event.is_set():
            try:
                data = self._stream.read(AUDIO_FRAME_SAMPLES, exception_on_overflow=False)

                audio_data = np.frombuffer(data, dtype=np.int16).copy()

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
        self._queue = asyncio.Queue(maxsize=3)
        self._loop = asyncio.get_running_loop()
        self._stopped = False

        shared_microphone.register(self._loop, self._queue)

    async def recv(self):
        data = await self._queue.get()

        audio_data = np.frombuffer(data, dtype=np.int16).copy()
        audio_data = audio_data.reshape(1, -1)

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
# HTML
# =========================================================

INDEX_HTML = """
<!doctype html>
<html>
<head>
    <meta charset="utf-8">
    <title>Live Audio Transmission</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    <style>
        body {
            font-family: Arial, sans-serif;
            padding: 24px;
            background: #f3f4f6;
            text-align: center;
        }

        .card {
            background: white;
            max-width: 480px;
            margin: auto;
            padding: 24px;
            border-radius: 16px;
            box-shadow: 0 4px 14px rgba(0,0,0,.12);
        }

        #startButton {
            display: block;
            width: 100%;
            padding: 22px;
            margin-top: 20px;
            font-size: 24px;
            font-weight: bold;
            border: none;
            border-radius: 999px;
            color: white;
            background: #16a34a;
            cursor: pointer;
            touch-action: manipulation;
            pointer-events: auto;
        }

        #startButton:active {
            background: #15803d;
        }

        #status {
            margin-top: 18px;
            font-size: 16px;
            color: #444;
        }

        audio {
            width: 100%;
            margin-top: 20px;
        }

        .info {
            background: #f8fafc;
            border: 1px solid #e5e7eb;
            border-radius: 10px;
            padding: 12px;
            margin-top: 16px;
            color: #374151;
            font-size: 14px;
        }

        pre {
            text-align: left;
            background: #111827;
            color: #e5e7eb;
            padding: 12px;
            border-radius: 8px;
            overflow: auto;
            max-height: 220px;
            font-size: 12px;
        }
    </style>
</head>

<body>
    <div class="card">
        <h1>Live Audio Transmission</h1>
        <p>Connect headphones, then tap below to hear the live audio feed.</p>

        <div class="info">
            WiFi: <strong>{{HOTSPOT_NAME}}</strong><br>
            Password: <strong>{{WIFI_PASSWORD}}</strong><br>
            URL: <strong>http://{{HOTSPOT_IP}}:{{SERVER_PORT}}</strong>
        </div>

        <button id="startButton" type="button">LISTEN LIVE</button>

        <audio id="audio" autoplay playsinline controls></audio>

        <div id="status">Page loaded. Ready to listen.</div>

        <pre id="logBox"></pre>
    </div>

    <script>
        const button = document.getElementById("startButton");
        const statusEl = document.getElementById("status");
        const logBox = document.getElementById("logBox");
        const audioEl = document.getElementById("audio");

        let pc = null;

        function log(msg) {
            const line = new Date().toLocaleTimeString() + " - " + msg;
            console.log(line);
            logBox.textContent += line + "\\n";
            logBox.scrollTop = logBox.scrollHeight;
        }

        log("JavaScript loaded");

        button.onclick = async function () {
            log("Button click detected");
            button.textContent = "CONNECTING...";
            statusEl.textContent = "Creating audio connection...";

            try {
                await startWebRTC();
            } catch (err) {
                log("ERROR: " + err.toString());
                statusEl.textContent = "Error: " + err.toString();
                button.textContent = "TRY AGAIN";
                button.style.background = "#c62828";
            }
        };

        async function startWebRTC() {
            if (pc) {
                pc.close();
                pc = null;
            }

            log("Creating RTCPeerConnection");

            pc = new RTCPeerConnection({ iceServers: [] });

            pc.ontrack = function (event) {
                log("Received remote track: " + event.track.kind);
                audioEl.srcObject = event.streams[0];

                audioEl.play()
                    .then(() => log("audio.play() succeeded"))
                    .catch((err) => log("audio.play() failed: " + err.toString()));
            };

            pc.onconnectionstatechange = function () {
                log("Connection state: " + pc.connectionState);

                if (pc.connectionState === "connected") {
                    statusEl.textContent = "Connected - Listening Live";
                    button.textContent = "LIVE AUDIO ON";
                    button.style.background = "#21a35b";
                }

                if (
                    pc.connectionState === "failed" ||
                    pc.connectionState === "disconnected" ||
                    pc.connectionState === "closed"
                ) {
                    statusEl.textContent = "Disconnected";
                    button.textContent = "LISTEN LIVE";
                    button.style.background = "#16a34a";
                }
            };

            pc.oniceconnectionstatechange = function () {
                log("ICE state: " + pc.iceConnectionState);
            };

            pc.addTransceiver("audio", { direction: "recvonly" });

            log("Creating offer");
            const offer = await pc.createOffer();

            log("Setting local description");
            await pc.setLocalDescription(offer);

            log("Posting offer to server");
            const response = await fetch("/offer", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({
                    sdp: pc.localDescription.sdp,
                    type: pc.localDescription.type
                })
            });

            log("Server response: " + response.status);

            if (!response.ok) {
                const text = await response.text();
                throw new Error("Server returned " + response.status + ": " + text);
            }

            const answer = await response.json();

            log("Setting remote description");
            await pc.setRemoteDescription(answer);

            statusEl.textContent = "Waiting for audio connection...";
        }
    </script>
</body>
</html>
"""


BUTTON_TEST_HTML = """
<!doctype html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Button Test</title>
    <style>
        body { font-family: Arial; padding: 40px; text-align: center; }
        button { font-size: 28px; padding: 24px; width: 100%; }
        #out { margin-top: 24px; font-size: 20px; }
    </style>
</head>
<body>
    <button onclick="document.getElementById('out').innerText='Button works: ' + new Date().toLocaleTimeString()">TEST BUTTON</button>
    <div id="out">Not clicked yet</div>
</body>
</html>
"""


# =========================================================
# ROUTES
# =========================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    hotspot_ip = get_hotspot_ip()
    return INDEX_HTML.replace("{{HOTSPOT_NAME}}", HOTSPOT_NAME)\
        .replace("{{WIFI_PASSWORD}}", wifi_password_label())\
        .replace("{{HOTSPOT_IP}}", hotspot_ip)\
        .replace("{{SERVER_PORT}}", str(SERVER_PORT))


@app.get("/button-test", response_class=HTMLResponse)
async def button_test():
    return BUTTON_TEST_HTML


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


@app.post("/offer")
async def offer(request: Request):
    logger.info("POST /offer received")

    params = await request.json()

    offer_desc = RTCSessionDescription(
        sdp=params["sdp"],
        type=params["type"]
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
        "type": pc.localDescription.type
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
    parser.add_argument("--no-hotspot", action="store_true")
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
    print(" Live Audio Transmission")
    print("=" * 60)
    print(f"WiFi SSID:       {HOTSPOT_NAME}")
    print(f"WiFi password:   {wifi_password_label()}")
    print(f"Listen URL:      http://{hotspot_ip}:{args.port}")
    print(f"Button test:     http://{hotspot_ip}:{args.port}/button-test")
    print(f"Mic list:        http://{hotspot_ip}:{args.port}/mics")
    print(f"Stats:           http://{hotspot_ip}:{args.port}/stats")
    print()
    print(f"Audio rate:      {AUDIO_RATE}")
    print(f"Frame samples:   {AUDIO_FRAME_SAMPLES}")
    print(f"Shared mic:      enabled")
    print("=" * 60)
    print()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()