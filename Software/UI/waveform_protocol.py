"""
WaveForm control protocol — byte-aligned, AXI-DMA-friendly snapshot packet.

Transport: laptop client --TCP--> PS, then PS --AXI DMA (MM2S, 64-bit AXIS)--> PL.

Design notes
------------
* Full-state snapshot, not a delta. Every packet carries the complete state of
  all 5 source slots, so the protocol is idempotent: a lost or duplicated packet
  is fully corrected by the next one.
* Every field is byte aligned and the packet is a whole number of 64-bit (8-byte)
  AXIS beats — one beat per logical unit. The PL slices fields straight off the
  TDATA byte lanes: no bit shifting, no cross-beat straddling, no TKEEP handling.

Layout — little-endian, 48 bytes = 6 x 64-bit beats
---------------------------------------------------
  Beat 0  header (8 bytes)
    [0]    n_active      u8    0-5      live source count
    [1]    flags         u8    bit0 = paused
    [2:4]  global_time   u16   0-65535  PS time counter
    [4:8]  reserved      ----  zero (expansion room / 64-bit padding)

  Beats 1..5  source i (8 bytes each, all 5 slots always written)
    [0]    amplitude     u8    0-255    / 255   -> 0.0-1.0
    [1]    frequency     u8    0-255    / 25.5  -> 0.1-10.0
    [2]    directivity   u8    0-255    / 255   -> 0.0-1.0   (antenna.a)
    [3]    direction     u8    0-71     * 5deg  -> radians   (antenna.theta_0)
    [4:6]  x             u16   0-639    pixel column
    [6:8]  y             u16   0-479    pixel row

In SystemVerilog the source beat decodes with no logic, e.g.:
    amp  = tdata[ 7: 0];  freq = tdata[15: 8];  dir = tdata[23:16];
    th   = tdata[31:24];  x    = tdata[47:32];  y   = tdata[63:48];
"""

import math
import struct

MAX_SOURCES = 5

_HEADER_FMT  = "<BBH4x"      # n_active, flags, global_time, 4 reserved bytes
_SOURCE_FMT  = "<BBBBHH"     # amplitude, frequency, directivity, direction, x, y
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)              # 8
_SOURCE_SIZE = struct.calcsize(_SOURCE_FMT)              # 8
PACKET_BYTES = _HEADER_SIZE + MAX_SOURCES * _SOURCE_SIZE  # 48

assert _HEADER_SIZE == 8 and _SOURCE_SIZE == 8, "beats must be 8 bytes (64-bit AXIS)"
assert PACKET_BYTES == 48, f"packet must be 48 bytes, got {PACKET_BYTES}"

_FLAG_PAUSED = 0x01

# Pixel bounds (must match the simulator field resolution).
_FIELD_W = 640
_FIELD_H = 480


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# ── Quantisation: physical units -> wire integers ──────────────────────────────
def q_amplitude(v: float) -> int:
    """0.0-1.0 -> 0-255."""
    return _clamp(int(round(v * 255.0)), 0, 255)


def q_frequency(v: float) -> int:
    """0.1-10.0 -> 0-255 (x25.5)."""
    return _clamp(int(round(v * 25.5)), 0, 255)


def q_directivity(v: float) -> int:
    """0.0-1.0 -> 0-255 (antenna.a)."""
    return _clamp(int(round(v * 255.0)), 0, 255)


def q_direction(rad: float) -> int:
    """Radians (0-2pi) -> 0-71, snapped to the 5-degree grid."""
    step = int(round(math.degrees(rad) / 5.0)) % 72
    return _clamp(step, 0, 71)


# ── Dequantisation: wire integers -> physical units ────────────────────────────
def dq_amplitude(v: int) -> float:
    return v / 255.0


def dq_frequency(v: int) -> float:
    return v / 25.5


def dq_directivity(v: int) -> float:
    return v / 255.0


def dq_direction(v: int) -> float:
    """0-71 -> radians (v * 5deg), ready for np.cos(theta - theta_0)."""
    return math.radians(v * 5.0)


# ── Public API ─────────────────────────────────────────────────────────────────
def pack_packet(global_time: int, paused: bool, sources: list) -> bytes:
    """
    Pack a full control snapshot into a fixed 48-byte packet.

    sources: list of dicts with keys amplitude, frequency, a, theta_0, x, y.
             Up to MAX_SOURCES are encoded; inactive slots are zero-filled.
    """
    n_active = _clamp(len(sources), 0, MAX_SOURCES)
    flags    = _FLAG_PAUSED if paused else 0

    out = bytearray()
    out += struct.pack(_HEADER_FMT, n_active, flags, global_time & 0xFFFF)

    for i in range(MAX_SOURCES):
        if i < n_active:
            s = sources[i]
            out += struct.pack(
                _SOURCE_FMT,
                q_amplitude(s["amplitude"]),
                q_frequency(s["frequency"]),
                q_directivity(s["a"]),
                q_direction(s["theta_0"]),
                _clamp(int(s["x"]), 0, _FIELD_W - 1),
                _clamp(int(s["y"]), 0, _FIELD_H - 1),
            )
        else:
            out += struct.pack(_SOURCE_FMT, 0, 0, 0, 0, 0, 0)

    return bytes(out)


def unpack_packet(raw: bytes) -> dict:
    """
    Decode a 48-byte packet.

    Returns a dict with global_time, n_active, paused, and sources — a list of
    n_active dicts each holding amplitude, frequency, a, theta_0 (radians),
    x, y (pixels), plus a _raw dict of the pre-decoded integer fields.

    Raises ValueError on wrong length.
    """
    if len(raw) != PACKET_BYTES:
        raise ValueError(f"expected {PACKET_BYTES} bytes, got {len(raw)}")

    n_active, flags, global_time = struct.unpack(_HEADER_FMT, raw[:_HEADER_SIZE])
    n_active = _clamp(n_active, 0, MAX_SOURCES)
    paused   = bool(flags & _FLAG_PAUSED)

    sources = []
    for i in range(n_active):
        off = _HEADER_SIZE + i * _SOURCE_SIZE
        amp_i, freq_i, direc_i, direction_i, x, y = struct.unpack(
            _SOURCE_FMT, raw[off:off + _SOURCE_SIZE])
        sources.append({
            "amplitude": dq_amplitude(amp_i),
            "frequency": dq_frequency(freq_i),
            "a":         dq_directivity(direc_i),
            "theta_0":   dq_direction(direction_i),
            "x":         x,
            "y":         y,
            "_raw": {
                "amplitude":   amp_i,
                "frequency":   freq_i,
                "directivity": direc_i,
                "direction":   direction_i,
                "x":           x,
                "y":           y,
            },
        })

    return {
        "global_time": global_time,
        "n_active":    n_active,
        "paused":      paused,
        "sources":     sources,
    }
