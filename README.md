# WaveForm — EM Field Renderer

Real-time electromagnetic wave field visualiser with a PYNQ-Z1 FPGA acceleration backend. A Python frontend streams antenna and wall configuration to the FPGA hardware driver (with a pure-NumPy software simulator included for reference), which computes the field and streams back a 640×480 scalar frame at a higher fps than a pure CPU implementation.

---

## File Structure

```
Y2-FPGA-Project/
├── README.md
├── FPGA_EM_Field_Renderer_Brief.pdf
│
├── Software/
│   ├── UI/
│   │   ├── client.py                        # Frontend — OpenGL window + ImGui control panel
│   │   ├── pynq_sim.py                      # Software simulation backend (NumPy, no hardware)
│   │   ├── waveform_protocol.py             # Shared binary control protocol (48-byte packets)
│   │   └── requirements.txt                 # Python dependencies
│   │
│   └── NumPy_Scripts/
│       ├── EMGoldenModel.py                 # Reference NumPy EM model
│       ├── EMFixed_Point_Analysis.py        # Fixed-point quantisation analysis
│       └── Antenna_math_model.py            # Antenna directivity maths
│
└── Hardware/
    └── pynq_driver.py                       # PYNQ-Z1 TCP server (AXI-Lite + AXI DMA)
```

Legacy files (`fpga_sim.py`, `laptop_sim.py`, `arm_driver.py`, `laptop_client_hardware_integrated.py`, `pynq_server_hardware_integrated.py`) are retained for reference but are not part of the active system.

---

## Features

### Visualisation
- 640×480 field rendered with the a colormap via OpenGL 3.3 core-profile shaders
- Overlaid coordinate grid (40 px spacing)
- Up to **5 simultaneous EM point sources**, each shown as a coloured dot
- Selected source highlighted with a white selection ring
- Live **display fps** and **receive fps** counters in the panel footer

### Source Control
Each antenna has four independently adjustable parameters:

| Parameter | Range | Wire encoding |
|---|---|---|
| Amplitude | 0.0 – 1.0 | 8-bit (÷255) |
| Frequency | 0.1 – 10.0× | 8-bit (÷25.5) |
| Direction (θ₀) | 0° – 360° | 7-bit, 5° steps |
| Directivity (a) | 0.0 – 1.0 | 8-bit (÷255) |

Sources can be positioned by **click-dragging** on the field, or by editing sliders in the panel.

### Antenna Presets
One-click configurations loaded from the **PRESETS** section:

| Preset | Description |
|---|---|
| Single Shot | One omnidirectional source at field centre |
| Double Shot | Two sources ±100 px from centre, same frequency |
| Beam Forming | 4-element vertical array, high directivity, all beaming right |
| Interference | Two sources with different frequencies (1.0× and 2.5×) producing beat patterns |

### Moving Sources
Each source can be set to oscillate between two waypoints (A and B) along a **triangle-wave** path:
- Enable with the **Moving** checkbox in the MOTION section
- Adjust **Period** (0.5 – 20 s), **Point A**, and **Point B** with sliders
- While moving, a coloured line and endpoint circles are drawn on the field as an overlay

### Walls
Two wall types can be drawn on the field in **Wall** draw mode:

| Type | Colour | Behaviour |
|---|---|---|
| Reflect | Red | Image-source method — real field blocked on shadow side, reflected field visible on source side |
| Absorb | Grey | Field zeroed on the far side of the wall |

- **Click-drag** to draw; the wall snaps to horizontal or vertical based on dominant drag axis
- Select a wall by clicking near its midpoint; **Delete** key removes the selected wall
- The **WALLS** list in the panel shows all walls with inline delete (×) buttons
- Up to 2 reflectors and 2 absorbers are forwarded to the FPGA hardware (hardware slot limit)

### Other Controls
- **Space** — pause / resume simulation
- **Tab** — cycle selected antenna
- **Delete** — remove selected antenna or wall (depending on draw mode)
- **Escape** — exit wall draw mode
- **F11** — toggle fullscreen

---

## How to Run

### 1. Install dependencies (laptop/PC only)

```bash
cd Software/UI
pip install -r requirements.txt
```

> On WSL2 / Linux you may also need `python3-opengl` from your package manager if PyOpenGL cannot find `libGL`.

### 2. Simulation mode (no FPGA hardware)

Open two terminals.

**Terminal 1 — start the simulator:**
```bash
cd Software/UI
python pynq_sim.py
```

**Terminal 2 — start the client:**
```bash
cd Software/UI
python client.py
```

`client.py` connects to `127.0.0.1` ports 5000 (frame stream) and 5001 (control). The simulator will print connected addresses and per-antenna/wall updates as packets arrive.

### 3. Hardware mode (PYNQ-Z1 board)

**On the PYNQ board** — copy `Hardware/pynq_driver.py` to the board and run:
```bash
python pynq_driver.py
```

The driver prints hardware register defaults on startup to confirm the overlay loaded correctly, then opens ports 5000 and 5001.

**On the laptop** — edit the `HOST` constant near the top of `Software/UI/client.py`:
```python
HOST = "192.168.2.99"   # replace with your board's IP
```

Then start the client normally:
```bash
cd Software/UI
python client.py
```

---

## Architecture

```
┌─────────────────────────────┐        TCP :5001 (control)        ┌─────────────────────────┐
│         client.py           │ ──────────────────────────────►   │   pynq_sim.py  OR       │
│  ModernGL + pyimgui + GLFW  │ ◄──────────────────────────────   │   pynq_driver.py        │
│  OpenGL 3.3 core profile    │        TCP :5000 (frames)         │   (PYNQ-Z1 hardware)    │
└─────────────────────────────┘                                    └─────────────────────────┘
```

### Control protocol (port 5001)

Two packet types are multiplexed on the same TCP stream. The receiver checks the first byte to disambiguate:

| Packet | Trigger byte | Size | Format |
|---|---|---|---|
| Antenna snapshot | byte[0] ∈ {0..4} (ant_id) | 48 bytes | `waveform_protocol.pack_packet()` — full state of all 5 slots |
| Wall add | byte[0] == 5 | 12 bytes | `>BBHHHHxx` (cmd=5, type, x1, y1, x2, y2) |
| Wall delete | byte[0] == 6 | 12 bytes | `>BBHHHHxx` (cmd=6, 0, wall_id, 0, 0, 0) |

The antenna packet is a **full-state snapshot** — every packet carries all 5 source slots, so a lost packet is fully corrected by the next one. This makes the protocol idempotent and AXI-DMA-friendly (fixed 48-byte, 6 × 64-bit AXIS beats).

### Frame protocol (port 5000)

```
[ 4-byte big-endian length ][ length × uint8 field values ]
```

Each frame is `FIELD_H × FIELD_W = 307 200` bytes, one uint8 per pixel (0 = minimum field, 255 = maximum). The client normalises across the frame and maps to the viridis colormap in the fragment shader.

---

## Dependencies

| Package | Version |
|---|---|
| numpy | ≥ 1.24 |
| matplotlib | ≥ 3.7 |
| glfw | ≥ 2.5 |
| moderngl | ≥ 5.10 |
| imgui\[glfw\] | ≥ 1.4 |
| PyOpenGL | (pulled in by imgui\[glfw\]) |

Hardware driver additionally requires the PYNQ Python library (`pynq`) available on the board's default Python environment.
