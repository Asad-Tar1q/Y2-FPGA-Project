"""
WaveForm — EM Field Renderer client.
Run:  python client.py
Connects to pynq_sim.py on localhost. Falls back to demo mode if unavailable.
"""

import sys
import time
import threading
import socket
import struct
import math

import pygame
import numpy as np

# ── Dimensions ─────────────────────────────────────────────────────────────────
WINDOW_W    = 920
WINDOW_H    = 480
FIELD_W     = 640
FIELD_H     = 480
PANEL_X     = 640
PANEL_W     = 280
GRID_STEP   = 40
MAX_SOURCES = 8
MAX_VIS     = 6       # max source-list rows before scrolling
MIN_SEP     = 26      # minimum center-to-center px between sources

# ── Network ───────────────────────────────────────────────────────────────────
HOST       = "127.0.0.1"
FRAME_PORT = 5000
CTRL_PORT  = 5001
CTRL_FMT   = ">BBHHff2x"
CTRL_SIZE  = struct.calcsize(CTRL_FMT)   # 16 bytes
SEND_HZ    = 30

# ── Colour palette ─────────────────────────────────────────────────────────────
C_BG        = ( 11,  25,  41)
C_PANEL     = ( 15,  32,  53)
C_CYAN      = (  0, 200, 220)
C_MINT      = (168, 255, 216)
C_LAVENDER  = (176, 196, 255)
C_WHITE     = (255, 255, 255)
C_SEC       = (139, 168, 200)
C_BORDER    = ( 26,  53,  80)
C_SELROW    = ( 19,  40,  64)
C_DANGER    = (220,  70,  70)
C_AMBER     = (239, 159,  39)

SOURCE_COLS = [
    C_CYAN, C_AMBER,
    (220, 100, 220), (100, 220, 100),
    (220, 220, 100), (100, 180, 220),
    (220, 140, 100), (160, 100, 220),
]

# ── CUDA / NumPy LUT ──────────────────────────────────────────────────────────
_lut: np.ndarray | None = None
_cuda_ready = False
_cuda       = None
_d_lut      = None
_gpu_render = None


def _build_viridis() -> np.ndarray:
    t = np.linspace(0.0, 1.0, 256)
    r = np.clip(0.267 + 0.004*t + 2.334*t**2 - 3.567*t**3 + 1.765*t**4, 0, 1)
    g = np.clip(0.005 + 1.466*t - 0.720*t**2 + 0.222*t**3 - 0.028*t**4, 0, 1)
    b = np.clip(0.329 + 1.498*t - 4.773*t**2 + 5.282*t**3 - 2.143*t**4, 0, 1)
    return (np.stack([r, g, b], axis=1) * 255).astype(np.uint8)


def _init_lut():
    global _lut, _cuda_ready, _cuda, _d_lut, _gpu_render
    _lut = _build_viridis()
    try:
        from numba import cuda as _nc

        @_nc.jit
        def _kern(frame_flat, lut, rgb_out):
            idx = _nc.grid(1)
            if idx < frame_flat.size:
                v = frame_flat[idx]
                rgb_out[idx, 0] = lut[v, 0]
                rgb_out[idx, 1] = lut[v, 1]
                rgb_out[idx, 2] = lut[v, 2]

        _cuda       = _nc
        _d_lut      = _nc.to_device(_lut)
        _gpu_render = _kern
        _cuda_ready = True
        print("[client] CUDA colour mapping enabled")
    except Exception as exc:
        print(f"[client] CUDA unavailable ({exc.__class__.__name__}), using NumPy")


def apply_lut(frame: np.ndarray) -> np.ndarray:
    """uint8 (H,W) → RGB (H,W,3) via viridis LUT."""
    if _cuda_ready:
        try:
            flat    = frame.ravel()
            d_frame = _cuda.to_device(flat)
            d_out   = _cuda.device_array((flat.size, 3), dtype=np.uint8)
            threads = 256
            blocks  = (flat.size + threads - 1) // threads
            _gpu_render[blocks, threads](d_frame, _d_lut, d_out)
            return d_out.copy_to_host().reshape(frame.shape[0], frame.shape[1], 3)
        except Exception:
            pass
    return _lut[frame]


# ── Antenna model ─────────────────────────────────────────────────────────────
_next_id = 0


