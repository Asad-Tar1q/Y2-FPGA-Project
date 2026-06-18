#!/usr/bin/env python3
import socket
import struct
import threading
import time
import traceback

import numpy as np
from pynq import Overlay, allocate

BITFILE = "/home/xilinx/jupyter_notebooks/EMWaves/test7/base.bit"
PIXGEN_PATH = "pixel_generator_0"
DMA_PATH = "axi_dma_0"

HOST = "0.0.0.0"
FRAME_PORT = 5000
CTRL_PORT = 5001

WIDTH = 640
HEIGHT = 480
FRAME_BYTES = WIDTH * HEIGHT
FRAME_HEADER = struct.pack(">I", FRAME_BYTES)

MAX_HW_SOURCES = 8
MAX_WALLS = 4
REG_TIME = 0
REG_CTRL = 1
SRC_BASE = 4
SRC_STRIDE = 4
WALL_BASE = SRC_BASE + MAX_HW_SOURCES * SRC_STRIDE
WALL_STRIDE = 3

SCENE_MAGIC = b"WFSC"
SCENE_VERSION = 1
SCENE_HDR_FMT = ">4sBBBBH"
SRC_REC_FMT = ">hhhhBBBBbbBB"
WALL_REC_FMT = ">hhhhBBBB"
SCENE_HDR_SIZE = struct.calcsize(SCENE_HDR_FMT)
SRC_REC_SIZE = struct.calcsize(SRC_REC_FMT)
WALL_REC_SIZE = struct.calcsize(WALL_REC_FMT)

state_lock = threading.Lock()
hw_lock = threading.Lock()
stop_evt = threading.Event()

paused = False
frame_time = 0
sources = []
walls = []

pixgen = None
dma = None
_last_regs = {}


def i16(v):
    v = int(round(v))
    return max(-32768, min(32767, v))


def u8(v):
    return max(0, min(255, int(round(v))))


def pack_i16_xy(x, y):
    return ((i16(y) & 0xFFFF) << 16) | (i16(x) & 0xFFFF)


def write_reg(index, value, force=False):
    value = int(value) & 0xFFFFFFFF
    if force or _last_regs.get(index) != value:
        pixgen.write(index * 4, value)
        _last_regs[index] = value


def clear_hw_scene_locked():
    for i in range(MAX_HW_SOURCES):
        b = SRC_BASE + i * SRC_STRIDE
        write_reg(b + 0, 0, True)
        write_reg(b + 1, 0, True)
        write_reg(b + 2, 0, True)
        write_reg(b + 3, 0, True)

    for i in range(MAX_WALLS):
        b = WALL_BASE + i * WALL_STRIDE
        write_reg(b + 0, 0, True)
        write_reg(b + 1, 0, True)
        write_reg(b + 2, 0, True)

    write_reg(REG_CTRL, 0, True)
    write_reg(REG_TIME, 0, True)


