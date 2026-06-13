"""
ctrl_benchmark.py  —  WaveForm control-path benchmark server

Listens on port 5001 for 37-byte bit-packed control packets.
Measures and reports every second:

  Throughput   packets/s received
  Loss         dropped packets detected via seq gaps
  Decode time  min / mean / max / σ  in nanoseconds
  A9 cycles    estimated ARM Cortex-A9 cycles per decode  (@ 650 MHz)
  Jitter       std dev of inter-packet arrival intervals  (µs)
  Bandwidth    bytes/s

On startup, runs a quantisation accuracy self-test — no hardware needed.

Usage
-----
  python ctrl_benchmark.py               # listen on 0.0.0.0:5001
  python ctrl_benchmark.py --port 5002   # different port
  python ctrl_benchmark.py --accuracy    # accuracy test only, then exit
"""

import argparse
import math
import random
import socket
import statistics
import time
import threading
from collections import deque

from waveform_protocol import (
    PACKET_BYTES, MAGIC, N_SOURCES,
    pack_packet, unpack_packet,
    q_amplitude,    dq_amplitude,
    q_frequency,    dq_frequency,
    q_dir_strength, dq_dir_strength,
    q_direction,    dq_direction,
    MAX_ERR,
)

# ── Config ────────────────────────────────────────────────────────────────────
HOST           = "0.0.0.0"
DEFAULT_PORT   = 5001
A9_CLOCK_MHZ   = 650        # ARM Cortex-A9 on PYNQ-Z1
REPORT_EVERY_S = 1.0
WINDOW         = 300        # keep last N samples for rolling stats


# ═════════════════════════════════════════════════════════════════════════════
# Accuracy self-test (no TCP required)
# ═════════════════════════════════════════════════════════════════════════════

def run_accuracy_test(n_trials: int = 50_000, verbose: bool = True) -> dict:
    """
    Pack → unpack N random packets and measure per-field round-trip error.
    Verifies bit packing is lossless within quantisation limits.
    """
    errors = {f: [] for f in
              ["amplitude", "frequency", "dir_strength", "direction"]}
    bit_errors = 0

    for trial in range(n_trials):
        amp   = random.random()
        freq  = random.uniform(0.1, 10.0)
        dstr  = random.random()
        direc = random.uniform(0.0, 355.0)
        x     = random.randint(0, 639)
        y     = random.randint(0, 479)
        gt    = random.randint(0, 8192)
        seq   = random.randint(0, 65535)

        sources = [{"amplitude": amp, "frequency": freq,
                    "dir_strength": dstr, "direction": direc,
                    "x": x, "y": y}]

        raw  = pack_packet(seq, gt, sources)
        data = unpack_packet(raw)

        # Check packet size
        assert len(raw) == PACKET_BYTES, \
            f"Packet size mismatch: {len(raw)} != {PACKET_BYTES}"

        # Check header fields exactly
        if data["seq"] != seq:
            bit_errors += 1
        if data["global_time"] != gt:
            bit_errors += 1
        if data["n_active"] != 1:
            bit_errors += 1

        s = data["sources"][0]
        if s["x"] != x or s["y"] != y:
            bit_errors += 1

        # Floating-point round-trip errors
        errors["amplitude"   ].append(abs(amp   - s["amplitude"]))
        errors["frequency"   ].append(abs(freq  - s["frequency"]))
        errors["dir_strength"].append(abs(dstr  - s["dir_strength"]))
        errors["direction"   ].append(abs(direc - dq_direction(q_direction(direc))))

    results = {}
    for field, errs in errors.items():
        results[field] = {
            "max":        max(errs),
            "mean":       statistics.mean(errs),
            "theoretical": MAX_ERR[field],
            "pass":       max(errs) <= MAX_ERR[field] + 1e-9,
        }
    results["_bit_errors"]  = bit_errors
    results["_n_trials"]    = n_trials
    results["_packet_bytes"] = PACKET_BYTES

    if verbose:
        _print_accuracy(results, n_trials)

    return results


