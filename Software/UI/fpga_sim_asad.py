import socket
import struct
import threading
import time

import numpy as np

HOST = "0.0.0.0"
FRAME_PORT = 5000
CTRL_PORT = 5001
WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT * 3
CTRL_FMT = ">BBHHff2x"
CTRL_SIZE = struct.calcsize(CTRL_FMT)

TARGET_FPS = 30.0

# ── Shared antenna state ───────────────────────────────────────────────────────
_ant_lock = threading.Lock()
_antennas: dict = {
    0: {"id": 0, "x": 320, "y": 240, "amplitude": 0.75, "frequency": 1.0}
}

# Pre-compute coordinate grids (reused every frame)
_xs = np.arange(WIDTH, dtype=np.float32)
_ys = np.arange(HEIGHT, dtype=np.float32)
_XX, _YY = np.meshgrid(_xs, _ys)


# ── Colour map ───────────────────────────────────────────────────────────────
def build_viridis() -> np.ndarray:
    """Custom RdBu LUT:
    - negative -> blue (#2166AC)
    - zero     -> white (#F7F7F7)
    - positive -> red (#B2182B)
    """
    neg = np.array([33, 102, 172], dtype=np.float32)   # #2166AC
    zero = np.array([247, 247, 247], dtype=np.float32)  # #F7F7F7
    pos = np.array([178, 24, 43], dtype=np.float32)    # #B2182B

    lut = np.zeros((256, 3), dtype=np.uint8)
    for i in range(256):
        t = i / 255.0
        if t <= 0.5:
            u = t / 0.5
            col = (1 - u) * neg + u * zero
        else:
            u = (t - 0.5) / 0.5
            col = (1 - u) * zero + u * pos
        lut[i] = np.clip(col + 0.5, 0, 255).astype(np.uint8)
    return lut

_VIRIDIS = build_viridis()


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
                            "id": ant_id,
                            "x": int(x),
                            "y": int(y),
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

    t = time.monotonic()
    field = np.zeros((HEIGHT, WIDTH), dtype=np.float32)

    for a in ants:
        cx = float(a["x"])
        cy = float(a["y"])
        amp = float(a.get("amplitude", 0.75))
        freq = float(a.get("frequency", 1.0))
        r = np.hypot(_XX - cx, _YY - cy)
        field += amp * np.cos(2.0 * np.pi * freq * r / 80.0 - t * 3.0) / (r / 40.0 + 1.0)

    lo, hi = field.min(), field.max()
    if hi > lo:
        field = (field - lo) / (hi - lo)
    else:
        field[:] = 0.5
    return (field * 255).astype(np.uint8)


def _frame_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, FRAME_PORT))
    srv.listen(1)
    print(f"[frame] listening on port {FRAME_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_frame, args=(conn, addr), daemon=True).start()


def _handle_frame(conn, addr):
    print(f"[frame] connected {addr}")
    fps_n, fps_t = 0, time.monotonic()
    try:
        while True:
            frame = _generate_frame()
            rgb = _VIRIDIS[frame]
            conn.sendall(struct.pack(">I", FRAME_BYTES) + rgb.tobytes())
            fps_n += 1
            now = time.monotonic()
            if now - fps_t >= 1.0:
                print(f"[frame] {fps_n / (now - fps_t):.1f} fps")
                fps_n, fps_t = 0, now
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        conn.close()
        print(f"[frame] disconnected {addr}")


if __name__ == "__main__":
    threading.Thread(target=_ctrl_server, daemon=True).start()
    threading.Thread(target=_frame_server, daemon=True).start()
    print("WaveForm simulator running on ports 5000 (frames) and 5001 (ctrl). Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down.")
 