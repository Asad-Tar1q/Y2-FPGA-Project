"""
waveform_protocol.py  —  WaveForm bit-packed control protocol

All floating-point values are quantised to integers before packing.
No floats cross the wire — the buffer contains only machine integers.

Packet layout — 294 bits → 37 bytes (2 zero-padding bits at end):

  Bits    Field           Width   Range / encoding
  ────────────────────────────────────────────────────────────────
   0-15   magic           16 b    0x574F  ("WF" framing sentinel)
  16-31   seq             16 b    0-65535, wraps — loss/latency tracking
  32-34   n_active         3 b    0-5     number of live sources
  35-48   global_time     14 b    0-8192  PS time counter (ωt proxy)
  ────────────────────────────────────────────────────────────────
  Per source × 5  (49 bits each, sources packed contiguously):
  +0      amplitude        8 b    0-255   → 0.000-1.000  (÷ 255)
  +8      frequency        7 b    1-100   → 0.1-10.0     (÷ 10)
  +15     dir_strength     8 b    0-255   → 0.000-1.000  (÷ 255)
  +23     direction        7 b    0-71    → 0-355°        (× 5)
  +30     x               10 b    0-639   pixel column
  +40     y                9 b    0-479   pixel row
  ────────────────────────────────────────────────────────────────
  292-293 padding           2 b    0x0
  ────────────────────────────────────────────────────────────────
  Total: 294 bits = 37 bytes

FPGA register map (AXI-Lite, 32-bit words, PS writes one burst per frame):

  Reg 0   [15:0]  global_time   [18:16] n_active   [31:19] unused
  Reg 1   [7:0]   src0.amp      [15:8]  src0.dstr  [22:16] src0.dir
          [31:23] src0.freq (7b split: [29:23])   — or use reg 2
  Reg 2   [6:0]   src0.freq     [16:7]  src0.x     [25:17] src0.y
  Reg 3-4  src1 (same layout)
  ...
  Reg 9-10 src4
"""

import math
import struct

# ── Constants ─────────────────────────────────────────────────────────────────
MAGIC            = 0x574F          # ASCII "WF"
N_SOURCES        = 5
_HEADER_BITS     = 16 + 16 + 3 + 14          # magic + seq + n_active + time = 49
_SOURCE_BITS     = 8 + 7 + 8 + 7 + 10 + 9   # per-source = 49
PACKET_BITS      = _HEADER_BITS + N_SOURCES * _SOURCE_BITS   # = 294
PACKET_BYTES     = math.ceil(PACKET_BITS / 8)                 # = 37

assert PACKET_BYTES == 37, f"Protocol change: expected 37 bytes, got {PACKET_BYTES}"

# Individual field bit widths
_MAGIC_BITS   = 16
_SEQ_BITS     = 16
_NACT_BITS    = 3
_TIME_BITS    = 14    # 0-16383 → comfortably covers 0-8192
_AMP_BITS     = 8
_FREQ_BITS    = 7
_DSTR_BITS    = 8
_DIR_BITS     = 7
_X_BITS       = 10
_Y_BITS       = 9


# ── Quantisers — float → integer (these run on the client, no floats on wire) ─

def q_amplitude(v: float) -> int:
    """0.0-1.0  →  0-255"""
    return min(255, max(0, round(v * 255)))

def q_frequency(v: float) -> int:
    """0.1-10.0 Hz multiplier  →  1-100"""
    return min(100, max(1, round(v * 10)))

def q_dir_strength(v: float) -> int:
    """0.0-1.0  →  0-255"""
    return min(255, max(0, round(v * 255)))

def q_direction(deg: float) -> int:
    """0-355° in 5° steps  →  0-71"""
    return int(round(deg / 5)) % 72

def q_global_time(t: int) -> int:
    """0-8192  →  0-8192 (fits in 14 bits)"""
    return min(8192, max(0, int(t)))

def q_x(x: int) -> int:
    return min(639, max(0, int(x)))

def q_y(y: int) -> int:
    return min(479, max(0, int(y)))


# ── Dequantisers — integer → float (run on server / benchmark side) ───────────

def dq_amplitude(v: int)    -> float: return v / 255.0
def dq_frequency(v: int)    -> float: return v / 10.0
def dq_dir_strength(v: int) -> float: return v / 255.0
def dq_direction(v: int)    -> float: return float(v * 5)


# ── Theoretical max quantisation errors (useful for benchmark accuracy table) ─
MAX_ERR = {
    "amplitude":    0.5 / 255,          # ≈ 0.00196
    "frequency":    0.5 / 10,           # ≈ 0.050 Hz multiplier
    "dir_strength": 0.5 / 255,          # ≈ 0.00196
    "direction":    2.5,                # ± 2.5°
    "x":            0,                  # exact integer
    "y":            0,                  # exact integer
    "global_time":  0,                  # exact integer
}


# ── Bit packer — MSB first, zero-initialised buffer ──────────────────────────

