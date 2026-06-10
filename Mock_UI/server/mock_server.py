"""Mock PS-side websocket server for the EM Field Renderer UI.

Implements protocol v0.1 (see ../protocol.md). Stores antenna state in
memory, renders a real far-field image from it in NumPy each tick, and
streams JPEG frames over binary WebSocket frames.

Run:
    pip install -r requirements.txt
    python mock_server.py
"""

import asyncio
import io
import json
import logging
import math
import time
from dataclasses import dataclass, field

import numpy as np
import websockets
from PIL import Image

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mock_server")

HOST = "0.0.0.0"
PORT = 8765
PROTOCOL_VERSION = "0.1"
GRID_W, GRID_H = 200, 200
MAX_ANTENNAS = 32
TELEMETRY_HZ = 10.0
FRAME_HZ = 30.0
JPEG_QUALITY = 80


@dataclass
class State:
    globals: dict = field(default_factory=lambda: {
        "frequency_hz": 2.4e9,
        "time_rate": 1.0,
        "paused": False,
        "out_mode": 0,
    })
    antennas: list = field(default_factory=list)
    applied_seq: int = -1


STATE = State()
CLIENTS: set = set()
T0 = time.monotonic()


def validate_state_update(msg):
    if "globals" not in msg or "antennas" not in msg:
        return "missing globals or antennas"
    g = msg["globals"]
    for k in ("frequency_hz", "time_rate", "paused", "out_mode"):
        if k not in g:
            return f"globals.{k} missing"
    if not isinstance(msg["antennas"], list):
        return "antennas must be a list"
    if len(msg["antennas"]) > MAX_ANTENNAS:
        return f"too many antennas (max {MAX_ANTENNAS})"
    for i, a in enumerate(msg["antennas"]):
        for k in ("id", "x", "y", "amplitude", "phase", "pattern"):
            if k not in a:
                return f"antenna {i}: {k} missing"
        if not (0.0 <= a["x"] <= GRID_W - 1):
            return f"antenna {i}: x out of range"
        if not (0.0 <= a["y"] <= GRID_H - 1):
            return f"antenna {i}: y out of range"
        if a["pattern"] not in (0, 1, 2):
            return f"antenna {i}: pattern not in (0,1,2)"
    return None


PRESETS = {
    "two_slit": [
        {"id": 0, "x": 80,  "y": 100, "amplitude": 1.0, "phase": 0.0, "pattern": 0},
        {"id": 1, "x": 120, "y": 100, "amplitude": 1.0, "phase": 0.0, "pattern": 0},
    ],
    "phased_array": [
        {"id": i, "x": 60 + i * 20, "y": 100, "amplitude": 1.0,
         "phase": i * math.pi / 4, "pattern": 0}
        for i in range(4)
    ],
    "dipole_pair": [
        {"id": 0, "x": 90,  "y": 100, "amplitude": 1.0, "phase": 0.0,        "pattern": 1},
        {"id": 1, "x": 110, "y": 100, "amplitude": 1.0, "phase": math.pi,    "pattern": 1},
    ],
}


_Y, _X = np.mgrid[0:GRID_H, 0:GRID_W].astype(np.float32)
_LAMBDA_MIN_PIX = 6.0
_LAMBDA_MAX_PIX = 60.0


def display_wavelength_pixels(freq_hz):
    f = max(1e8, min(6e9, freq_hz))
    t = (math.log10(f) - math.log10(1e8)) / (math.log10(6e9) - math.log10(1e8))
    return _LAMBDA_MAX_PIX * (1 - t) + _LAMBDA_MIN_PIX * t


def render_field(antennas, globals_, omega_t):
    out = np.zeros((GRID_H, GRID_W), dtype=np.float32)
    if not antennas:
        return out
    lam = display_wavelength_pixels(globals_["frequency_hz"])
    k = 2 * np.pi / lam
    for a in antennas:
        dx = _X - a["x"]
        dy = _Y - a["y"]
        r = np.sqrt(dx * dx + dy * dy) + 1.0
        if a["pattern"] == 0:
            D = 1.0
        elif a["pattern"] == 1:
            theta = np.arctan2(dy, dx)
            D = np.abs(np.sin(theta))
        else:
            theta = np.arctan2(dy, dx)
            D = np.cos(theta) ** 2
        out += a["amplitude"] * D * (1.0 / r) * np.cos(omega_t - k * r + a["phase"])
    return out


def _viridis(v):
    stops = np.array([
        [0.267, 0.005, 0.329],
        [0.127, 0.567, 0.551],
        [0.369, 0.789, 0.383],
        [0.993, 0.906, 0.144],
    ], dtype=np.float32)
    idx = v * 3.0
    lo = np.floor(idx).astype(np.int32).clip(0, 2)
    hi = lo + 1
    t = (idx - lo)[..., None]
    c = stops[lo] * (1 - t) + stops[hi] * t
    return c[..., 0], c[..., 1], c[..., 2]