class Antenna:
    def __init__(self, x: float = FIELD_W / 2, y: float = FIELD_H / 2,
                 amplitude: float = 0.75, frequency: float = 1.0):
        global _next_id
        self.id        = _next_id
        _next_id      += 1
        self.x         = float(x)
        self.y         = float(y)
        self.amplitude = float(amplitude)
        self.frequency = float(frequency)

    @property
    def colour(self):
        return SOURCE_COLS[self.id % len(SOURCE_COLS)]

    @property
    def label(self):
        return f"A{self.id + 1}"


# ── Networking ────────────────────────────────────────────────────────────────
_frame_lock   = threading.Lock()
_latest_frame: np.ndarray | None = None
_recv_fps     = 0.0

_ctrl_sock    = None
_ctrl_lock    = threading.Lock()
_last_send_t  = 0.0


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
                raw = buf[:length]
                buf = buf[length:]
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(FIELD_H, FIELD_W)
                with _frame_lock:
                    _latest_frame = frame.copy()
                n += 1
                now = time.monotonic()
                if now - t0 >= 1.0:
                    _recv_fps = n / (now - t0)
                    n, t0     = 0, now
        except Exception as e:
            print(f"[client] frame stream lost ({e.__class__.__name__}) — retrying")
            time.sleep(2)


def _connect_ctrl() -> bool:
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


def _raw_send(ant: Antenna):
    msg = struct.pack(CTRL_FMT,
                      ant.id & 0xFF, 0,
                      int(ant.x) & 0xFFFF, int(ant.y) & 0xFFFF,
                      ant.amplitude, ant.frequency)
    with _ctrl_lock:
        if _ctrl_sock:
            try:
                _ctrl_sock.sendall(msg)
            except Exception:
                pass


def send_antenna(ant: Antenna, force: bool = False):
    global _last_send_t
    now = time.monotonic()
    if not force and now - _last_send_t < 1.0 / SEND_HZ:
        return
    _last_send_t = now
    _raw_send(ant)


def send_all(antennas):
    for a in antennas:
        _raw_send(a)


def send_delete(ant_id: int):
    """Signal the sim to remove this antenna (cmd byte = 1)."""
    msg = struct.pack(CTRL_FMT, ant_id & 0xFF, 1, 0, 0, 0.0, 0.0)
    with _ctrl_lock:
        if _ctrl_sock:
            try:
                _ctrl_sock.sendall(msg)
            except Exception:
                pass