def write_scene_to_hw():
    with state_lock:
        src_snapshot = list(sources[:MAX_HW_SOURCES])
        wall_snapshot = list(walls[:MAX_WALLS])
        local_paused = paused

    regs = {}

    for i in range(MAX_HW_SOURCES):
        b = SRC_BASE + i * SRC_STRIDE
        if i < len(src_snapshot):
            s = src_snapshot[i]
            enable = 1 if s.get("enable", True) else 0
            moving = 1 if s.get("moving", False) else 0
            virtual = 1 if s.get("virtual", False) else 0
            phase_inv = 1 if s.get("phase_inv", False) else 0
            wall_id = int(s.get("wall_id", 0)) & 0x0F
            amp = u8(s.get("amp", 0))
            freq = u8(s.get("freq", 16))
            phase = u8(s.get("phase", 0))
            directivity = u8(s.get("directivity", 0))
            dirx = i16(s.get("dirx", 127)) & 0xFF
            diry = i16(s.get("diry", 0)) & 0xFF
            vx = i16(s.get("vx", 0))
            vy = i16(s.get("vy", 0))

            regs[b + 0] = pack_i16_xy(s.get("x", 0), s.get("y", 0))
            regs[b + 1] = (enable |
                           (moving << 1) |
                           (virtual << 2) |
                           (phase_inv << 3) |
                           (wall_id << 4) |
                           (amp << 8) |
                           (freq << 16) |
                           (phase << 24))
            regs[b + 2] = dirx | (diry << 8) | (directivity << 16)
            regs[b + 3] = (vx & 0xFFFF) | ((vy & 0xFFFF) << 16)
        else:
            regs[b + 0] = 0
            regs[b + 1] = 0
            regs[b + 2] = 0
            regs[b + 3] = 0

    for i in range(MAX_WALLS):
        b = WALL_BASE + i * WALL_STRIDE
        if i < len(wall_snapshot):
            w = wall_snapshot[i]
            enable = 1 if w.get("enable", True) else 0
            reflect = 1 if w.get("type", 0) == 1 else 0
            phase_inv = 1 if w.get("phase_inv", False) else 0
            gain = u8(w.get("gain", 160))
            regs[b + 0] = pack_i16_xy(w.get("x1", 0), w.get("y1", 0))
            regs[b + 1] = pack_i16_xy(w.get("x2", 0), w.get("y2", 0))
            regs[b + 2] = enable | (reflect << 1) | (phase_inv << 2) | (gain << 8)
        else:
            regs[b + 0] = 0
            regs[b + 1] = 0
            regs[b + 2] = 0

    ctrl = ((1 if local_paused else 0) |
            ((len(src_snapshot) & 0xFF) << 8) |
            ((len(wall_snapshot) & 0xFF) << 16))

    with hw_lock:
        for idx in sorted(regs):
            write_reg(idx, regs[idx])
        write_reg(REG_CTRL, ctrl)


def parse_scene_packet(payload):
    if len(payload) < SCENE_HDR_SIZE:
        raise ValueError("short scene packet")

    magic, version, flags, n_src, n_wall, _ = struct.unpack_from(SCENE_HDR_FMT, payload, 0)
    if magic != SCENE_MAGIC:
        raise ValueError("bad scene magic")
    if version != SCENE_VERSION:
        raise ValueError(f"unsupported scene version {version}")

    pos = SCENE_HDR_SIZE
    new_sources = []
    for _ in range(min(n_src, MAX_HW_SOURCES)):
        if pos + SRC_REC_SIZE > len(payload):
            raise ValueError("truncated source records")
        x, y, vx, vy, amp, freq, phase, directivity, dirx, diry, sflags, wall_id = struct.unpack_from(SRC_REC_FMT, payload, pos)
        pos += SRC_REC_SIZE
        new_sources.append({
            "x": x, "y": y, "vx": vx, "vy": vy,
            "amp": amp, "freq": freq, "phase": phase,
            "directivity": directivity, "dirx": dirx, "diry": diry,
            "enable": bool(sflags & 0x01),
            "moving": bool(sflags & 0x02),
            "virtual": bool(sflags & 0x04),
            "phase_inv": bool(sflags & 0x08),
            "wall_id": wall_id,
        })

    pos += max(0, n_src - MAX_HW_SOURCES) * SRC_REC_SIZE

    new_walls = []
    for _ in range(min(n_wall, MAX_WALLS)):
        if pos + WALL_REC_SIZE > len(payload):
            raise ValueError("truncated wall records")
        x1, y1, x2, y2, enable, wall_type, gain, phase_inv = struct.unpack_from(WALL_REC_FMT, payload, pos)
        pos += WALL_REC_SIZE
        new_walls.append({
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "enable": bool(enable),
            "type": int(wall_type),
            "gain": gain,
            "phase_inv": bool(phase_inv),
        })

    return bool(flags & 0x01), new_sources, new_walls


def recv_exact(conn, n):
    buf = bytearray(n)
    view = memoryview(buf)
    got = 0
    while got < n:
        r = conn.recv_into(view[got:], n - got)
        if r == 0:
            raise ConnectionResetError("socket closed")
        got += r
    return bytes(buf)