def field_to_rgb(fld, out_mode):
    if out_mode == 0:
        v = np.clip(fld, -1.0, 1.0)
        r = np.clip(0.5 + 0.5 * v, 0, 1)
        b = np.clip(0.5 - 0.5 * v, 0, 1)
        g = np.clip(1.0 - np.abs(v), 0, 1) * 0.35
    elif out_mode == 1:
        v = np.clip(np.abs(fld), 0, 1)
        r, g, b = _viridis(v)
    elif out_mode == 2:
        v = np.clip(fld ** 2, 0, 1)
        r, g, b = _viridis(v)
    else:
        v = np.clip((fld + 1) * 0.5, 0, 1)
        r = g = b = v
    img = np.stack([r, g, b], axis=-1)
    return (img * 255).astype(np.uint8)


def encode_jpeg(rgb):
    img = Image.fromarray(rgb, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


async def broadcast_text(msg):
    if not CLIENTS:
        return
    payload = json.dumps(msg)
    await asyncio.gather(*(c.send(payload) for c in CLIENTS), return_exceptions=True)


async def broadcast_binary(data):
    if not CLIENTS:
        return
    await asyncio.gather(*(c.send(data) for c in CLIENTS), return_exceptions=True)


async def send_text(ws, msg):
    await ws.send(json.dumps(msg))


async def handle_message(ws, msg):
    t = msg.get("type")
    if t == "state_update":
        err = validate_state_update(msg)
        if err is not None:
            log.warning("state_error seq=%s: %s", msg.get("seq"), err)
            await send_text(ws, {"type": "state_error",
                                 "seq": msg.get("seq", -1), "reason": err})
            return
        STATE.globals = msg["globals"]
        STATE.antennas = msg["antennas"]
        STATE.applied_seq = msg.get("seq", STATE.applied_seq)
        log.info("state_update seq=%s antennas=%d",
                 STATE.applied_seq, len(STATE.antennas))
        await send_text(ws, {"type": "state_ack", "seq": STATE.applied_seq})
    elif t == "command":
        action = msg.get("action")
        if action in ("reset", "clear_antennas"):
            STATE.antennas = []
            log.info("command: %s", action)
        elif action == "load_preset":
            name = msg.get("preset")
            if name in PRESETS:
                STATE.antennas = [dict(a) for a in PRESETS[name]]
                log.info("command: load_preset %s", name)
            else:
                log.warning("unknown preset: %s", name)
        else:
            log.warning("unknown command action: %s", action)
    else:
        log.warning("unknown message type: %s", t)


async def telemetry_loop():
    period = 1.0 / TELEMETRY_HZ
    frame = 0
    fake_fps = 58.2
    fake_baseline = 4.1
    while True:
        await asyncio.sleep(period)
        frame += int(fake_fps * period)
        omega_t = (2 * math.pi * (time.monotonic() - T0) * 0.5) % (2 * math.pi)
        await broadcast_text({
            "type": "telemetry",
            "frame": frame,
            "fps": fake_fps,
            "cpu_baseline_fps": fake_baseline,
            "ratio": fake_fps / fake_baseline,
            "omega_t": omega_t,
            "applied_seq": STATE.applied_seq,
        })


async def frame_loop():
    period = 1.0 / FRAME_HZ
    phase = 0.0
    last = time.monotonic()
    while True:
        await asyncio.sleep(period)
        now = time.monotonic()
        dt = now - last
        last = now
        if STATE.globals.get("paused"):
            continue
        phase += 2.0 * math.pi * STATE.globals.get("time_rate", 1.0) * dt
        phase = phase % (2 * math.pi)
        if not CLIENTS:
            continue
        try:
            rgb = field_to_rgb(
                render_field(STATE.antennas, STATE.globals, phase),
                STATE.globals.get("out_mode", 0),
            )
            await broadcast_binary(encode_jpeg(rgb))
        except Exception:
            log.exception("frame_loop error")


async def handler(ws):
    CLIENTS.add(ws)
    peer = getattr(ws, "remote_address", "?")
    log.info("client connected: %s", peer)
    try:
        await send_text(ws, {
            "type": "hello",
            "protocol_version": PROTOCOL_VERSION,
            "server": "mock",
            "grid": {"w": GRID_W, "h": GRID_H},
            "max_antennas": MAX_ANTENNAS,
            "state": {"globals": STATE.globals, "antennas": STATE.antennas},
        })
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                log.warning("ignoring unexpected binary from client")
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("bad JSON from %s", peer)
                continue
            await handle_message(ws, msg)
    except websockets.ConnectionClosed:
        pass
    finally:
        CLIENTS.discard(ws)
        log.info("client disconnected: %s", peer)


async def main():
    log.info("listening on ws://%s:%d/", HOST, PORT)
    async with websockets.serve(handler, HOST, PORT, max_size=2**22):
        await asyncio.gather(telemetry_loop(), frame_loop())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
