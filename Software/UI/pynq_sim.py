"""
WaveForm — Simulated PYNQ-Z1 FPGA backend.
Run:  python pynq_sim.py
Port 5001 — receives 16-byte antenna control structs from client.
Port 5000 — streams uint8 frames (4-byte big-endian length header) to client.
"""

import socket
import struct
import threading
import time

import numpy as np

HOST        = "0.0.0.0"
FRAME_PORT  = 5000
CTRL_PORT   = 5001
FIELD_W     = 640
FIELD_H     = 480
FRAME_BYTES = FIELD_W * FIELD_H
CTRL_FMT    = ">BBHHff2x"
CTRL_SIZE   = struct.calcsize(CTRL_FMT)   # 16 bytes

TARGET_FPS  = 30.0

# ── Shared antenna state ───────────────────────────────────────────────────────
_ant_lock = threading.Lock()
_antennas: dict = {
    0: {"id": 0, "x": 320, "y": 240, "amplitude": 0.75, "frequency": 1.0}
}

# Pre-compute coordinate grids (reused every frame)
_xs = np.arange(FIELD_W, dtype=np.float32)
_ys = np.arange(FIELD_H, dtype=np.float32)
_XX, _YY = np.meshgrid(_xs, _ys)


# ── Control server — port 5001 ─────────────────────────────────────────────────
def _handle_ctrl(conn, addr):
    print(f"[ctrl] connected {addr}")
    buf = b""
    try:
        while True:
            chunk = conn.recv(256)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= CTRL_SIZE:
                raw = buf[:CTRL_SIZE]
                buf = buf[CTRL_SIZE:]
                ant_id, cmd, x, y, amplitude, frequency = struct.unpack(CTRL_FMT, raw)
                if cmd == 1:
                    with _ant_lock:
                        _antennas.pop(ant_id, None)
                    print(f"  [PL] delete antenna {ant_id}")
                else:
                    with _ant_lock:
                        _antennas[ant_id] = {
                            "id":        ant_id,
                            "x":         int(x),
                            "y":         int(y),
                            "amplitude": float(amplitude),
                            "frequency": float(frequency),
                        }
                    print(f"  [PL] antenna {ant_id}: "
                          f"amp={amplitude:.3f}  "
                          f"freq=×{frequency:.3f}  "
                          f"pos=({int(x)},{int(y)})")
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        conn.close()
        print(f"[ctrl] disconnected {addr}")


def _ctrl_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, CTRL_PORT))
    srv.listen(4)
    print(f"[ctrl] listening on port {CTRL_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_ctrl, args=(conn, addr), daemon=True).start()


# ── Frame generator ────────────────────────────────────────────────────────────
def _generate_frame() -> np.ndarray:
    with _ant_lock:
        ants = list(_antennas.values())

    t     = time.monotonic()
    field = np.zeros((FIELD_H, FIELD_W), dtype=np.float32)

    for a in ants:
        cx   = float(a["x"])
        cy   = float(a["y"])
        amp  = float(a.get("amplitude", 0.75))
        freq = float(a.get("frequency", 1.0))
        r    = np.hypot(_XX - cx, _YY - cy)
        # Decaying sinusoidal ring: amplitude envelope / (r/scale + 1)
        field += amp * np.cos(2.0 * np.pi * freq * r / 80.0 - t * 3.0) \
                     / (r / 40.0 + 1.0)

    lo, hi = field.min(), field.max()
    if hi > lo:
        field = (field - lo) / (hi - lo)
    else:
        field[:] = 0.5
    return (field * 255).astype(np.uint8)


# ── Frame server — port 5000 ───────────────────────────────────────────────────
def _handle_frame(conn, addr):
    print(f"[frame] connected {addr}")
    fps_n, fps_t = 0, time.monotonic()
    frame_period = 1.0 / TARGET_FPS
    try:
        while True:
            t0      = time.monotonic()
            frame   = _generate_frame()
            payload = frame.tobytes()
            conn.sendall(struct.pack(">I", FRAME_BYTES) + payload)
            fps_n += 1
            now = time.monotonic()
            if now - fps_t >= 1.0:
                print(f"[frame] {fps_n / (now - fps_t):.1f} fps")
                fps_n, fps_t = 0, now
            sleep_t = frame_period - (now - t0)
            if sleep_t > 0.001:
                time.sleep(sleep_t)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        conn.close()
        print(f"[frame] disconnected {addr}")


def _frame_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, FRAME_PORT))
    srv.listen(1)
    print(f"[frame] listening on port {FRAME_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_frame, args=(conn, addr), daemon=True).start()


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=_ctrl_server,  daemon=True).start()
    threading.Thread(target=_frame_server, daemon=True).start()
    print("WaveForm simulator running on ports 5000 (frames) and 5001 (ctrl). Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down.")
