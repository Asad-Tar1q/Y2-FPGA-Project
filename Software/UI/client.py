"""
WaveForm — EM Field Renderer client (ModernGL + pyimgui).
Run:  python client.py
"""

import os
import sys

# On Linux (including WSL2) PyOpenGL defaults to GLX for context detection,
# which is correct. macOS uses CGL and Windows uses WGL — don't override those.
if sys.platform.startswith("linux"):
    os.environ.setdefault("PYOPENGL_PLATFORM", "glx")

import time
import math
import threading
import socket
import struct

import numpy as np
import glfw
import moderngl
import imgui
from imgui.integrations.glfw import GlfwRenderer
import matplotlib

# ── Constants ──────────────────────────────────────────────────────────────────
WINDOW_W    = 920       # initial window width
WINDOW_H    = 480       # initial window height
FIELD_W     = 640       # texture / simulation resolution
FIELD_H     = 480
PANEL_W     = 280       # sidebar always this wide
GRID_STEP   = 40
MAX_SOURCES = 5
MIN_SEP     = 26

# ── Network ────────────────────────────────────────────────────────────────────
HOST       = "127.0.0.1"
FRAME_PORT = 5000
CTRL_PORT  = 5001
CTRL_FMT   = ">BBHHffff"
CTRL_SIZE  = struct.calcsize(CTRL_FMT)   # 20 bytes
SEND_HZ    = 30

SOURCE_COLS_F = [
    (0.000, 0.784, 0.863),
    (0.937, 0.624, 0.153),
    (0.863, 0.392, 0.863),
    (0.392, 0.863, 0.392),
    (0.863, 0.863, 0.392),
    (0.392, 0.706, 0.863),
    (0.863, 0.549, 0.392),
    (0.627, 0.392, 0.863),
]

# ── Antenna model ──────────────────────────────────────────────────────────────
_next_id = 0

class Antenna:
    def __init__(self, x=FIELD_W/2, y=FIELD_H/2, amplitude=0.75, frequency=1.0):
        global _next_id
        self.id        = _next_id
        _next_id      += 1
        self.x         = float(x)
        self.y         = float(y)
        self.amplitude = float(amplitude)
        self.frequency = float(frequency)
        self.theta_0   = 0.0
        self.a         = 0.0

    @property
    def colour(self):
        return SOURCE_COLS_F[self.id % len(SOURCE_COLS_F)]

    @property
    def label(self):
        return f"A{self.id + 1}"


# ── Networking ─────────────────────────────────────────────────────────────────
_frame_lock   = threading.Lock()
_latest_frame = None
_recv_fps     = 0.0
_ctrl_sock    = None
_ctrl_lock    = threading.Lock()
_last_send_t  = 0.0
_paused       = False


def _recv_loop():
    global _latest_frame, _recv_fps
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(3.0)
            s.connect((HOST, FRAME_PORT))
            s.settimeout(None)
            print("[client] frame stream connected")
            buf = b""
            n, t0 = 0, time.monotonic()
            while True:
                while len(buf) < 4:
                    chunk = s.recv(4096)
                    if not chunk:
                        raise ConnectionResetError
                    buf += chunk
                length = struct.unpack(">I", buf[:4])[0]
                buf    = buf[4:]
                while len(buf) < length:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionResetError
                    buf += chunk
                raw   = buf[:length]
                buf   = buf[length:]
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(FIELD_H, FIELD_W)
                with _frame_lock:
                    _latest_frame = frame.copy()
                n  += 1
                now = time.monotonic()
                if now - t0 >= 1.0:
                    _recv_fps = n / (now - t0)
                    n, t0     = 0, now
        except Exception as e:
            print(f"[client] frame stream lost ({e.__class__.__name__}) — retrying")
            time.sleep(2)


def _connect_ctrl():
    global _ctrl_sock
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        s.connect((HOST, CTRL_PORT))
        s.settimeout(None)
        with _ctrl_lock:
            _ctrl_sock = s
        print("[client] control socket connected")
        return True
    except Exception as e:
        print(f"[client] control socket unavailable ({e.__class__.__name__}) — demo mode")
        return False