def handle_ctrl(conn, addr):
    global paused, sources, walls
    print(f"[ctrl] connected {addr}")
    last_payload = None
    try:
        while not stop_evt.is_set():
            header = recv_exact(conn, 4)
            length = struct.unpack(">I", header)[0]
            if length <= 0 or length > 65536:
                raise ValueError(f"invalid control packet length {length}")
            payload = recv_exact(conn, length)
            if payload == last_payload:
                continue
            last_payload = payload
            p, s, w = parse_scene_packet(payload)
            with state_lock:
                paused = p
                sources = s
                walls = w
            write_scene_to_hw()
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    except Exception:
        print("[ctrl] unexpected exception:")
        traceback.print_exc()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        print(f"[ctrl] disconnected {addr}")


def ctrl_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, CTRL_PORT))
    srv.listen(2)
    print(f"[ctrl] listening on {HOST}:{CTRL_PORT}")
    while not stop_evt.is_set():
        try:
            conn, addr = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=handle_ctrl, args=(conn, addr), daemon=True).start()
        except OSError:
            break
        except Exception:
            traceback.print_exc()
            time.sleep(0.2)


def send_frame(conn, view):
    conn.sendall(FRAME_HEADER)
    conn.sendall(view)


def frame_server():
    global frame_time
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, FRAME_PORT))
    srv.listen(1)
    print(f"[frame] listening on {HOST}:{FRAME_PORT}")

    buf_a = allocate(shape=(HEIGHT, WIDTH), dtype=np.uint8)
    buf_b = allocate(shape=(HEIGHT, WIDTH), dtype=np.uint8)
    bufs = [buf_a, buf_b]
    views = [memoryview(buf_a.reshape(FRAME_BYTES)), memoryview(buf_b.reshape(FRAME_BYTES))]

    while not stop_evt.is_set():
        conn, addr = srv.accept()
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        except Exception:
            pass
        print(f"[frame] connected {addr}")

        frames = 0
        t0 = time.monotonic()
        dma_total = 0.0
        tcp_total = 0.0
        idx = 0
        transfer_active = False

        try:
            with hw_lock:
                write_reg(REG_TIME, frame_time, True)
            dma.recvchannel.transfer(bufs[idx])
            transfer_active = True

            while not stop_evt.is_set():
                t_dma0 = time.perf_counter()
                dma.recvchannel.wait()
                transfer_active = False
                t_dma1 = time.perf_counter()

                done_idx = idx
                idx ^= 1

                with state_lock:
                    local_paused = paused
                if not local_paused:
                    frame_time = (frame_time + 1) & 0xFFFFFFFF

                with hw_lock:
                    write_reg(REG_TIME, frame_time, True)

                dma.recvchannel.transfer(bufs[idx])
                transfer_active = True

                t_tcp0 = time.perf_counter()
                send_frame(conn, views[done_idx])
                t_tcp1 = time.perf_counter()

                dma_total += t_dma1 - t_dma0
                tcp_total += t_tcp1 - t_tcp0
                frames += 1

                now = time.monotonic()
                if now - t0 >= 1.0:
                    print(f"[frame] {frames / (now - t0):5.1f} fps | DMA {dma_total/max(frames,1)*1e3:5.2f} ms | TCP {tcp_total/max(frames,1)*1e3:5.2f} ms")
                    frames = 0
                    dma_total = 0.0
                    tcp_total = 0.0
                    t0 = now
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass
        except Exception:
            print("[frame] unexpected exception:")
            traceback.print_exc()
        finally:
            if transfer_active:
                try:
                    dma.recvchannel.wait()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass
            print(f"[frame] disconnected {addr}")


def main():
    global pixgen, dma
    print(f"[regmap] MAX_HW_SOURCES={MAX_HW_SOURCES} MAX_WALLS={MAX_WALLS} WALL_BASE={WALL_BASE}")
    print(f"Loading overlay: {BITFILE}")
    overlay = Overlay(BITFILE)
    pixgen = getattr(overlay, PIXGEN_PATH)
    dma = getattr(overlay, DMA_PATH)

    with hw_lock:
        clear_hw_scene_locked()

    threading.Thread(target=ctrl_server, daemon=True).start()
    try:
        frame_server()
    except KeyboardInterrupt:
        print("Stopping")
    finally:
        stop_evt.set()


if __name__ == "__main__":
    main()
