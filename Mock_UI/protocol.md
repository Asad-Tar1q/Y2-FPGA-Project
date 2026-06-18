# UI ↔ PS WebSocket Protocol

**Version:** 0.1 (draft)
**Transport:** WebSocket over WiFi (default port `8765`).
**Encoding:** JSON, UTF-8, one message per WebSocket frame.

The browser app is the client; the Python server on the PYNQ PS is the host.
Either side may send a message at any time after the handshake.

---

## 1. Field grid and coordinate system

The render grid is **200 × 200 pixels**. Antenna positions and source
coordinates are expressed in **grid units** (floats), with `(0, 0)` at the
top-left and `(199, 199)` at the bottom-right. The browser must clamp
positions into `[0, 199]` before sending; the server will reject out-of-range
values.

Phases are in **radians** in `[-π, π]`. Frequency is in **Hz**. Amplitude is
**unitless** (the renderer tone-maps before display).

Antenna IDs are integers `0 … N-1` and are assigned by the browser. Order in
the `antennas` array is significant — it matches `ANT_RAM[i]` on the FPGA.

Pattern IDs:

| id | name      |
|----|-----------|
| 0  | isotropic |
| 1  | dipole    |
| 2  | array     |

Output modes (`out_mode`):

| id | name | meaning              |
|----|------|----------------------|
| 0  | E    | signed real field    |
| 1  | absE | magnitude            |
| 2  | E2   | squared magnitude    |
| 3  | phi  | phase                |

---

## 2. Channels

The WebSocket carries two kinds of frames:

- **Text frames** are JSON control messages (§3). Both directions.
- **Binary frames** are encoded field images (§4). Server → browser only.

This split avoids base64 overhead and lets the browser distinguish them
trivially: `typeof ev.data === "string"` ⇒ control, otherwise ⇒ frame.

## 3. Control messages

Every text message has a `type` field. Unknown types are ignored with a
warning.

```json
{ "type": "<message-type>", ... }
```

---

### 3.1 `hello` (server → browser, once on connect)

Sent immediately after the WebSocket upgrades. Tells the browser what the
server is and what it currently believes about the world.

```json
{
  "type": "hello",
  "protocol_version": "0.1",
  "server": "mock" | "pynq",
  "grid": { "w": 200, "h": 200 },
  "max_antennas": 32,
  "state": { ... full state_update payload ... }
}
```

### 3.2 `state_update` (browser → server)

Sent whenever the antenna list or any global parameter changes. **The browser
sends the full state, not a diff.** The server applies parameters at the next
frame boundary.

The browser should debounce these to **at most 30 Hz** while a slider is being
dragged, and send a final message on release.

```json
{
  "type": "state_update",
  "seq": 42,
  "globals": {
    "frequency_hz": 2.4e9,
    "time_rate": 1.0,
    "paused": false,
    "out_mode": 0
  },
  "antennas": [
    {
      "id": 0,
      "x": 100.0,
      "y": 100.0,
      "amplitude": 1.0,
      "phase": 0.0,
      "pattern": 0
    }
  ]
}
```

`seq` is a monotonically increasing integer from the browser. The server
echoes the most recently *applied* seq back in telemetry so the browser can
tell when its updates have landed.

### 3.3 `state_ack` (server → browser)

Sent after a `state_update` has been validated and queued for the next frame.
Carries no payload beyond the acknowledged seq. The browser uses this to
clear a "pending" indicator.

```json
{ "type": "state_ack", "seq": 42 }
```

If validation fails, the server sends `state_error` instead:

```json
{ "type": "state_error", "seq": 42, "reason": "antenna 3: x out of range" }
```

### 3.4 `telemetry` (server → browser, periodic)

Pushed at ~10 Hz. Drives the HUD: fps, CPU baseline ratio, current `ωt`,
frame counter, last applied seq.

```json
{
  "type": "telemetry",
  "frame": 12345,
  "fps": 58.2,
  "cpu_baseline_fps": 4.1,
  "ratio": 14.2,
  "omega_t": 1.2734,
  "applied_seq": 42
}
```

### 3.5 `command` (browser → server)

One-shot actions that don't fit the state model. Currently:

```json
{ "type": "command", "action": "reset" | "clear_antennas" | "load_preset", "preset": "two_slit" }
```

Presets currently defined: `two_slit`, `phased_array`, `dipole_pair`.

---

## 4. Field frames (binary, server → browser)

Each binary WebSocket frame is **the raw bytes of a JPEG image** — no
header, no envelope, no framing — sized at the native render resolution
(200 × 200). The browser is expected to decode each frame with
`createImageBitmap` (or an `<img>` blob URL fallback) and draw it as the
background of the rendered field display.

Why no header? The most recent applied `seq` and `omega_t` are already
broadcast in `telemetry` at ~10 Hz, which is plenty for HUD purposes. Per-
frame metadata would just bloat the hot path. If we later need per-frame
metadata (e.g. variable-size frames, or interleaved modes), we'll prefix a
fixed-size header.

The server emits frames at up to **30 Hz** by default. If the browser tab is
backgrounded (and signals it via a `command` of `pause_stream`), the server
should drop to a much lower rate.

---

## 5. Connection lifecycle

1. Browser connects to `ws://<pynq-ip>:8765/`.
2. Server sends `hello`.
3. Browser sends an initial `state_update` (the user's current canvas).
4. Either side sends messages as needed.
5. If the connection drops, the browser auto-reconnects with exponential
   backoff (250 ms, 500 ms, 1 s, 2 s, max 5 s) and re-sends its full state.

---

## 6. Notes on rate-limiting

The brief calls for capping UI rate to 30 Hz. The browser is responsible for
this — the server will accept faster messages but will only apply the most
recent state per frame, dropping intermediate updates silently. This keeps
slider-drag UX feeling smooth without flooding AXI-Lite.