def _raw_send(ant):
    msg = struct.pack(CTRL_FMT,
                      ant.id & 0xFF, 0,
                      int(ant.x) & 0xFFFF, int(ant.y) & 0xFFFF,
                      max(0.0, min(1.0, ant.amplitude)),
                      ant.frequency,
                      ant.theta_0,
                      max(0.0, min(1.0, ant.a)))
    with _ctrl_lock:
        if _ctrl_sock:
            try:
                _ctrl_sock.sendall(msg)
            except Exception:
                pass


def send_antenna(ant, force=False):
    global _last_send_t
    now = time.monotonic()
    if not force and now - _last_send_t < 1.0 / SEND_HZ:
        return
    _last_send_t = now
    _raw_send(ant)


def send_all(antennas):
    for a in antennas:
        _raw_send(a)


def send_delete(ant_id):
    msg = struct.pack(CTRL_FMT, ant_id & 0xFF, 1, 0, 0, 0.0, 0.0, 0.0, 0.0)
    with _ctrl_lock:
        if _ctrl_sock:
            try:
                _ctrl_sock.sendall(msg)
            except Exception:
                pass


def send_pause_toggle():
    msg = struct.pack(CTRL_FMT, 0, 2, 0, 0, 0.0, 0.0, 0.0, 0.0)
    with _ctrl_lock:
        if _ctrl_sock:
            try:
                _ctrl_sock.sendall(msg)
            except Exception:
                pass


# ── GLSL shaders ───────────────────────────────────────────────────────────────
_FIELD_VERT = """
#version 330 core
in vec2 in_vert;
in vec2 in_uv;
out vec2 uv;
void main() {
    uv = in_uv;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

_FIELD_FRAG = """
#version 330 core
uniform sampler2D field;
uniform sampler2D viridis;
in vec2 uv;
out vec4 fragColour;
void main() {
    float val = texture(field, uv).r;
    fragColour = vec4(texture(viridis, vec2(val, 0.5)).rgb, 1.0);
}
"""

_COLOUR_VERT = """
#version 330 core
in vec2 in_vert;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

_COLOUR_FRAG = """
#version 330 core
uniform vec4 u_colour;
out vec4 fragColour;
void main() {
    fragColour = u_colour;
}
"""

_CIRCLE_VERT = """
#version 330 core
in vec2 in_vert;
in vec2 in_uv;
out vec2 frag_uv;
void main() {
    frag_uv = in_uv;
    gl_Position = vec4(in_vert, 0.0, 1.0);
}
"""

_CIRCLE_FRAG = """
#version 330 core
uniform vec4  u_colour;
uniform float u_inner;
in vec2 frag_uv;
out vec4 fragColour;
void main() {
    float d = length(frag_uv);
    if (d > 1.0 || d < u_inner) discard;
    fragColour = u_colour;
}
"""


