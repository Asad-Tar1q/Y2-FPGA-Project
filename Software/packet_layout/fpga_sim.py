"""
fpga_sim.py  —  WaveForm simulated FPGA backend

Port 5000 : streams uint8 frames to client  (4-byte big-endian length header)
Port 5001 : receives 37-byte bit-packed control packets from client

Decodes the bit-packed struct, mirrors what the PS would write to the
AXI-Lite regfile, and generates physically meaningful interference frames
driven by the decoded antenna state.

Later: replace _generate_frame() with PYNQ DMA reads and
       replace _apply_to_regfile() with actual MMIO writes.
"""

import socket
import struct
import threading
import time
import math

import numpy as np

from waveform_protocol import (
    PACKET_BYTES, unpack_packet,
    dq_amplitude, dq_frequency, dq_dir_strength, dq_direction
)

HOST        = "0.0.0.0"
FRAME_PORT  = 5000
CTRL_PORT   = 5001
W, H        = 640, 480
FRAME_BYTES = W * H
TARGET_FPS  = 30.0

# ── Pre-computed coordinate grids (reused every frame) ────────────────────────
_XX, _YY = np.meshgrid(np.arange(W, dtype=np.float32),
                        np.arange(H, dtype=np.float32))

# ── Shared decoded state (written by ctrl thread, read by frame thread) ───────
_state_lock = threading.Lock()
_state = {
    "global_time": 0,
    "n_active":    1,
    "sources": [{
        "active":       True,
        "amplitude":    0.75,
        "frequency":    1.0,
        "dir_strength": 0.0,
        "direction":    0.0,
        "x":            W / 2,
        "y":            H / 2,
        "_raw": {
            "amplitude": 191, "frequency": 10,
            "dir_strength": 0, "direction": 0,
            "x": W // 2, "y": H // 2,
        }
    }]
}


# ── Regfile mirror — shows exactly what the PS would write over AXI-Lite ──────
# 11 registers, 32 bits each (see waveform_protocol.py header for layout)
_regfile = [0] * 11

def _apply_to_regfile(data: dict):
    """
    Pack decoded integer values into the register layout.
    On real hardware this becomes mmio.write(base + offset, value) calls.

    Reg 0:  [15:0] global_time  [18:16] n_active
    Reg 1:  [7:0] amp  [15:8] dstr  [22:16] dir  [29:23] freq   (src 0)
    Reg 2:  [9:0] x    [18:10] y                               (src 0)
    Reg 3-4: src 1, Reg 5-6: src 2, Reg 7-8: src 3, Reg 9-10: src 4
    """
    _regfile[0] = (data["global_time"] & 0x3FFF) | ((data["n_active"] & 0x7) << 16)

    for i, s in enumerate(data["sources"][:5]):
        r     = s.get("_raw", {})
        amp   = r.get("amplitude",    0) & 0xFF
        dstr  = r.get("dir_strength", 0) & 0xFF
        dirq  = r.get("direction",    0) & 0x7F
        freqq = r.get("frequency",   10) & 0x7F
        xq    = r.get("x",            0) & 0x3FF
        yq    = r.get("y",            0) & 0x1FF

        reg_a = amp | (dstr << 8) | (dirq << 16) | (freqq << 23)
        reg_b = xq  | (yq   << 10)

        _regfile[1 + i * 2]     = reg_a
        _regfile[1 + i * 2 + 1] = reg_b

    return _regfile


# ── Control server — port 5001 ────────────────────────────────────────────────

def _recv_exactly(conn, n: int) -> bytes:
    buf = bytearray(n)
    mv  = memoryview(buf)
    rcv = 0
    while rcv < n:
        c = conn.recv_into(mv[rcv:], n - rcv)
        if not c:
            raise ConnectionResetError("socket closed")
        rcv += c
    return bytes(buf)