# ── Drawing helpers ───────────────────────────────────────────────────────────
def lerp_col(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def draw_grad_text(surf, text, font, x, y, c1, c2):
    total_w = font.size(text)[0]
    cx = x
    for i, ch in enumerate(text):
        cw = font.size(ch)[0]
        t  = (cx - x + cw / 2) / max(total_w, 1)
        glyph = font.render(ch, True, lerp_col(c1, c2, t))
        surf.blit(glyph, (cx, y))
        cx += cw


def draw_border(surf, rect: pygame.Rect, radius: int, col, alpha: int = 255, w: int = 1):
    s = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
    pygame.draw.rect(s, col + (alpha,), s.get_rect(), w, border_radius=radius)
    surf.blit(s, rect.topleft)


def draw_fill(surf, rect: pygame.Rect, radius: int, col, alpha: int = 255):
    s = pygame.Surface((rect.w, rect.h), pygame.SRCALPHA)
    pygame.draw.rect(s, col + (alpha,), s.get_rect(), border_radius=radius)
    surf.blit(s, rect.topleft)


def draw_ripple(surf, cx: int, cy: int, n: int = 5, max_r: int = 52):
    s = pygame.Surface((max_r * 2 + 4, max_r * 2 + 4), pygame.SRCALPHA)
    for i in range(1, n + 1):
        r = int(max_r * i / n)
        pygame.draw.arc(s, C_CYAN + (40,),
                        (max_r - r, max_r - r, r * 2, r * 2),
                        0.0, 1.5708, 2)
    surf.blit(s, (cx - max_r, cy - max_r))


# ── Slider ────────────────────────────────────────────────────────────────────
class Slider:
    def __init__(self, vmin: float, vmax: float, value: float,
                 fill_col, label: str, fmt):
        self.vmin     = vmin
        self.vmax     = vmax
        self.value    = value
        self.fill_col = fill_col
        self.label    = label
        self.fmt      = fmt
        self.dragging = False
        self.rect     = pygame.Rect(0, 0, 100, 14)

    def set_rect(self, x: int, y: int, w: int):
        self.rect = pygame.Rect(x, y, w, 14)

    def _thumb_x(self) -> int:
        t = (self.value - self.vmin) / max(self.vmax - self.vmin, 1e-9)
        return self.rect.x + int(t * self.rect.w)

    def _set_from_x(self, px: int):
        t = (px - self.rect.x) / max(self.rect.w, 1)
        self.value = max(self.vmin, min(self.vmax,
                         self.vmin + t * (self.vmax - self.vmin)))

    def handle(self, ev) -> bool:
        if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            hit = self.rect.inflate(0, 20).collidepoint(ev.pos)
            if hit:
                self.dragging = True
                self._set_from_x(ev.pos[0])
                return True
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            if self.dragging:
                self.dragging = False
                return True
        elif ev.type == pygame.MOUSEMOTION and self.dragging:
            self._set_from_x(ev.pos[0])
            return True
        return False

    def draw(self, surf, font):
        r  = self.rect
        tx = self._thumb_x()
        # Track
        pygame.draw.rect(surf, C_BORDER,
                         (r.x, r.centery - 2, r.w, 4), border_radius=2)
        # Fill
        fw = max(0, tx - r.x)
        if fw:
            pygame.draw.rect(surf, self.fill_col,
                             (r.x, r.centery - 2, fw, 4), border_radius=2)
        # Thumb
        pygame.draw.circle(surf, C_WHITE,  (tx, r.centery), 7)
        pygame.draw.circle(surf, C_BORDER, (tx, r.centery), 7, 1)
        # Labels above track
        lbl = font.render(self.label, True, C_SEC)
        surf.blit(lbl, (r.x, r.y - 13))
        val = font.render(self.fmt(self.value), True, C_WHITE)
        surf.blit(val, (r.right - val.get_width(), r.y - 13))


# ── Main Application ──────────────────────────────────────────────────────────
class WaveFormApp:
    def __init__(self):
        pygame.init()
        self.screen = pygame.display.set_mode(
            (WINDOW_W, WINDOW_H), pygame.SCALED | pygame.RESIZABLE)
        pygame.display.set_caption("WaveForm — EM Field Renderer")
        self.clock  = pygame.time.Clock()

        self.font_title = pygame.font.SysFont("Courier New", 20, bold=True)
        self.font_md    = pygame.font.SysFont("Courier New", 14, bold=True)
        self.font_sm    = pygame.font.SysFont("Courier New", 11)

        self.antennas: list[Antenna] = [Antenna()]
        self.sel         = 0
        self._list_off   = 0      # scroll offset for source list

        # Persistent slider objects — rects updated each draw
        self._amp_sl  = Slider(0.0, 10.0,  0.75, C_CYAN,
                               "Amplitude",     lambda v: f"{v:.2f}")
        self._freq_sl = Slider(0.1, 10.0,  1.0,  C_CYAN,
                               "Frequency (×)", lambda v: f"×{v:.2f}")
        self._sync_sliders()

        # Interaction state
        self._drag_ant      = None
        self._last_drag_t   = 0.0

        # Layout rects (set during draw, read during events)
        self._row_rects: list[pygame.Rect] = []
        self._add_rect  = pygame.Rect(0, 0, 0, 0)
        self._del_rect  = pygame.Rect(0, 0, 0, 0)

        # FPS tracking
        self._disp_fps    = 0.0
        self._disp_n      = 0
        self._disp_t      = time.monotonic()

    # ── Sync helpers ───────────────────────────────────────────────────────────
    def _sync_sliders(self):
        if not self.antennas:
            self._amp_sl.dragging  = False
            self._freq_sl.dragging = False
            return
        a = self.antennas[self.sel]
        self._amp_sl.value     = a.amplitude
        self._amp_sl.fill_col  = a.colour
        self._freq_sl.value    = a.frequency
        self._amp_sl.dragging  = False
        self._freq_sl.dragging = False

    def _clamp_scroll(self):
        if not self.antennas:
            self._list_off = 0
            return
        n = len(self.antennas)
        max_off = max(0, n - MAX_VIS)
        self._list_off = max(0, min(self._list_off, max_off))
        if self.sel < self._list_off:
            self._list_off = self.sel
        elif self.sel >= self._list_off + MAX_VIS:
            self._list_off = self.sel - MAX_VIS + 1
        self._list_off = max(0, min(self._list_off, max_off))

    def _overlaps_any(self, x: float, y: float, exclude: int = -1) -> bool:
        for i, a in enumerate(self.antennas):
            if i == exclude:
                continue
            if (x - a.x) ** 2 + (y - a.y) ** 2 < MIN_SEP ** 2:
                return True
        return False

    def _find_free_pos(self) -> tuple[float, float]:
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

    # ── Run ───────────────────────────────────────────────────────────────────
    def run(self):
        _init_lut()
        threading.Thread(target=_recv_loop, daemon=True).start()
        _connect_ctrl()
        send_all(self.antennas)

        while True:
            self.clock.tick(60)
            self._disp_n += 1
            now = time.monotonic()
            if now - self._disp_t >= 1.0:
                self._disp_fps = self._disp_n / (now - self._disp_t)
                self._disp_n, self._disp_t = 0, now

            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    pygame.quit()
                    sys.exit()
                self._handle(ev)

            self._draw()
            pygame.display.flip()

    # ── Event handling ─────────────────────────────────────────────────────────
    def _handle(self, ev):
        ant = self.antennas[self.sel] if self.antennas else None

        # Sliders take priority (only when a source is selected)
        if ant and self._amp_sl.handle(ev):
            ant.amplitude = self._amp_sl.value
            send_antenna(ant)
            return
        if ant and self._freq_sl.handle(ev):
            ant.frequency = self._freq_sl.value
            send_antenna(ant)
            return

        if ev.type == pygame.KEYDOWN:
            if ev.key == pygame.K_F11:
                pygame.display.toggle_fullscreen()
            elif ev.key == pygame.K_TAB and self.antennas:
                self.sel = (self.sel + 1) % len(self.antennas)
                self._clamp_scroll()
                self._sync_sliders()
            elif ev.key in (pygame.K_DELETE, pygame.K_BACKSPACE):
                self._del_ant()

        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            mx, my = ev.pos
            if mx < FIELD_W:
                self._field_click(mx, my)
            else:
                self._panel_click(mx, my)

        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            if self._drag_ant is not None:
                send_antenna(self.antennas[self._drag_ant], force=True)
            self._drag_ant = None

        elif ev.type == pygame.MOUSEMOTION and self._drag_ant is not None:
            mx, my = ev.pos
            a  = self.antennas[self._drag_ant]
            nx = float(max(0, min(FIELD_W - 1, mx)))
            ny = float(max(0, min(FIELD_H - 1, my)))
            if not self._overlaps_any(nx, ny, self._drag_ant):
                a.x, a.y = nx, ny
            now = time.monotonic()
            if now - self._last_drag_t >= 1.0 / SEND_HZ:
                send_antenna(a)
                self._last_drag_t = now

    def _ant_at(self, mx, my) -> int | None:
        for i, a in enumerate(self.antennas):
            if (mx - a.x) ** 2 + (my - a.y) ** 2 <= 144:   # r = 12
                return i
        return None

    def _field_click(self, mx, my):
        hit = self._ant_at(mx, my)
        if hit is not None:
            self.sel = hit
            self._clamp_scroll()
            self._sync_sliders()
            self._drag_ant = hit
        elif self.antennas:
            # Teleport only if destination is free
            if not self._overlaps_any(float(mx), float(my), self.sel):
                ant = self.antennas[self.sel]
                ant.x, ant.y = float(mx), float(my)
                send_antenna(ant, force=True)

    def _panel_click(self, mx, my):
        if self._add_rect.collidepoint(mx, my):
            self._add_ant()
        elif self._del_rect.collidepoint(mx, my):
            self._del_ant()
        else:
            for vis_idx, r in enumerate(self._row_rects):
                if r.collidepoint(mx, my):
                    self.sel = self._list_off + vis_idx
                    self._clamp_scroll()
                    self._sync_sliders()
                    break

    def _add_ant(self):
        if len(self.antennas) >= MAX_SOURCES:
            return
        x, y = self._find_free_pos()
        a = Antenna(x, y)
        self.antennas.append(a)
        self.sel = len(self.antennas) - 1
        self._clamp_scroll()
        self._sync_sliders()
        send_antenna(a, force=True)

    def _del_ant(self):
        if not self.antennas:
            return
        ant = self.antennas[self.sel]
        send_delete(ant.id)          # tell sim to remove this antenna
        del self.antennas[self.sel]
        if self.antennas:
            self.sel = min(self.sel, len(self.antennas) - 1)
        else:
            self.sel = 0
        self._clamp_scroll()
        self._sync_sliders()

    # ── Drawing ────────────────────────────────────────────────────────────────
    def _draw(self):
        self.screen.fill(C_BG)
        self._draw_field()
        self._draw_panel()

    # ── Field ──────────────────────────────────────────────────────────────────
    def _draw_field(self):
        s = self.screen

        # Grid
        for x in range(0, FIELD_W + 1, GRID_STEP):
            pygame.draw.line(s, C_BORDER, (x, 0), (x, FIELD_H))
        for y in range(0, FIELD_H + 1, GRID_STEP):
            pygame.draw.line(s, C_BORDER, (0, y), (FIELD_W, y))

        # Frame overlay
        with _frame_lock:
            frame = _latest_frame
        if frame is not None:
            rgb  = apply_lut(frame)
            surf = pygame.surfarray.make_surface(rgb.swapaxes(0, 1))
            surf.set_alpha(210)
            s.blit(surf, (0, 0))

        # Source dots
        for i, a in enumerate(self.antennas):
            ix, iy = int(a.x), int(a.y)
            if i == self.sel:
                pygame.draw.circle(s, C_WHITE, (ix, iy), 14, 2)
            pygame.draw.circle(s, a.colour, (ix, iy), 10)
            lbl = self.font_sm.render(a.label, True, C_WHITE)
            s.blit(lbl, (ix - lbl.get_width() // 2, iy - lbl.get_height() // 2))

        # HUD
        lines = [
            f"N={len(self.antennas)}",
            f"recv {_recv_fps:5.1f} fps",
            f"disp {self._disp_fps:5.1f} fps",
        ]
        lh  = 15
        hw  = 116
        hh  = len(lines) * lh + 6
        hud = pygame.Surface((hw, hh), pygame.SRCALPHA)
        hud.fill((0, 0, 0, 110))
        s.blit(hud, (6, 6))
        for i, ln in enumerate(lines):
            t = self.font_sm.render(ln, True, C_CYAN)
            s.blit(t, (10, 9 + i * lh))

    # ── Panel ──────────────────────────────────────────────────────────────────
    def _draw_panel(self):
        s  = self.screen
        px = PANEL_X
        pw = PANEL_W

        # Background
        pygame.draw.rect(s, C_PANEL, (px, 0, pw, WINDOW_H))

        # Ripple decoration (corner motif)
        draw_ripple(s, px + 2, 2, n=5, max_r=50)

        # ── Header ────────────────────────────────────────────────────────────
        draw_grad_text(s, "WaveForm", self.font_title, px + 14, 7, C_MINT, C_LAVENDER)
        sub = self.font_sm.render("EM SOURCE CONTROL", True, C_SEC)
        s.blit(sub, (px + 14, 31))
        pygame.draw.line(s, C_BORDER, (px, 50), (px + pw, 50), 1)

        # ── Source list ────────────────────────────────────────────────────────
        row_h   = 22
        list_y  = 56
        n_vis   = min(len(self.antennas), MAX_VIS)
        card_h  = max(n_vis, 1) * row_h + 8
        card_r  = pygame.Rect(px + 8, list_y, pw - 16, card_h)

        draw_fill  (s, card_r, 6, C_SELROW, alpha=255)
        draw_border(s, card_r, 6, C_MINT,   alpha=76)

        if not self.antennas:
            empty_t = self.font_sm.render("(no sources)", True, C_SEC)
            s.blit(empty_t, (card_r.centerx - empty_t.get_width() // 2,
                             list_y + 9))

        self._clamp_scroll()
        self._row_rects = []
        for vis_i in range(n_vis):
            ant_i    = self._list_off + vis_i
            a        = self.antennas[ant_i]
            ry       = list_y + 4 + vis_i * row_h
            row_r    = pygame.Rect(px + 8, ry, pw - 16, row_h)
            self._row_rects.append(row_r)
            if ant_i == self.sel:
                draw_fill  (s, row_r, 4, C_SELROW, alpha=255)
                draw_border(s, row_r, 4, C_CYAN,   alpha=255, w=1)
            pygame.draw.circle(s, a.colour, (px + 20, ry + row_h // 2), 5)
            lbl  = self.font_sm.render(a.label, True, C_WHITE)
            s.blit(lbl,  (px + 30,  ry + 5))
            amp  = self.font_sm.render(f"{a.amplitude:.2f}", True, C_SEC)
            s.blit(amp,  (px + 58,  ry + 5))
            freq = self.font_sm.render(f"×{a.frequency:.2f}", True, C_SEC)
            s.blit(freq, (px + 102, ry + 5))
            pos  = self.font_sm.render(f"({int(a.x)},{int(a.y)})", True, C_SEC)
            s.blit(pos,  (px + 155, ry + 5))

        # Scroll indicator dots if list is clipped
        if len(self.antennas) > MAX_VIS:
            total  = len(self.antennas)
            dot_x0 = card_r.right - 10
            for i in range(total):
                col = C_CYAN if i == self.sel else C_BORDER
                pygame.draw.circle(s, col,
                                   (dot_x0 - (total - 1 - i) * 6,
                                    list_y + card_h + 4), 2)

        # ── Add / Del buttons ──────────────────────────────────────────────────
        btn_y = list_y + card_h + 8 + (6 if len(self.antennas) > MAX_VIS else 0)
        self._add_rect = pygame.Rect(px + 10, btn_y, 92, 20)
        self._del_rect = pygame.Rect(px + 110, btn_y, 92, 20)

        pygame.draw.rect(s, C_PANEL,  self._add_rect, border_radius=4)
        pygame.draw.rect(s, C_CYAN,   self._add_rect, 1, border_radius=4)
        pygame.draw.rect(s, C_PANEL,  self._del_rect, border_radius=4)
        pygame.draw.rect(s, C_DANGER, self._del_rect, 1, border_radius=4)
        add_t = self.font_sm.render("+ Add", True, C_CYAN)
        del_t = self.font_sm.render("× Del", True, C_DANGER)
        s.blit(add_t, (self._add_rect.centerx - add_t.get_width() // 2, btn_y + 4))
        s.blit(del_t, (self._del_rect.centerx - del_t.get_width() // 2, btn_y + 4))

        # ── Divider ────────────────────────────────────────────────────────────
        div_y = btn_y + 26
        pygame.draw.line(s, C_BORDER, (px + 8, div_y), (px + pw - 8, div_y), 1)

        # ── Selected source controls ───────────────────────────────────────────
        ctrl_y  = div_y + 6
        ctrl_h  = WINDOW_H - ctrl_y - 22
        ctrl_r  = pygame.Rect(px + 8, ctrl_y, pw - 16, ctrl_h)
        draw_border(s, ctrl_r, 6, C_MINT, alpha=76)

        if not self.antennas:
            none_t = self.font_sm.render("(no source selected)", True, C_SEC)
            s.blit(none_t, (ctrl_r.centerx - none_t.get_width() // 2,
                            ctrl_y + 14))
        else:
            ant = self.antennas[self.sel]

            # Sub-header (dot + label)
            pygame.draw.circle(s, ant.colour, (px + 20, ctrl_y + 13), 6)
            hdr = self.font_md.render(ant.label, True, C_WHITE)
            s.blit(hdr, (px + 32, ctrl_y + 5))

            # Update slider rects (preserves dragging state)
            sl_x = px + 16
            sl_w = pw - 32
            self._amp_sl.fill_col = ant.colour
            self._amp_sl.set_rect (sl_x, ctrl_y + 40, sl_w)
            self._freq_sl.set_rect(sl_x, ctrl_y + 86, sl_w)

            if not self._amp_sl.dragging:
                self._amp_sl.value = ant.amplitude
            if not self._freq_sl.dragging:
                self._freq_sl.value = ant.frequency

            self._amp_sl.draw (s, self.font_sm)
            self._freq_sl.draw(s, self.font_sm)

            # Position readout
            pos_y = ctrl_y + 112
            pos_t = self.font_sm.render(
                f"x {int(ant.x):>3d}  y {int(ant.y):>3d}", True, C_WHITE)
            s.blit(pos_t, (px + 16, pos_y))

            # Minimap (56×56)
            mm_x, mm_y, mm_w, mm_h = px + 16, pos_y + 16, 56, 56
            mm_r = pygame.Rect(mm_x, mm_y, mm_w, mm_h)
            pygame.draw.rect(s, C_BG, mm_r)
            draw_border(s, mm_r, 4, C_MINT, alpha=76)
            dot_x = mm_x + int(ant.x / FIELD_W * mm_w)
            dot_y = mm_y + int(ant.y / FIELD_H * mm_h)
            pygame.draw.circle(s, ant.colour, (dot_x, dot_y), 3)
            drag_t = self.font_sm.render("drag on field", True, C_SEC)
            s.blit(drag_t, (mm_x + mm_w + 6, mm_y + 20))

        # ── Footer ─────────────────────────────────────────────────────────────
        foot = self.font_sm.render(
            "Tab: cycle   Del: remove   F11: fullscreen", True, C_SEC)
        s.blit(foot, (px + 8, WINDOW_H - 16))


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    WaveFormApp().run()
