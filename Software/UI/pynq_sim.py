"""
WaveForm — Simulated PYNQ-Z1 FPGA backend.
Run:  python pynq_sim.py
Port 5001 — receives 37-byte bit-packed control packets from client.
Port 5000 — streams uint8 frames (4-byte big-endian length header) to client.
"""

import socket
import struct
import threading
import time

import numpy as np

from waveform_protocol import PACKET_BYTES, unpack_packet

HOST        = "0.0.0.0"
FRAME_PORT  = 5000
CTRL_PORT   = 5001
FIELD_W     = 640
FIELD_H     = 480
FRAME_BYTES = FIELD_W * FIELD_H

TARGET_FPS  = 30.0

# ── Shared state (all guarded by _ant_lock) ────────────────────────────────────
_ant_lock    = threading.Lock()
_antennas: dict = {
    0: {"id": 0, "x": 320, "y": 240, "amplitude": 0.75, "frequency": 1.0,
        "theta_0": 0.0, "a": 0.0}
}
_sim_time    = 0.0
_last_real_t = time.monotonic()
_paused      = False

# Pre-compute coordinate grids (reused every frame)
_xs = np.arange(FIELD_W, dtype=np.float32)
_ys = np.arange(FIELD_H, dtype=np.float32)
_XX, _YY = np.meshgrid(_xs, _ys)


# ── Control server — port 5001 ─────────────────────────────────────────────────
def _handle_ctrl(conn, addr):
    global _paused
    print(f"[ctrl] {addr} connected — {PACKET_BYTES}-byte packets")
    buf = b""
    try:
        while True:
            chunk = conn.recv(512)
            if not chunk:
                break
            buf += chunk
            while len(buf) >= PACKET_BYTES:
                raw, buf = buf[:PACKET_BYTES], buf[PACKET_BYTES:]
                try:
                    data = unpack_packet(raw)
                except ValueError as e:
                    print(f"[ctrl] bad packet: {e}")
                    continue

                with _ant_lock:
                    _paused    = data["paused"]
                    new_ants   = {}
                    for i, s in enumerate(data["sources"]):
                        new_ants[i] = {
                            "id":        i,
                            "x":         s["x"],
                            "y":         s["y"],
                            "amplitude": s["amplitude"],
                            "frequency": s["frequency"],
                            "theta_0":   s["theta_0"],   # radians, ready for frame gen
                            "a":         s["a"],
                        }
                    _antennas.clear()
                    _antennas.update(new_ants)

                print(f"[PL] t={data['global_time']} "
                      f"n={data['n_active']} paused={data['paused']}")
                for i, s in enumerate(data["sources"]):
                    raw_s = s["_raw"]
                    print(f"     src{i}: amp={raw_s['amplitude']:>3} "
                          f"freq={raw_s['frequency']:>3} "
                          f"dir={raw_s['directivity']:>3} "
                          f"th={raw_s['direction']:>2} "
                          f"x={raw_s['x']:>3} y={raw_s['y']:>3}")
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        conn.close()
        print(f"[ctrl] {addr} disconnected")


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
    global _sim_time, _last_real_t
    with _ant_lock:
        ants = list(_antennas.values())
        now  = time.monotonic()
        if not _paused:
            _sim_time += now - _last_real_t
        _last_real_t = now
        t = _sim_time

    field = np.zeros((FIELD_H, FIELD_W), dtype=np.float32)

    for a_dict in ants:
        cx      = float(a_dict["x"])
        cy      = float(a_dict["y"])
        amp     = float(a_dict.get("amplitude", 0.75))
        freq    = float(a_dict.get("frequency", 1.0))
        theta_0 = float(a_dict.get("theta_0", 0.0))
        a       = float(a_dict.get("a", 0.0))

        dx    = _XX - cx
        dy    = _YY - cy
        r     = np.hypot(dx, dy)
        theta = np.arctan2(dy, dx)
        D     = (1.0 - a) + a * np.maximum(0.0, np.cos(theta - theta_0)) ** 6
        field += amp * D * np.cos(2.0 * np.pi * freq * r / 80.0 - t * 3.0) \
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