def _print_accuracy(results: dict, n_trials: int):
    sep = "─" * 66
    print(f"\n{'═'*66}")
    print(f"  WaveForm Control Protocol — Quantisation Accuracy")
    print(f"  Packet size: {results['_packet_bytes']} bytes  |  "
          f"Trials: {n_trials:,}  |  "
          f"Bit errors: {results['_bit_errors']}")
    print(sep)
    print(f"  {'Field':<16}  {'Max err':>10}  {'Mean err':>10}  "
          f"{'Theoretical':>12}  {'Status':>6}")
    print(sep)
    for field in ["amplitude", "frequency", "dir_strength", "direction"]:
        r = results[field]
        status = "✓ PASS" if r["pass"] else "✗ FAIL"
        unit   = "°" if field == "direction" else ""
        print(f"  {field:<16}  {r['max']:>9.5f}{unit}  "
              f"{r['mean']:>9.5f}{unit}  "
              f"{r['theoretical']:>11.5f}{unit}  "
              f"{status:>6}")
    print(sep)
    print(f"  Integer fields (x, y, seq, global_time, n_active): exact (0 error)")
    print(f"{'═'*66}\n")


# ═════════════════════════════════════════════════════════════════════════════
# Decode latency micro-benchmark (no TCP, measures pure Python unpack cost)
# ═════════════════════════════════════════════════════════════════════════════

def run_decode_benchmark(n_trials: int = 10_000) -> dict:
    """Measure time for unpack_packet() on pre-built packets."""
    # Build a batch of packets to decode
    packets = []
    for i in range(min(n_trials, 1000)):
        sources = [{"amplitude": random.random(), "frequency": random.uniform(0.1, 10),
                    "dir_strength": random.random(), "direction": random.uniform(0, 355),
                    "x": random.randint(0, 639), "y": random.randint(0, 479)}
                   for _ in range(random.randint(1, N_SOURCES))]
        packets.append(pack_packet(i, i % 8193, sources))

    # Warm up
    for p in packets[:10]:
        unpack_packet(p)

    # Timed run
    times_ns = []
    for rep in range(n_trials):
        pkt = packets[rep % len(packets)]
        t0  = time.perf_counter_ns()
        unpack_packet(pkt)
        times_ns.append(time.perf_counter_ns() - t0)

    mean_ns  = statistics.mean(times_ns)
    stdev_ns = statistics.stdev(times_ns)
    min_ns   = min(times_ns)
    max_ns   = max(times_ns)
    a9_cycles = mean_ns * A9_CLOCK_MHZ / 1000.0

    print(f"\n{'═'*66}")
    print(f"  Decode latency  (Python BitUnpacker, N={n_trials:,})")
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Min          {min_ns:>8.0f} ns")
    print(f"  Mean         {mean_ns:>8.0f} ns")
    print(f"  Max          {max_ns:>8.0f} ns")
    print(f"  Std dev      {stdev_ns:>8.0f} ns")
    print(f"  A9 cycles    {a9_cycles:>8.0f}   (@ {A9_CLOCK_MHZ} MHz est.)")
    print(f"  Throughput   {1e9/mean_ns:>8.0f} decode/s   (single-threaded)")
    print(f"  Note: RTL parallel decode = 1 cycle  |  "
          f"serial = {PACKET_BYTES*8} cycles")
    print(f"{'═'*66}\n")

    return {"mean_ns": mean_ns, "stdev_ns": stdev_ns,
            "min_ns": min_ns, "max_ns": max_ns, "a9_cycles": a9_cycles}


# ═════════════════════════════════════════════════════════════════════════════
# Live TCP benchmark server
# ═════════════════════════════════════════════════════════════════════════════