class _BitPacker:
    __slots__ = ("_buf", "_pos")

    def __init__(self, total_bits: int):
        self._buf = bytearray(math.ceil(total_bits / 8))
        self._pos = 0

    def pack(self, value: int, nbits: int) -> None:
        """Write `nbits` bits of `value` MSB-first into the buffer."""
        for i in range(nbits - 1, -1, -1):
            if (value >> i) & 1:
                self._buf[self._pos >> 3] |= 1 << (7 - (self._pos & 7))
            self._pos += 1

    def to_bytes(self) -> bytes:
        return bytes(self._buf)


class _BitUnpacker:
    __slots__ = ("_buf", "_pos")

    def __init__(self, data: bytes):
        self._buf = data
        self._pos = 0

    def unpack(self, nbits: int) -> int:
        """Read `nbits` bits MSB-first from the buffer."""
        v = 0
        for _ in range(nbits):
            v = (v << 1) | ((self._buf[self._pos >> 3] >> (7 - (self._pos & 7))) & 1)
            self._pos += 1
        return v


# ── Public API ────────────────────────────────────────────────────────────────

def pack_packet(seq: int, global_time: int, sources: list) -> bytes:
    """
    Quantise and bit-pack a full control snapshot into PACKET_BYTES = 37 bytes.

    All conversion from float to integer happens inside this function.
    The returned bytes contain only machine-width integers — no IEEE 754 floats.

    Parameters
    ----------
    seq         : uint16, wrapping sequence counter (for loss detection)
    global_time : int 0-8192, the PS time counter
    sources     : list of dicts (up to N_SOURCES = 5), each with:
                    amplitude    float  0.0-1.0
                    frequency    float  0.1-10.0
                    dir_strength float  0.0-1.0
                    direction    float  0-355  (quantised to 5° grid)
                    x            int    0-639
                    y            int    0-479
    """
    n  = min(N_SOURCES, len(sources))
    bp = _BitPacker(PACKET_BITS)

    # ── Header ────────────────────────────────────────────────────────────────
    bp.pack(MAGIC,                       _MAGIC_BITS)
    bp.pack(seq & 0xFFFF,                _SEQ_BITS)
    bp.pack(n,                           _NACT_BITS)
    bp.pack(q_global_time(global_time),  _TIME_BITS)

    # ── Sources (all 5 slots always written; inactive slots zero-padded) ──────
    for i in range(N_SOURCES):
        s = sources[i] if i < n else {}
        bp.pack(q_amplitude   (s.get("amplitude",    0.0)), _AMP_BITS)
        bp.pack(q_frequency   (s.get("frequency",    1.0)), _FREQ_BITS)
        bp.pack(q_dir_strength(s.get("dir_strength", 0.0)), _DSTR_BITS)
        bp.pack(q_direction   (s.get("direction",    0.0)), _DIR_BITS)
        bp.pack(q_x           (s.get("x",              0)), _X_BITS)
        bp.pack(q_y           (s.get("y",              0)), _Y_BITS)

    return bp.to_bytes()


def unpack_packet(raw: bytes) -> dict:
    """
    Decode a PACKET_BYTES-length bit-packed control packet.

    Returns
    -------
    dict with:
        seq         int
        global_time int   (0-8192)
        n_active    int   (0-5)
        sources     list  of dicts (n_active entries), each:
            amplitude    float
            frequency    float
            dir_strength float
            direction    float (degrees)
            x            int
            y            int
            _raw         dict of pre-dequantised integers (for direct regfile writes)

    Raises ValueError on bad magic or insufficient data.
    """
    if len(raw) < PACKET_BYTES:
        raise ValueError(f"Packet too short: {len(raw)} < {PACKET_BYTES}")

    bp = _BitUnpacker(raw)

    magic       = bp.unpack(_MAGIC_BITS)
    seq         = bp.unpack(_SEQ_BITS)
    n_active    = bp.unpack(_NACT_BITS)
    global_time = bp.unpack(_TIME_BITS)

    if magic != MAGIC:
        raise ValueError(f"Bad magic: {magic:#06x}  (expected {MAGIC:#06x})")

    sources = []
    for i in range(N_SOURCES):
        amp_q  = bp.unpack(_AMP_BITS)
        freq_q = bp.unpack(_FREQ_BITS)
        dstr_q = bp.unpack(_DSTR_BITS)
        dir_q  = bp.unpack(_DIR_BITS)
        x_raw  = bp.unpack(_X_BITS)
        y_raw  = bp.unpack(_Y_BITS)
        sources.append({
            "active":       i < n_active,
            "amplitude":    dq_amplitude(amp_q),
            "frequency":    dq_frequency(freq_q),
            "dir_strength": dq_dir_strength(dstr_q),
            "direction":    dq_direction(dir_q),
            "x":            x_raw,
            "y":            y_raw,
            "_raw": {               # integer values — write directly to regfile
                "amplitude":    amp_q,
                "frequency":    freq_q,
                "dir_strength": dstr_q,
                "direction":    dir_q,
                "x":            x_raw,
                "y":            y_raw,
            },
        })

    return {
        "seq":         seq,
        "global_time": global_time,
        "n_active":    n_active,
        "sources":     sources[:n_active],
    }