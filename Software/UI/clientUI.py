"""
WaveForm — PYNQ EM Field Renderer Server

Port 5000  — streams frames to client (uint8, 4-byte header)
Port 5001  — receives antenna parameter JSON from client

In production: replace the fake frame generator with PYNQ DMA reads,
and replace _apply_params() with AXI-Lite MMIO writes.
"""

import socket
import struct
import threading
import time
import json

import numpy as np

HOST       = "0.0.0.0"
FRAME_PORT = 5000
CTRL_PORT  = 5001
WIDTH, HEIGHT = 640, 480
FRAME_BYTES   = WIDTH * HEIGHT

# ── Shared antenna state ──────────────────────────────────────────────────────
_ant_lock = threading.Lock()
_antennas: dict[int, dict] = {
    0: {"id": 0, "amplitude": 1.0, "frequency": 1.0, "x": 100, "y": 100}
}


def _apply_params(ant: dict):
    """
    Apply antenna parameters to the PL.
    Replace with your PYNQ MMIO writes:

        from pynq import MMIO
        ANT_BASE   = 0x100
        ANT_STRIDE = 5 * 4          # 5 registers × 4 bytes
        base = ANT_BASE + ant['id'] * ANT_STRIDE
        mmio.write(base + 0x00, int(ant['x']))
        mmio.write(base + 0x04, int(ant['y']))
        mmio.write(base + 0x08, float_to_q115(ant['amplitude']))
        mmio.write(base + 0x0C, float_to_q412(ant['phase']))
        # frequency → wavenumber k, write to K_WAVE reg 0x10
        import math
        k = 2 * math.pi * ant['frequency']
        mmio.write(0x10, float_to_q97(k))
    """
    print(f"  [PL] antenna {ant['id']}: "
          f"amp={ant['amplitude']:.3f}  "
          f"freq=×{ant['frequency']:.3f}  "
          f"pos=({ant['x']},{ant['y']})")


# ═══════════════════════════════════════════════════════════════════════════════
# Control server — port 5001
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_ctrl_client(conn, addr):
    print(f"[ctrl] client connected: {addr}")
    buf = ""
    try:
        while True:
            chunk = conn.recv(4096).decode(errors="ignore")
            if not chunk:
                break
            buf += chunk
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    ant_id = int(msg.get("id", 0))
                    with _ant_lock:
                        if ant_id not in _antennas:
                            _antennas[ant_id] = {}
                        _antennas[ant_id].update(msg)
                        ant_copy = dict(_antennas[ant_id])
                    _apply_params(ant_copy)
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    print(f"[ctrl] bad message: {e}")
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        conn.close()
        print(f"[ctrl] client disconnected: {addr}")


def _ctrl_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, CTRL_PORT))
    srv.listen(4)
    print(f"[ctrl] listening on {HOST}:{CTRL_PORT}")
    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=_handle_ctrl_client,
                             args=(conn, addr), daemon=True)
        t.start()


# ═══════════════════════════════════════════════════════════════════════════════
# Frame server — port 5000
# ═══════════════════════════════════════════════════════════════════════════════

def _generate_frame() -> np.ndarray:
    """
    STUB — replace with PYNQ DMA frame read, e.g.:
        frame_buf = allocate(shape=(HEIGHT, WIDTH), dtype=np.uint8, cacheable=True)
        dma.recvchannel.transfer(frame_buf)
        dma.recvchannel.wait()
        return frame_buf
    """
    with _ant_lock:
        ants = list(_antennas.values())

    # Cheap CPU stand-in: superpose sinusoidal rings from each antenna
    t_now = time.monotonic()
    xs = np.arange(WIDTH,  dtype=np.float32)
    ys = np.arange(HEIGHT, dtype=np.float32)
    XX, YY = np.meshgrid(xs, ys)
    field  = np.zeros((HEIGHT, WIDTH), dtype=np.float32)

    for a in ants:
        cx = a.get("x", 100) / 200 * WIDTH
        cy = a.get("y", 100) / 200 * HEIGHT
        amp  = float(a.get("amplitude", 1.0))
        freq = float(a.get("frequency", 1.0))
        r    = np.hypot(XX - cx, YY - cy) + 1e-6
        field += amp * np.cos(2 * np.pi * freq * r / 60 - t_now * 2) / r * 20

    # Normalise → uint8
    lo, hi = field.min(), field.max()
    if hi > lo:
        field = (field - lo) / (hi - lo)
    return (field * 255).astype(np.uint8)


def _handle_frame_client(conn, addr):
    print(f"[frame] client connected: {addr}")
    frames_sent, fps_timer = 0, time.monotonic()
    try:
        while True:
            frame   = _generate_frame()
            payload = frame.tobytes()
            header  = struct.pack(">I", FRAME_BYTES)
            conn.sendall(header + payload)
            frames_sent += 1
            now = time.monotonic()
            if now - fps_timer >= 1.0:
                print(f"[frame] send fps: {frames_sent/(now-fps_timer):.1f}")
                frames_sent, fps_timer = 0, now
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        conn.close()
        print(f"[frame] client disconnected: {addr}")


def _frame_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, FRAME_PORT))
    srv.listen(1)
    print(f"[frame] listening on {HOST}:{FRAME_PORT}")
    while True:
        conn, addr = srv.accept()
        t = threading.Thread(target=_handle_frame_client,
                             args=(conn, addr), daemon=True)
        t.start()


# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    threading.Thread(target=_ctrl_server,  daemon=True).start()
    threading.Thread(target=_frame_server, daemon=True).start()
    print("WaveForm server running. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down.")