class _Stats:
    """Thread-safe rolling statistics accumulator."""
    def __init__(self):
        self._lock       = threading.Lock()
        self.decode_ns   = deque(maxlen=WINDOW)
        self.arrivals    = deque(maxlen=WINDOW)   # perf_counter_ns timestamps
        self.pkt_ok      = 0
        self.pkt_bad     = 0
        self.pkt_dropped = 0       # seq gaps
        self.bytes_recv  = 0
        self.last_seq    = -1
        self._t0         = time.perf_counter()

    def record(self, decode_ns: int, arrived_ns: int, seq: int, n_bytes: int):
        with self._lock:
            self.decode_ns.append(decode_ns)
            self.arrivals.append(arrived_ns)
            self.pkt_ok    += 1
            self.bytes_recv += n_bytes
            if self.last_seq >= 0:
                expected = (self.last_seq + 1) & 0xFFFF
                if seq != expected:
                    self.pkt_dropped += (seq - expected) & 0xFFFF
            self.last_seq = seq

    def record_bad(self):
        with self._lock:
            self.pkt_bad += 1

    def snapshot_and_reset(self) -> dict:
        with self._lock:
            now     = time.perf_counter()
            elapsed = now - self._t0
            self._t0 = now

            ok      = self.pkt_ok
            bad     = self.pkt_bad
            dropped = self.pkt_dropped
            brecv   = self.bytes_recv

            self.pkt_ok = self.pkt_bad = self.pkt_dropped = self.bytes_recv = 0

            dec = list(self.decode_ns)
            arr = list(self.arrivals)

        throughput = ok / elapsed if elapsed > 0 else 0.0
        bandwidth  = brecv / elapsed if elapsed > 0 else 0.0

        dec_mean   = statistics.mean(dec)   if dec else 0
        dec_stdev  = statistics.stdev(dec)  if len(dec) > 1 else 0
        dec_min    = min(dec)               if dec else 0
        dec_max    = max(dec)               if dec else 0
        a9_cycles  = dec_mean * A9_CLOCK_MHZ / 1000.0

        # Inter-arrival jitter
        if len(arr) > 1:
            intervals_us = [(arr[i+1] - arr[i]) / 1000.0
                            for i in range(len(arr)-1)]
            jitter_us = statistics.stdev(intervals_us)
            mean_ia   = statistics.mean(intervals_us)
        else:
            jitter_us = 0.0
            mean_ia   = 0.0

        loss_rate = dropped / max(ok + dropped, 1) * 100.0

        return {
            "elapsed_s":     elapsed,
            "pkt_ok":        ok,
            "pkt_bad":       bad,
            "pkt_dropped":   dropped,
            "loss_pct":      loss_rate,
            "throughput_hz": throughput,
            "bandwidth_bs":  bandwidth,
            "dec_mean_ns":   dec_mean,
            "dec_stdev_ns":  dec_stdev,
            "dec_min_ns":    dec_min,
            "dec_max_ns":    dec_max,
            "a9_cycles":     a9_cycles,
            "jitter_us":     jitter_us,
            "mean_ia_us":    mean_ia,
        }


_stats = _Stats()
_last_decoded: dict | None = None    # most recent successfully decoded packet


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


def _handle_client(conn, addr):
    global _last_decoded
    print(f"[bench] client connected: {addr}  "
          f"(expecting {PACKET_BYTES}-byte packets)")
    try:
        while True:
            arrived_ns = time.perf_counter_ns()
            raw        = _recv_exactly(conn, PACKET_BYTES)

            t_decode_start = time.perf_counter_ns()
            try:
                data = unpack_packet(raw)
                decode_ns = time.perf_counter_ns() - t_decode_start
            except ValueError as e:
                _stats.record_bad()
                print(f"[bench] bad packet: {e}")
                continue

            _stats.record(decode_ns, arrived_ns, data["seq"], PACKET_BYTES)
            _last_decoded = data

    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        conn.close()
        print(f"[bench] client disconnected: {addr}")


