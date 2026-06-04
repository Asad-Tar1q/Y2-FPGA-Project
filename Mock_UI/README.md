# EM Field Renderer — UI

This folder contains the user-facing half of the FPGA EM Field Renderer
project: the browser/tablet control app and the PS-side websocket server
that bridges it to the AXI-Lite registers on the PYNQ-Z1.

The brief lives at `../FPGA_EM_Field_Renderer_Brief (1).pdf`; the report
skeleton at `../FPGAys_Report.pdf`. This UI work covers requirements
**3.1 (separate-hardware UI), 3.2 (intuitive controls), 3.3 (overlay)**.

## Layout

```
ui/
├── protocol.md         JSON websocket protocol (v0.1)
├── server/
│   ├── mock_server.py  Mock PS-side server (stores state, fakes telemetry)
│   └── requirements.txt
└── web/                React + Vite browser app
    ├── package.json
    ├── vite.config.js
    ├── index.html
    └── src/
        ├── main.jsx
        ├── App.jsx
        ├── AntennaCanvas.jsx   Drag-to-place 200×200 canvas
        ├── Sidebar.jsx         Globals + per-antenna sliders + presets
        ├── Hud.jsx             f / λ / mode / fps overlay
        ├── useWebSocket.js     Auto-reconnecting websocket hook
        └── protocol.js         Shared protocol constants
```

## Run the mock backend

```bash
cd server
pip install -r requirements.txt
python mock_server.py
```

The server listens on `ws://0.0.0.0:8765/`. It stores antenna state in
memory and emits fake telemetry at 10 Hz, so you can develop the UI without
any PYNQ hardware in the loop.

## Run the browser app

In a second terminal:

```bash
cd web
npm install
npm run dev
```

Vite serves on `http://localhost:5173`. Because `vite.config.js` sets
`host: true`, you can also open it from a tablet on the same WiFi using the
host laptop's IP — e.g. `http://192.168.1.42:5173`. To point the app at a
server on a different host, use `?ws=ws://other.host:8765` in the URL.

## What works today

- Drag-to-place antennas on a 200×200 canvas (click empty space to add, click
  an antenna to select, drag to move, Backspace/Delete to remove).
- Per-antenna sliders: amplitude (0–2), phase (−π to π), pattern (isotropic
  / dipole / array).
- Globals: frequency (slider + derived λ), time rate, output mode (E / |E| /
  |E|² / φ), pause/play, reset.
- Presets: two-slit, phased array, dipole pair.
- HUD overlay showing f, λ, mode, antenna count, ωt, fps, CPU baseline,
  speedup ratio.
- Auto-reconnecting websocket with status indicator.
- 30 Hz outbound rate limit on state updates.

## What's still TODO

- **Swap the mock server for a real PYNQ one** that writes AXI-Lite
  registers via PYNQ MMIO (§5.3 of the brief). Same protocol — just change
  the inside of `handle_message`.
- **HUD on HDMI**: the brief calls for an overlay rendered onto the
  framebuffer by the PS (not just on the browser). That's a separate piece
  living in `server/` once the framebuffer pipeline exists.
- **Wavefront contour overlay** (Req 3.3, optional). Draw zero-crossings of
  `cos(ωt − k·r + φ)` on the HDMI render.
- **More presets** — currently three; teaching scenarios from §10
  (near-vs-far, beam steering) belong here.

## Protocol

See `protocol.md` for the full spec. In short: browser sends `state_update`
on changes, server sends `telemetry` periodically and `state_ack`/
`state_error` in response to updates. Either side may send `command` for
one-shot actions (reset, load_preset, clear_antennas).
