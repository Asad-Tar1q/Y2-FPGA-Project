"""
WaveForm — Simulated PYNQ-Z1 FPGA backend.
Run:  python pynq_sim.py
Port 5001 — receives 20-byte antenna control structs from client.
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
CTRL_FMT    = ">BBHHffff"
CTRL_SIZE   = struct.calcsize(CTRL_FMT)   # 20 bytes
WALL_FMT    = ">BBHHHHxx"
WALL_SIZE   = struct.calcsize(WALL_FMT)   # 12 bytes

TARGET_FPS  = 30.0

# ── Shared state (all guarded by _ant_lock) ────────────────────────────────────
_ant_lock    = threading.Lock()
_antennas: dict = {
    0: {"id": 0, "x": 320, "y": 240, "amplitude": 0.75, "frequency": 1.0,
        "theta_0": 0.0, "a": 0.0}
}
_walls: dict    = {}   # wall_id → {"x1","y1","x2","y2","type": 0=reflect,1=absorb}
_wall_next_id   = 0    # mirrors client _next_wall_id; only ever increments
_sim_time    = 0.0
_last_real_t = time.monotonic()
_paused      = False

# Pre-compute coordinate grids (reused every frame)
_xs = np.arange(FIELD_W, dtype=np.float32)
_ys = np.arange(FIELD_H, dtype=np.float32)
_XX, _YY = np.meshgrid(_xs, _ys)


# ── Control server — port 5001 ─────────────────────────────────────────────────
def _handle_ctrl(conn, addr):
    global _paused, _wall_next_id
    print(f"[ctrl] connected {addr}")
    buf = b""
    try:
        while True:
            chunk = conn.recv(256)
            if not chunk:
                break
            buf += chunk
            while True:
                if len(buf) < 2:
                    break
                # Antenna packets: cmd at byte 1, values 0/1/2 — never 3 or 4.
                # Wall packets:    cmd at byte 0, values 3/4   — never 0, 1, or 2.
                # Check antenna first (byte 1 is unambiguous regardless of ant_id).
                if buf[1] in (0, 1, 2):
                    # Antenna command — 20-byte packet
                    if len(buf) < CTRL_SIZE:
                        break
                    raw = buf[:CTRL_SIZE]
                    buf = buf[CTRL_SIZE:]
                    ant_id, cmd, x, y, amplitude, frequency, theta_0, a = \
                        struct.unpack(CTRL_FMT, raw)
                    if cmd == 1:
                        with _ant_lock:
                            _antennas.pop(ant_id, None)
                        print(f"  [PL] delete antenna {ant_id}")
                    elif cmd == 2:
                        with _ant_lock:
                            _paused = not _paused
                        print(f"  [PL] simulation {'paused' if _paused else 'resumed'}")
                    else:
                        amplitude = max(0.0, min(1.0, float(amplitude)))
                        a         = max(0.0, min(1.0, float(a)))
                        with _ant_lock:
                            _antennas[ant_id] = {
                                "id":        ant_id,
                                "x":         int(x),
                                "y":         int(y),
                                "amplitude": amplitude,
                                "frequency": float(frequency),
                                "theta_0":   float(theta_0),
                                "a":         a,
                            }
                        print(f"  [PL] antenna {ant_id}: "
                              f"amp={amplitude:.3f}  freq=×{frequency:.3f}  "
                              f"θ={theta_0:.3f}  a={a:.3f}  "
                              f"pos=({int(x)},{int(y)})")
                elif buf[0] in (3, 4):
                    # Wall command — 12-byte packet
                    if len(buf) < WALL_SIZE:
                        break
                    raw = buf[:WALL_SIZE]
                    buf = buf[WALL_SIZE:]
                    cmd, wtype, x1, y1, x2, y2 = struct.unpack(WALL_FMT, raw)
                    if cmd == 4:
                        with _ant_lock:
                            # x1 field holds wall_id for delete
                            _walls.pop(x1, None)
                        print(f"  [PL] delete wall {x1}")
                    else:
                        with _ant_lock:
                            wall_id = _wall_next_id
                            _wall_next_id += 1
                            _walls[wall_id] = {
                                "x1": int(x1), "y1": int(y1),
                                "x2": int(x2), "y2": int(y2),
                                "type": int(wtype),
                            }
                        tname = "reflect" if wtype == 0 else "absorb"
                        print(f"  [PL] wall {wall_id}: ({x1},{y1})→({x2},{y2}) {tname}")
                else:
                    # Unknown byte — discard and re-sync
                    print(f"  [PL] unexpected byte 0x{buf[0]:02x}, skipping")
                    buf = buf[1:]
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
def _source_contribution(cx, cy, amp, freq, theta_0, a_val, t):
    """Compute field contribution for one point source at (cx, cy)."""
    dx    = _XX - cx
    dy    = _YY - cy
    r     = np.hypot(dx, dy)
    theta = np.arctan2(dy, dx)
    D     = (1.0 - a_val) + a_val * np.maximum(0.0, np.cos(theta - theta_0)) ** 6
    return amp * D * np.cos(2.0 * np.pi * freq * r / 80.0 - t * 3.0) \
                   / (r / 40.0 + 1.0)


def _generate_frame() -> np.ndarray:
    global _sim_time, _last_real_t
    with _ant_lock:
        ants  = list(_antennas.values())
        walls = list(_walls.values())
        now   = time.monotonic()
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
        a_val   = float(a_dict.get("a", 0.0))

        field += _source_contribution(cx, cy, amp, freq, theta_0, a_val, t)

        # Reflectors: add image source mirrored across each reflector wall
        for w in walls:
            if w["type"] != 0:
                continue
            x1, y1, x2, y2 = w["x1"], w["y1"], w["x2"], w["y2"]
            if abs(x2 - x1) >= abs(y2 - y1):   # horizontal wall at y=y1
                img_cy = 2.0 * y1 - cy
                field += _source_contribution(cx, img_cy, amp, freq, theta_0, a_val, t)
            else:                                # vertical wall at x=x1
                img_cx = 2.0 * x1 - cx
                field += _source_contribution(img_cx, cy, amp, freq, theta_0, a_val, t)

    # Absorbers: zero out field on the far side from the average source position
    if ants:
        avg_sx = float(np.mean([a["x"] for a in ants]))
        avg_sy = float(np.mean([a["y"] for a in ants]))
    else:
        avg_sx, avg_sy = FIELD_W / 2.0, FIELD_H / 2.0

    for w in walls:
        if w["type"] != 1:
            continue
        x1, y1, x2, y2 = w["x1"], w["y1"], w["x2"], w["y2"]
        if abs(x2 - x1) >= abs(y2 - y1):   # horizontal wall
            if avg_sy <= y1:
                field[_YY > y1] = 0.0
            else:
                field[_YY < y1] = 0.0
        else:                               # vertical wall
            if avg_sx <= x1:
                field[_XX > x1] = 0.0
            else:
                field[_XX < x1] = 0.0

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
