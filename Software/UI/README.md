# Software/UI

This folder contains the Python-based UI and supporting simulation/server code for the WaveForm EM field renderer.

## What is included

- `client.py` — main Pygame UI client.
- `fpga_sim.py` — local simulator that generates EM-like frame data and responds to antenna controls.
- `clientUI.py` — PYNQ-style server stub for a production FPGA/PL backend (JSON control API).
- `laptop_sim.py` — alternate local simulation harness for laptop testing.
- `pynq_sim.py` — reference PYNQ implementation used as a model for the FPGA-based path.
- `requirements.txt` — required Python packages for the UI.

## Design choices so far

### UI rendering

- The main UI is designed at a fixed logical resolution of `920×480`.
- To keep the UI sharp in fullscreen or larger windows, `client.py` now uses `pygame.SCALED` when creating the display surface.
- `pygame.SCALED` allows Pygame to upscale the fixed logical surface via the display system, which is generally much crisper than manually scaling a smaller surface in software.

### Text rendering

- The UI now uses `pygame.freetype` for font rendering.
- This renders text directly at the requested size and avoids the soft, blurry look that occurs when small rasterized text is scaled up.
- A lightweight wrapper is used so the rest of the code can still call `font.render(...)` and `font.size(...)`.

### Frame colour mapping

- Both `client.py` and `fpga_sim.py` now share the same custom colour map.
- The current scheme uses an `RdBu`-style ramp mapping the field values through blue-to-red colours.
- This keeps the client and simulator visually consistent.

### Network and control architecture

- `client.py` connects to `fpga_sim.py` over two sockets:
  - Frame stream on port `5000`
  - Control commands on port `5001`
- `fpga_sim.py` sends raw RGB frames with a 4-byte length header, while the client reads and displays them at each frame.
- Antenna parameters are sent from the client to the simulator on the control connection.

## How to run

### Create and activate a Python environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r Software/UI/requirements.txt
```

### Run the simulator

The simulator must be running before the client starts.

```bash
python Software/UI/fpga_sim.py
```

### Run the UI client

In a separate terminal:

```bash
python Software/UI/client.py
```

### Optional: use `clientUI.py` for PYNQ-style server behavior

`clientUI.py` is a stub server that mimics the control and frame server interface expected by a production FPGA backend. It is not the main UI client.

## Notes

- `client.py` is the current active UI implementation.
- `fpga_sim.py` is the matching simulation backend for development and testing.
- If you want to run on a real PYNQ board later, `clientUI.py` and `pynq_sim.py` are the starting points for production integration.