def _handle_ctrl(conn, addr):
    print(f"[ctrl] {addr} connected  (expecting {PACKET_BYTES}-byte packets)")
    pkts_ok, pkts_bad = 0, 0
    last_seq = -1

    try:
        while True:
            raw = _recv_exactly(conn, PACKET_BYTES)

            try:
                data = unpack_packet(raw)
            except ValueError as e:
                pkts_bad += 1
                print(f"[ctrl] bad packet #{pkts_ok+pkts_bad}: {e}")
                continue

            # Detect sequence gaps
            seq = data["seq"]
            if last_seq >= 0:
                expected = (last_seq + 1) & 0xFFFF
                if seq != expected:
                    dropped = (seq - expected) & 0xFFFF
                    print(f"[ctrl] ⚠ seq gap: expected {expected}, got {seq}"
                          f" ({dropped} dropped)")
            last_seq = seq
            pkts_ok += 1

            # Update shared state
            with _state_lock:
                _state["global_time"] = data["global_time"]
                _state["n_active"]    = data["n_active"]
                _state["sources"]     = data["sources"]

            # Mirror to regfile and print
            regs = _apply_to_regfile(data)
            print(f"[PL] seq={seq:05d}  t={data['global_time']:>4d}"
                  f"  n={data['n_active']}  "
                  f"reg0={regs[0]:#010x}")
            for i, s in enumerate(data["sources"]):
                r = s["_raw"]
                print(f"     src{i}: amp={r['amplitude']:>3d}"
                      f"  freq={r['frequency']:>3d}"
                      f"  dstr={r['dir_strength']:>3d}"
                      f"  dir={r['direction']:>2d}"
                      f"  x={r['x']:>3d}  y={r['y']:>3d}"
                      f"  → reg{1+i*2}={regs[1+i*2]:#010x}"
                      f"    reg{2+i*2}={regs[2+i*2]:#010x}")

    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        conn.close()
        print(f"[ctrl] {addr} disconnected  "
              f"(ok={pkts_ok} bad={pkts_bad})")


def _ctrl_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, CTRL_PORT))
    srv.listen(4)
    print(f"[ctrl] listening on :{CTRL_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_ctrl,
                         args=(conn, addr), daemon=True).start()


# ── Frame generator — driven by decoded antenna state ─────────────────────────

def _generate_frame() -> np.ndarray:
    """
    Compute E(x,y,t) = Σ_n  A_n · D_n(θ) · cos(ω·t - k·r + φ_n) / (r/scale + 1)

    D_n(θ) = 1 - S_n · (1 - cos(θ - dir_n)) / 2   (cardioid, S=dir_strength)
    global_time is used directly as the discretised ωt.
    """
    with _state_lock:
        t_raw   = _state["global_time"]
        sources = list(_state["sources"])

    # Map global_time (0-8192) → phase angle (0-2π)
    t_phase = (t_raw / 8192.0) * 2.0 * math.pi

    field = np.zeros((H, W), dtype=np.float32)

    for s in sources:
        if not s.get("active", False):
            continue
        cx   = float(s.get("x", W / 2))
        cy   = float(s.get("y", H / 2))
        amp  = float(s.get("amplitude",    0.75))
        freq = float(s.get("frequency",    1.0))
        dstr = float(s.get("dir_strength", 0.0))
        dirr = float(s.get("direction",    0.0)) * math.pi / 180.0

        r     = np.hypot(_XX - cx, _YY - cy) + 1e-6
        theta = np.arctan2(_YY - cy, _XX - cx)

        # Directional pattern — cardioid that degrades to isotropic when dstr=0
        D     = 1.0 - dstr * (1.0 - np.cos(theta - dirr)) / 2.0

        # Far-field wave with 1/r amplitude decay
        field += amp * D * np.cos(2.0 * np.pi * freq * r / 80.0 - t_phase) \
                         / (r / 40.0 + 1.0)

    lo, hi = field.min(), field.max()
    if hi > lo:
        field = (field - lo) / (hi - lo)
    else:
        field[:] = 0.5
    return (field * 255).astype(np.uint8)


# ── Frame server — port 5000 ──────────────────────────────────────────────────

def _handle_frame(conn, addr):
    print(f"[frame] {addr} connected")
    n, t0   = 0, time.monotonic()
    period  = 1.0 / TARGET_FPS
    try:
        while True:
            tick    = time.monotonic()
            frame   = _generate_frame()
            conn.sendall(struct.pack(">I", FRAME_BYTES) + frame.tobytes())
            n += 1
            now = time.monotonic()
            if now - t0 >= 1.0:
                print(f"[frame] {n / (now - t0):.1f} fps  "
                      f"(t={_state['global_time']:>4d})")
                n, t0 = 0, now
            sleep = period - (time.monotonic() - tick)
            if sleep > 0:
                time.sleep(sleep)
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        conn.close()
        print(f"[frame] {addr} disconnected")


def _frame_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, FRAME_PORT))
    srv.listen(1)
    print(f"[frame] listening on :{FRAME_PORT}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_frame,
                         args=(conn, addr), daemon=True).start()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=_ctrl_server,  daemon=True).start()
    threading.Thread(target=_frame_server, daemon=True).start()
    print(f"fpga_sim running — frames:{FRAME_PORT}  ctrl:{CTRL_PORT}  "
          f"packet:{PACKET_BYTES}B")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Shutting down.")