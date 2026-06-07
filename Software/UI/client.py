"""
WaveForm — EM Field Renderer client (ModernGL + pyimgui).
Run:  python client.py
"""

# Must be set before any OpenGL import — tells PyOpenGL to use GLX for
# context detection (GLFW on WSL2/Mesa uses GLX)
import os
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

# ── Dimensions ─────────────────────────────────────────────────────────────────
WINDOW_W    = 920
WINDOW_H    = 480
FIELD_W     = 640
FIELD_H     = 480
PANEL_X     = 640
PANEL_W     = 280
GRID_STEP   = 40
MAX_SOURCES = 8
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
        self.theta_0   = 0.0   # antenna direction, radians [0, 2π]
        self.a         = 0.0   # directionality: 0=isotropic, 1=log-periodic

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


# ── Coordinate helpers ──────────────────────────────────────────────────────────
def px_to_ndc_x(px):
    return px / WINDOW_W * 2.0 - 1.0

def px_to_ndc_y(py):
    return 1.0 - py / WINDOW_H * 2.0

def field_px_to_ndc(fx, fy):
    return px_to_ndc_x(fx), px_to_ndc_y(fy)

def _quad_verts(cx, cy, rx, ry):
    l, r = cx - rx, cx + rx
    b, t = cy - ry, cy + ry
    return np.array([
        l, b,  r, b,  l, t,
        r, b,  r, t,  l, t,
    ], dtype='f4').tobytes()


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
        glfw.swap_interval(1)

        self.ctx = moderngl.create_context()

        imgui.create_context()
        self.impl = GlfwRenderer(self.window)

        self._setup_gl()
        self._setup_callbacks()

        self.antennas     = [Antenna()]
        self.sel          = 0
        self._drag_ant    = None
        self._last_drag_t = 0.0
        self._disp_fps    = 0.0
        self._disp_n      = 0
        self._disp_t      = time.monotonic()

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

        rx = px_to_ndc_x(FIELD_W)
        # Interleaved pos(x,y) + uv(u,v). UV flips Y so numpy row-0 (image top)
        # maps to screen top.
        quad_data = np.array([
            # pos_x  pos_y   uv_u  uv_v
            -1.0,   -1.0,   0.0,  1.0,
             rx,    -1.0,   1.0,  1.0,
            -1.0,    1.0,   0.0,  0.0,
             rx,    -1.0,   1.0,  1.0,
             rx,     1.0,   1.0,  0.0,
            -1.0,    1.0,   0.0,  0.0,
        ], dtype='f4')
        self.field_vbo = ctx.buffer(quad_data.tobytes())
        self.field_vao = ctx.vertex_array(
            self.field_prog, [(self.field_vbo, '2f 2f', 'in_vert', 'in_uv')])

        self.col_prog = ctx.program(
            vertex_shader=_COLOUR_VERT, fragment_shader=_COLOUR_FRAG)

        self._build_grid()

        self.dot_vbo = ctx.buffer(reserve=48)
        self.dot_vao = ctx.vertex_array(
            self.col_prog, [(self.dot_vbo, '2f', 'in_vert')])

    def _build_grid(self):
        lines = []
        rx = px_to_ndc_x(FIELD_W)
        for col in range(0, FIELD_W + 1, GRID_STEP):
            nx = px_to_ndc_x(col)
            lines += [nx, -1.0, nx, 1.0]
        for row in range(0, FIELD_H + 1, GRID_STEP):
            ny = px_to_ndc_y(row)
            lines += [-1.0, ny, rx, ny]
        self.grid_vbo = self.ctx.buffer(np.array(lines, dtype='f4').tobytes())
        self.grid_vao = self.ctx.vertex_array(
            self.col_prog, [(self.grid_vbo, '2f', 'in_vert')])
        self._grid_count = len(lines) // 2

    # ── GLFW callbacks ─────────────────────────────────────────────────────────
    def _setup_callbacks(self):
        glfw.set_mouse_button_callback(self.window, self._mouse_button_cb)
        glfw.set_cursor_pos_callback(self.window,   self._cursor_pos_cb)
        glfw.set_key_callback(self.window,          self._key_cb)

    def _mouse_button_cb(self, win, button, action, mods):
        if imgui.get_io().want_capture_mouse:
            return
        x, y = glfw.get_cursor_pos(win)
        if button == glfw.MOUSE_BUTTON_LEFT and x < FIELD_W:
            if action == glfw.PRESS:
                hit = self._ant_at(x, y)
                if hit is not None:
                    self.sel       = hit
                    self._drag_ant = hit
                elif self.antennas:
                    ant = self.antennas[self.sel]
                    nx = float(max(0, min(FIELD_W - 1, x)))
                    ny = float(max(0, min(FIELD_H - 1, y)))
                    if not self._overlaps_any(nx, ny, self.sel):
                        ant.x, ant.y = nx, ny
                        send_antenna(ant, force=True)
            elif action == glfw.RELEASE:
                if self._drag_ant is not None:
                    send_antenna(self.antennas[self._drag_ant], force=True)
                self._drag_ant = None

    def _cursor_pos_cb(self, win, x, y):
        if self._drag_ant is not None and x < FIELD_W:
            a  = self.antennas[self._drag_ant]
            nx = float(max(0, min(FIELD_W - 1, x)))
            ny = float(max(0, min(FIELD_H - 1, y)))
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
            if key == glfw.KEY_SPACE:
                _paused = not _paused
                send_pause_toggle()
            elif key == glfw.KEY_TAB and self.antennas:
                self.sel = (self.sel + 1) % len(self.antennas)
            elif key == glfw.KEY_DELETE:
                self._del_ant()

    # ── Antenna helpers ────────────────────────────────────────────────────────
    def _ant_at(self, mx, my):
        for i, a in enumerate(self.antennas):
            if (mx - a.x) ** 2 + (my - a.y) ** 2 <= 144:
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
        r   = 10.0 / WINDOW_W * 2.0
        ry  = 10.0 / WINDOW_H * 2.0
        sr  = 14.0 / WINDOW_W * 2.0
        sry = 14.0 / WINDOW_H * 2.0

        self.ctx.enable(moderngl.BLEND)
        self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA

        for i, ant in enumerate(self.antennas):
            cx, cy = field_px_to_ndc(ant.x, ant.y)
            if i == self.sel:
                self.dot_vbo.write(_quad_verts(cx, cy, sr, sry))
                self.col_prog['u_colour'].value = (1.0, 1.0, 1.0, 1.0)
                self.dot_vao.render(moderngl.TRIANGLES, vertices=6)
            col = ant.colour
            self.dot_vbo.write(_quad_verts(cx, cy, r, ry))
            self.col_prog['u_colour'].value = (col[0], col[1], col[2], 1.0)
            self.dot_vao.render(moderngl.TRIANGLES, vertices=6)

        self.ctx.disable(moderngl.BLEND)

    # ── ImGui panel ────────────────────────────────────────────────────────────
    def _draw_panel(self):
        global _paused
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

        imgui.set_next_window_position(PANEL_X, 0)
        imgui.set_next_window_size(PANEL_W, WINDOW_H)
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

        # Pause / Resume button
        pause_label = "|| Pause" if not _paused else ">  Resume"
        p_col = (0.863, 0.275, 0.275, 1.0) if not _paused else (0.275, 0.784, 0.275, 1.0)
        imgui.push_style_color(imgui.COLOR_TEXT,           *p_col)
        imgui.push_style_color(imgui.COLOR_BUTTON,         0.043, 0.098, 0.161, 1.0)
        imgui.push_style_color(imgui.COLOR_BUTTON_HOVERED, p_col[0], p_col[1], p_col[2], 0.3)
        if imgui.button(pause_label, width=PANEL_W - 16):
            _paused = not _paused
            send_pause_toggle()
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
            label = (f"{ant.label}  A={ant.amplitude:.2f}  a={ant.a:.2f}"
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
            imgui.text(ant.label)

            imgui.push_item_width(PANEL_W - 32)

            changed, v = imgui.slider_float("Amplitude##amp", ant.amplitude, 0.0, 1.0)
            if changed:
                ant.amplitude = v
                send_antenna(ant)

            changed, v = imgui.slider_float("Frequency##freq", ant.frequency, 0.1, 10.0)
            if changed:
                ant.frequency = v
                send_antenna(ant)

            changed, v = imgui.slider_float("Direction##theta", ant.theta_0,
                                            0.0, 6.2832, format="%.2f rad")
            if changed:
                ant.theta_0 = v
                send_antenna(ant)

            changed, v = imgui.slider_float("Directivity##a", ant.a, 0.0, 1.0)
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

        # Footer
        imgui.set_cursor_pos((imgui.get_cursor_pos()[0], WINDOW_H - 20))
        imgui.push_style_color(imgui.COLOR_TEXT, 0.545, 0.659, 0.784, 1.0)
        imgui.text("Tab: cycle   Space: pause   Del: remove")
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


if __name__ == "__main__":
    WaveFormApp().run()