# ── Application ────────────────────────────────────────────────────────────────
class WaveFormApp:
    def __init__(self):
        if not glfw.init():
            raise RuntimeError("GLFW init failed")

        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)

        self.window = glfw.create_window(
            WINDOW_W, WINDOW_H, "WaveForm — EM Field Renderer", None, None)
        if not self.window:
            glfw.terminate()
            raise RuntimeError("GLFW window creation failed")

        glfw.make_context_current(self.window)
        glfw.set_window_size_limits(self.window, WINDOW_W, 520,
                                    glfw.DONT_CARE, glfw.DONT_CARE)
        glfw.swap_interval(1)

        self.ctx = moderngl.create_context()

        imgui.create_context()
        self.impl = GlfwRenderer(self.window)

        # Track current framebuffer dimensions (updated on resize)
        self.win_w, self.win_h = glfw.get_framebuffer_size(self.window)

        self._setup_gl()
        self._setup_callbacks()

        self.antennas     = [Antenna()]
        self.sel          = 0
        self._drag_ant    = None
        self._last_drag_t = 0.0
        self._disp_fps    = 0.0
        self._disp_n      = 0
        self._disp_t      = time.monotonic()

    # ── Coordinate helpers (depend on current window size) ─────────────────────
    def _field_w(self):
        """Pixel width of the field region (window minus panel)."""
        return max(1, self.win_w - PANEL_W)

    def _field_to_ndc(self, fx, fy):
        """Map field pixel coords [0,FIELD_W]×[0,FIELD_H] → NDC."""
        fw = self._field_w()
        nx = fx / FIELD_W * (fw / self.win_w * 2.0) - 1.0
        ny = 1.0 - fy / FIELD_H * 2.0
        return nx, ny

    def _screen_to_field(self, sx, sy):
        """Map screen pixel coords → field pixel coords."""
        fw = self._field_w()
        fx = sx / fw * FIELD_W
        fy = sy / self.win_h * FIELD_H
        return fx, fy

    # ── GL setup ───────────────────────────────────────────────────────────────
    def _setup_gl(self):
        ctx = self.ctx

        self.field_tex = ctx.texture((FIELD_W, FIELD_H), components=1, dtype='f1')
        self.field_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)

        lut_data = (matplotlib.colormaps["viridis"](np.linspace(0, 1, 256))[:, :3] * 255
                    ).astype(np.uint8)
        self.lut_tex = ctx.texture((256, 1), components=3, dtype='f1',
                                   data=lut_data.tobytes())
        self.lut_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

        self.field_prog = ctx.program(
            vertex_shader=_FIELD_VERT, fragment_shader=_FIELD_FRAG)
        self.field_prog['field']   = 0
        self.field_prog['viridis'] = 1

        # Field quad — 6 vertices, pos(x,y) + uv(u,v), updated on resize
        self.field_vbo = ctx.buffer(reserve=6 * 4 * 4)
        self.field_vao = ctx.vertex_array(
            self.field_prog, [(self.field_vbo, '2f 2f', 'in_vert', 'in_uv')])

        self.col_prog = ctx.program(
            vertex_shader=_COLOUR_VERT, fragment_shader=_COLOUR_FRAG)

        # Grid — fixed line count, contents updated on resize
        n_vert_lines = FIELD_W // GRID_STEP + 1
        n_horiz_lines = FIELD_H // GRID_STEP + 1
        self._grid_count = (n_vert_lines + n_horiz_lines) * 2
        self.grid_vbo = ctx.buffer(reserve=self._grid_count * 2 * 4)
        self.grid_vao = ctx.vertex_array(
            self.col_prog, [(self.grid_vbo, '2f', 'in_vert')])

        self.circle_prog = ctx.program(
            vertex_shader=_CIRCLE_VERT, fragment_shader=_CIRCLE_FRAG)

        # 6 verts × (pos xy + uv xy) × 4 bytes
        self.dot_vbo = ctx.buffer(reserve=6 * 4 * 4)
        self.dot_vao = ctx.vertex_array(
            self.circle_prog, [(self.dot_vbo, '2f 2f', 'in_vert', 'in_uv')])

        # Write initial geometry
        self._rebuild_geometry()

    def _rebuild_geometry(self):
        """Recompute field quad and grid NDC coords for the current window size."""
        fw = self._field_w()
        rx = fw / self.win_w * 2.0 - 1.0   # NDC x at right edge of field

        # Field quad: pos + UV, UV flips Y so numpy row-0 maps to screen top
        quad_data = np.array([
            -1.0, -1.0,  0.0, 1.0,
             rx,  -1.0,  1.0, 1.0,
            -1.0,  1.0,  0.0, 0.0,
             rx,  -1.0,  1.0, 1.0,
             rx,   1.0,  1.0, 0.0,
            -1.0,  1.0,  0.0, 0.0,
        ], dtype='f4')
        self.field_vbo.write(quad_data.tobytes())

        # Grid lines
        lines = []
        for col in range(0, FIELD_W + 1, GRID_STEP):
            nx = col / FIELD_W * (rx + 1.0) - 1.0
            lines += [nx, -1.0, nx, 1.0]
        for row in range(0, FIELD_H + 1, GRID_STEP):
            ny = 1.0 - row / FIELD_H * 2.0
            lines += [-1.0, ny, rx, ny]
        self.grid_vbo.write(np.array(lines, dtype='f4').tobytes())
        self._grid_count = len(lines) // 2

        # Update GL viewport
        self.ctx.viewport = (0, 0, self.win_w, self.win_h)

    # ── GLFW callbacks ─────────────────────────────────────────────────────────
    def _setup_callbacks(self):
        glfw.set_mouse_button_callback(self.window,      self._mouse_button_cb)
        glfw.set_cursor_pos_callback(self.window,        self._cursor_pos_cb)
        glfw.set_key_callback(self.window,               self._key_cb)
        glfw.set_framebuffer_size_callback(self.window,  self._on_resize)

    def _on_resize(self, win, w, h):
        if w <= 0 or h <= 0:
            return
        self.win_w, self.win_h = w, h
        self._rebuild_geometry()

    def _mouse_button_cb(self, win, button, action, mods):
        if imgui.get_io().want_capture_mouse:
            return
        sx, sy = glfw.get_cursor_pos(win)
        fw = self._field_w()
        if button == glfw.MOUSE_BUTTON_LEFT and sx < fw:
            fx, fy = self._screen_to_field(sx, sy)
            if action == glfw.PRESS:
                hit = self._ant_at(fx, fy)
                if hit is not None:
                    self.sel       = hit
                    self._drag_ant = hit
                elif self.antennas:
                    ant = self.antennas[self.sel]
                    nx = float(max(0, min(FIELD_W - 1, fx)))
                    ny = float(max(0, min(FIELD_H - 1, fy)))
                    if not self._overlaps_any(nx, ny, self.sel):
                        ant.x, ant.y = nx, ny
                        send_antenna(ant, force=True)
            elif action == glfw.RELEASE:
                if self._drag_ant is not None:
                    send_antenna(self.antennas[self._drag_ant], force=True)
                self._drag_ant = None

    def _cursor_pos_cb(self, win, sx, sy):
        if self._drag_ant is not None and sx < self._field_w():
            a  = self.antennas[self._drag_ant]
            fx, fy = self._screen_to_field(sx, sy)
            nx = float(max(0, min(FIELD_W - 1, fx)))
            ny = float(max(0, min(FIELD_H - 1, fy)))
            if not self._overlaps_any(nx, ny, self._drag_ant):
                a.x, a.y = nx, ny
            now = time.monotonic()
            if now - self._last_drag_t >= 1.0 / SEND_HZ:
                send_antenna(a)
                self._last_drag_t = now

    def _key_cb(self, win, key, scancode, action, mods):
        global _paused
        if imgui.get_io().want_capture_keyboard:
            return
        if action == glfw.PRESS:
            if key == glfw.KEY_F11:
                self._toggle_fullscreen()
            elif key == glfw.KEY_SPACE:
                _paused = not _paused
                send_pause_toggle()
            elif key == glfw.KEY_TAB and self.antennas:
                self.sel = (self.sel + 1) % len(self.antennas)
            elif key == glfw.KEY_DELETE:
                self._del_ant()

    def _toggle_fullscreen(self):
        if glfw.get_window_monitor(self.window):
            glfw.set_window_monitor(self.window, None,
                                    100, 100, WINDOW_W, WINDOW_H, 0)
        else:
            monitor = glfw.get_primary_monitor()
            mode    = glfw.get_video_mode(monitor)
            glfw.set_window_monitor(self.window, monitor, 0, 0,
                                    mode.size.width, mode.size.height,
                                    mode.refresh_rate)

    # ── Antenna helpers ────────────────────────────────────────────────────────
    def _ant_at(self, fx, fy):
        """Hit-test against field pixel coords."""
        for i, a in enumerate(self.antennas):
            if (fx - a.x) ** 2 + (fy - a.y) ** 2 <= 144:
                return i
        return None

    def _overlaps_any(self, x, y, exclude=-1):
        for i, a in enumerate(self.antennas):
            if i == exclude:
                continue
            if (x - a.x) ** 2 + (y - a.y) ** 2 < MIN_SEP ** 2:
                return True
        return False

    def _find_free_pos(self):
        cx, cy = FIELD_W / 2.0, FIELD_H / 2.0
        if not self._overlaps_any(cx, cy):
            return cx, cy
        for radius in range(MIN_SEP, 400, MIN_SEP):
            for deg in range(0, 360, 20):
                x = cx + radius * math.cos(math.radians(deg))
                y = cy + radius * math.sin(math.radians(deg))
                x = max(14.0, min(FIELD_W - 14.0, x))
                y = max(14.0, min(FIELD_H - 14.0, y))
                if not self._overlaps_any(x, y):
                    return x, y
        return cx, cy

    def _add_ant(self):
        if len(self.antennas) >= MAX_SOURCES:
            return
        x, y = self._find_free_pos()
        a = Antenna(x, y)
        self.antennas.append(a)
        self.sel = len(self.antennas) - 1
        send_antenna(a, force=True)

    def _del_ant(self):
        if not self.antennas:
            return
        ant = self.antennas[self.sel]
        send_delete(ant.id)
        del self.antennas[self.sel]
        if self.antennas:
            self.sel = min(self.sel, len(self.antennas) - 1)
        else:
            self.sel = 0

    def _apply_preset(self, name: str):
        for ant in self.antennas:
            send_delete(ant.id)
        self.antennas.clear()

        cx, cy = FIELD_W / 2.0, FIELD_H / 2.0
        if name == "single":
            defs = [
                dict(x=cx, y=cy, amplitude=0.75, frequency=1.0, theta_0=0.0, a=0.0),
            ]
        elif name == "double":
            defs = [
                dict(x=cx - 100, y=cy, amplitude=0.75, frequency=1.0, theta_0=0.0, a=0.0),
                dict(x=cx + 100, y=cy, amplitude=0.75, frequency=1.0, theta_0=0.0, a=0.0),
            ]
        elif name == "beamform":
            # 4-element vertical array centred on FIELD_H/2, all beaming right (theta_0=0)
            defs = [
                dict(x=cx, y=cy - 90, amplitude=0.75, frequency=1.0, theta_0=0.0, a=0.85),
                dict(x=cx, y=cy - 30, amplitude=0.75, frequency=1.0, theta_0=0.0, a=0.85),
                dict(x=cx, y=cy + 30, amplitude=0.75, frequency=1.0, theta_0=0.0, a=0.85),
                dict(x=cx, y=cy + 90, amplitude=0.75, frequency=1.0, theta_0=0.0, a=0.85),
            ]
        elif name == "interference":
            # Two sources at different frequencies — beat / channel interference
            defs = [
                dict(x=cx - 80, y=cy, amplitude=0.75, frequency=1.0, theta_0=0.0, a=0.0),
                dict(x=cx + 80, y=cy, amplitude=0.75, frequency=2.5, theta_0=0.0, a=0.0),
            ]
        else:
            defs = []

        for d in defs:
            ant = Antenna(d['x'], d['y'], d['amplitude'], d['frequency'])
            ant.theta_0 = d['theta_0']
            ant.a = d['a']
            self.antennas.append(ant)

        self.sel = 0
        send_all(self.antennas)

    # ── GL rendering ──────────────────────────────────────────────────────────
    def _draw_field(self, frame):
        if frame is not None:
            self.field_tex.write(frame.tobytes())
        self.field_tex.use(location=0)
        self.lut_tex.use(location=1)
        self.field_vao.render()

    def _draw_grid(self):
        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA
        self.col_prog['u_colour'].value = (0.102, 0.208, 0.314, 0.6)
        self.grid_vao.render(moderngl.LINES, vertices=self._grid_count)
        self.ctx.disable(moderngl.BLEND)

    def _draw_dots(self):
        if not self.antennas:
            return
        r_px  = 10.0
        sr_px = 14.0
        r_nx  = r_px  / self.win_w * 2.0
        r_ny  = r_px  / self.win_h * 2.0
        sr_nx = sr_px / self.win_w * 2.0
        sr_ny = sr_px / self.win_h * 2.0

        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA

        for i, ant in enumerate(self.antennas):
            cx, cy = self._field_to_ndc(ant.x, ant.y)
            if i == self.sel:
                # White ring: filled outer circle, hollow at 0.65 of radius
                self.dot_vbo.write(_circle_verts(cx, cy, sr_nx, sr_ny))
                self.circle_prog['u_colour'].value = (1.0, 1.0, 1.0, 1.0)
                self.circle_prog['u_inner'].value  = 0.65
                self.dot_vao.render(moderngl.TRIANGLES, vertices=6)
            col = ant.colour
            self.dot_vbo.write(_circle_verts(cx, cy, r_nx, r_ny))
            self.circle_prog['u_colour'].value = (col[0], col[1], col[2], 1.0)
            self.circle_prog['u_inner'].value  = 0.0
            self.dot_vao.render(moderngl.TRIANGLES, vertices=6)

        self.ctx.disable(moderngl.BLEND)

    # ── ImGui panel ────────────────────────────────────────────────────────────
    def _draw_panel(self):
        global _paused
        panel_x = self.win_w - PANEL_W

        imgui.push_style_color(imgui.COLOR_WINDOW_BACKGROUND,  0.059, 0.125, 0.208, 1.0)
        imgui.push_style_color(imgui.COLOR_TEXT,               0.847, 0.906, 0.969, 1.0)
        imgui.push_style_color(imgui.COLOR_FRAME_BACKGROUND,   0.043, 0.098, 0.161, 1.0)
        imgui.push_style_color(imgui.COLOR_SLIDER_GRAB,        0.000, 0.784, 0.863, 1.0)
        imgui.push_style_color(imgui.COLOR_SLIDER_GRAB_ACTIVE, 0.667, 1.000, 0.847, 1.0)
        imgui.push_style_color(imgui.COLOR_HEADER,             0.075, 0.157, 0.251, 1.0)
        imgui.push_style_color(imgui.COLOR_HEADER_ACTIVE,      0.000, 0.784, 0.863, 0.4)
        imgui.push_style_color(imgui.COLOR_BUTTON,             0.075, 0.157, 0.251, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED,     0.000, 0.784, 0.863, 0.3)
        imgui.push_style_color(imgui.COLOR_BUTTON_ACTIVE,      0.000, 0.784, 0.863, 0.6)
        imgui.push_style_var(imgui.STYLE_WINDOW_ROUNDING, 0.0)
        imgui.push_style_var(imgui.STYLE_FRAME_ROUNDING,  4.0)
        imgui.push_style_var(imgui.STYLE_ITEM_SPACING,    (8.0, 6.0))

        flags = (imgui.WINDOW_NO_TITLE_BAR |
                 imgui.WINDOW_NO_RESIZE     |
                 imgui.WINDOW_NO_MOVE       |
                 imgui.WINDOW_NO_SAVED_SETTINGS)

        imgui.set_next_window_position(panel_x, 0)
        imgui.set_next_window_size(PANEL_W, self.win_h)
        imgui.begin("##panel", flags=flags)

        # Header
        imgui.push_style_color(imgui.COLOR_TEXT, 0.000, 0.784, 0.863, 1.0)
        imgui.text("WaveForm")
        imgui.pop_style_color()
        imgui.same_line()
        imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
        imgui.text("EM SOURCE CONTROL")
        imgui.pop_style_color()

        imgui.separator()

        # Pause / Resume
        p_col = (0.863, 0.275, 0.275, 1.0) if not _paused else (0.275, 0.784, 0.275, 1.0)
        imgui.push_style_color(imgui.COLOR_TEXT,           *p_col)
        imgui.push_style_color(imgui.COLOR_BUTTON,         0.043, 0.098, 0.161, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, p_col[0], p_col[1], p_col[2], 0.3)
        pause_label = "|| Pause" if not _paused else ">  Resume"
        if imgui.button(pause_label, width=PANEL_W - 16):
            _paused = not _paused
            send_pause_toggle()
        imgui.pop_style_color(3)

        imgui.separator()

        # Presets
        imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
        imgui.text("PRESETS")
        imgui.pop_style_color()

        imgui.push_style_color(imgui.COLOR_TEXT,           0.000, 0.784, 0.863, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON,         0.043, 0.098, 0.161, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.000, 0.784, 0.863, 0.2)
        btn_w = PANEL_W - 16
        if imgui.button("Single Shot##p", width=btn_w):
            self._apply_preset("single")
        if imgui.button("Double Shot##p", width=btn_w):
            self._apply_preset("double")
        if imgui.button("Beam Forming##p", width=btn_w):
            self._apply_preset("beamform")
        if imgui.button("Interference##p", width=btn_w):
            self._apply_preset("interference")
        imgui.pop_style_color(3)

        imgui.separator()

        # Source list
        dl = imgui.get_window_draw_list()
        for i, ant in enumerate(self.antennas):
            col = ant.colour
            cx, cy = imgui.get_cursor_screen_pos()
            dl.add_circle_filled(cx + 6, cy + 8, 5,
                                 imgui.get_color_u32_rgba(col[0], col[1], col[2], 1.0))
            imgui.set_cursor_pos_x(imgui.get_cursor_pos()[0] + 16)
            label = (f"A{i+1}  A={ant.amplitude:.2f}  a={ant.a:.2f}"
                     f"  th={ant.theta_0:.2f}##row{i}")
            clicked, _ = imgui.selectable(label, i == self.sel, width=PANEL_W - 28)
            if clicked:
                self.sel = i

        # + Add / × Del
        imgui.push_style_color(imgui.COLOR_TEXT,           0.000, 0.784, 0.863, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON,         0.043, 0.098, 0.161, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.000, 0.784, 0.863, 0.2)
        if imgui.button("+ Add", width=100):
            self._add_ant()
        imgui.pop_style_color(3)
        imgui.same_line()
        imgui.push_style_color(imgui.COLOR_TEXT,           0.863, 0.275, 0.275, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON,         0.043, 0.098, 0.161, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, 0.863, 0.275, 0.275, 0.2)
        if imgui.button("x Del", width=100):
            self._del_ant()
        imgui.pop_style_color(3)

        imgui.separator()

        # Selected antenna controls
        if self.antennas:
            ant = self.antennas[self.sel]
            col = ant.colour
            dl  = imgui.get_window_draw_list()
            cx, cy = imgui.get_cursor_screen_pos()
            dl.add_circle_filled(cx + 6, cy + 8, 6,
                                 imgui.get_color_u32_rgba(col[0], col[1], col[2], 1.0))
            imgui.set_cursor_pos_x(imgui.get_cursor_pos()[0] + 16)
            imgui.text(f"A{self.sel + 1}")

            imgui.push_item_width(PANEL_W - 32)

            imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
            imgui.text("Amplitude")
            imgui.pop_style_color()
            changed, v = imgui.slider_float("##amp", ant.amplitude, 0.0, 1.0)
            if changed:
                ant.amplitude = v
                send_antenna(ant)

            imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
            imgui.text("Frequency")
            imgui.pop_style_color()
            changed, v = imgui.slider_float("##freq", ant.frequency, 0.1, 10.0,
                                            format="x%.2f")
            if changed:
                ant.frequency = v
                send_antenna(ant)

            imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
            imgui.text("Direction")
            imgui.pop_style_color()
            changed, v = imgui.slider_float("##theta", ant.theta_0,
                                            0.0, 6.2832, format="%.2f rad")
            if changed:
                ant.theta_0 = v
                send_antenna(ant)

            imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
            imgui.text("Directivity")
            imgui.pop_style_color()
            changed, v = imgui.slider_float("##a", ant.a, 0.0, 1.0)
            if changed:
                ant.a = v
                send_antenna(ant)

            imgui.pop_item_width()

            imgui.text(f"x {int(ant.x):>3}  y {int(ant.y):>3}")

            # Minimap
            mm_w, mm_h = 56, 56
            mx, my = imgui.get_cursor_screen_pos()
            dl = imgui.get_window_draw_list()
            dl.add_rect_filled(mx, my, mx + mm_w, my + mm_h,
                               imgui.get_color_u32_rgba(0.043, 0.098, 0.161, 1.0))
            dl.add_rect(mx, my, mx + mm_w, my + mm_h,
                        imgui.get_color_u32_rgba(0.659, 1.000, 0.847, 0.3))
            dl.add_circle_filled(
                mx + ant.x / FIELD_W * mm_w,
                my + ant.y / FIELD_H * mm_h,
                3, imgui.get_color_u32_rgba(col[0], col[1], col[2], 1.0))
            imgui.dummy(mm_w, mm_h)
            imgui.same_line()
            imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
            px, py = imgui.get_cursor_pos()
            imgui.set_cursor_pos((px, py + 20))
            imgui.text("drag on field")
            imgui.pop_style_color()

        # Footer — keybind hints + FPS, pinned to bottom
        imgui.set_cursor_pos((8, self.win_h - 68))
        imgui.push_style_color(imgui.COLOR_SEPARATOR, 0.102, 0.208, 0.314, 1.0)
        imgui.separator()
        imgui.pop_style_color()

        imgui.set_cursor_pos((8, self.win_h - 54))
        imgui.push_style_color(imgui.COLOR_TEXT, 0.000, 0.784, 0.863, 1.0)
        imgui.text("Space")
        imgui.pop_style_color()
        imgui.same_line(spacing=6)
        imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
        imgui.text("pause / resume")
        imgui.pop_style_color()

        imgui.set_cursor_pos((8, self.win_h - 38))
        imgui.push_style_color(imgui.COLOR_TEXT, 0.000, 0.784, 0.863, 1.0)
        imgui.text("Tab")
        imgui.pop_style_color()
        imgui.same_line(spacing=6)
        imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
        imgui.text("cycle")
        imgui.pop_style_color()
        imgui.same_line(spacing=14)
        imgui.push_style_color(imgui.COLOR_TEXT, 0.000, 0.784, 0.863, 1.0)
        imgui.text("Del")
        imgui.pop_style_color()
        imgui.same_line(spacing=6)
        imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
        imgui.text("remove")
        imgui.pop_style_color()

        imgui.set_cursor_pos((8, self.win_h - 18))
        imgui.push_style_color(imgui.COLOR_TEXT, 0.302, 0.502, 0.702, 1.0)
        imgui.text(f"disp {self._disp_fps:4.0f} fps")
        imgui.same_line(spacing=10)
        imgui.text(f"recv {_recv_fps:4.0f} fps")
        imgui.pop_style_color()

        imgui.end()
        imgui.pop_style_var(3)
        imgui.pop_style_color(10)

    # ── Main loop ──────────────────────────────────────────────────────────────
    def run(self):
        threading.Thread(target=_recv_loop, daemon=True).start()
        _connect_ctrl()
        send_all(self.antennas)

        while not glfw.window_should_close(self.window):
            glfw.poll_events()
            self.impl.process_inputs()

            self._disp_n += 1
            now = time.monotonic()
            if now - self._disp_t >= 1.0:
                self._disp_fps = self._disp_n / (now - self._disp_t)
                self._disp_n, self._disp_t = 0, now

            with _frame_lock:
                frame = _latest_frame
                if frame is not None:
                    frame = frame.copy()

            self.ctx.screen.use()
            self.ctx.clear(0.043, 0.098, 0.161)

            self._draw_field(frame)
            self._draw_grid()
            self._draw_dots()

            imgui.new_frame()
            self._draw_panel()
            imgui.render()
            self.impl.render(imgui.get_draw_data())

            glfw.swap_buffers(self.window)

        self.impl.shutdown()
        glfw.terminate()


def _quad_verts(cx, cy, rx, ry):
    l, r = cx - rx, cx + rx
    b, t = cy - ry, cy + ry
    return np.array([
        l, b,  r, b,  l, t,
        r, b,  r, t,  l, t,
    ], dtype='f4').tobytes()


def _circle_verts(cx, cy, rx, ry):
    """Quad with UV in [-1,1]×[-1,1]; fragment shader clips to unit circle."""
    l, r = cx - rx, cx + rx
    b, t = cy - ry, cy + ry
    return np.array([
        l, b, -1.0, -1.0,
        r, b,  1.0, -1.0,
        l, t, -1.0,  1.0,
        r, b,  1.0, -1.0,
        r, t,  1.0,  1.0,
        l, t, -1.0,  1.0,
    ], dtype='f4').tobytes()


if __name__ == "__main__":
    WaveFormApp().run()