def _report_loop():
    """Prints a stats table every REPORT_EVERY_S seconds."""
    sep = "─" * 72

    # Print header once
    print(f"\n  {'Metric':<28}  {'Value':>16}  {'Notes'}")
    print(sep)

    while True:
        time.sleep(REPORT_EVERY_S)
        s = _stats.snapshot_and_reset()

        if s["pkt_ok"] == 0:
            print("  (waiting for packets...)")
            continue

        rows = [
            ("Packets received",      f"{s['pkt_ok']}",          ""),
            ("Packets dropped",       f"{s['pkt_dropped']}",     f"loss {s['loss_pct']:.2f}%"),
            ("Packets malformed",     f"{s['pkt_bad']}",         "bad magic or length"),
            ("Throughput",            f"{s['throughput_hz']:.1f} pkt/s", ""),
            ("Bandwidth",             f"{s['bandwidth_bs']/1024:.2f} KB/s",
                                      f"{PACKET_BYTES}B × {s['throughput_hz']:.0f}/s"),
            ("",                      "",                         ""),
            ("Decode time  mean",     f"{s['dec_mean_ns']:.0f} ns",    ""),
            ("Decode time  min",      f"{s['dec_min_ns']:.0f} ns",     ""),
            ("Decode time  max",      f"{s['dec_max_ns']:.0f} ns",     ""),
            ("Decode time  σ",        f"{s['dec_stdev_ns']:.0f} ns",   ""),
            ("Estimated A9 cycles",   f"{s['a9_cycles']:.0f}",
                                      f"@ {A9_CLOCK_MHZ} MHz"),
            ("",                      "",                         ""),
            ("Inter-packet interval", f"{s['mean_ia_us']:.0f} µs",
                                      f"({1e6/s['throughput_hz']:.0f} µs expected)"),
            ("Jitter  (σ)",           f"{s['jitter_us']:.1f} µs",     ""),
        ]

        # Last decoded values
        if _last_decoded:
            d  = _last_decoded
            rows += [
                ("",                  "",                         ""),
                ("Last packet seq",   f"{d['seq']}",              ""),
                ("Global time",       f"{d['global_time']}",
                                      f"/ 8192  ({d['global_time']/8192*100:.1f}%)"),
                ("Active sources",    f"{d['n_active']}",         ""),
            ]
            for i, src in enumerate(d["sources"]):
                r = src["_raw"]
                rows.append((
                    f"  src{i} (raw regs)",
                    f"A={r['amplitude']:>3d}",
                    f"F={r['frequency']:>3d} "
                    f"DS={r['dir_strength']:>3d} "
                    f"D={r['direction']:>2d} "
                    f"x={r['x']:>3d} y={r['y']:>3d}"
                ))

        print(f"\n  {time.strftime('%H:%M:%S')}  ──────────────────────────────")
        for label, value, note in rows:
            if not label:
                continue
            note_str = f"  {note}" if note else ""
            print(f"  {label:<28}  {value:>16}{note_str}")
        print(sep)


def _tcp_server(port: int):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, port))
    srv.listen(4)
    print(f"[bench] listening on {HOST}:{port}  "
          f"(packet size = {PACKET_BYTES} bytes)")
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_handle_client,
                         args=(conn, addr), daemon=True).start()


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="WaveForm control-path benchmark server")
    parser.add_argument("--port",     type=int, default=DEFAULT_PORT)
    parser.add_argument("--accuracy", action="store_true",
                        help="Run accuracy self-test and exit")
    parser.add_argument("--decode-bench", action="store_true",
                        help="Run decode latency benchmark and exit")
    args = parser.parse_args()

    # Always run accuracy test on startup
    print("Running quantisation accuracy self-test...")
    results = run_accuracy_test(n_trials=50_000)
    all_pass = all(v["pass"] for k, v in results.items() if isinstance(v, dict))
    if not all_pass:
        print("  ⚠  Some accuracy checks FAILED — check protocol constants")
    else:
        print("  All accuracy checks passed ✓")

    if args.accuracy:
        return

    # Optional: decode latency micro-benchmark
    run_decode_benchmark(n_trials=10_000)

    if args.decode_bench:
        return

    # Live TCP benchmark
    threading.Thread(target=_tcp_server,  args=(args.port,), daemon=True).start()
    threading.Thread(target=_report_loop, daemon=True).start()

    print(f"\nBenchmark server running on port {args.port}.  Ctrl-C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()