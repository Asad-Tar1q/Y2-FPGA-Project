import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from dataclasses import dataclass
from pathlib import Path


wave settings
WIDTH, HEIGHT = 360, 360
K_WAVE        = 0.2
WAVE_SPEED    = 5.0 
BEAM_POWER    = 6.0
R_CLAMP       = 1.0


# ===========================================================================
#  Data classes
# ===========================================================================

@dataclass
class Antenna:
    """One point source on the grid (angles in DEGREES).

    cx, cy          : pixel position
    gain            : A_n linear amplitude
    directionality  : a in [0,1], 0 isotropic -> 1 fully directional
    direction_deg   : boresight the lobe points, degrees from +x CCW
    phase_deg       : emitted-wave phase, degrees
    """
    cx:             float
    cy:             float
    gain:           float = 1.0
    directionality: float = 0.0
    direction_deg:  float = 0.0
    phase_deg:      float = 0.0


@dataclass
class Reflector:
    """A finite flat mirror segment from (x1,y1) to (x2,y2).

    gamma     : reflection coefficient applied to amplitude (0..1)
    phase_inv : if True, add 180 deg phase flip (conductor boundary)
    """
    x1: float
    y1: float
    x2: float
    y2: float
    gamma:     float = 1.0
    phase_inv: bool  = False


# ===========================================================================
#  Reflector / blocker renderer
# ===========================================================================

class EMFieldRenderer:

    def __init__(self, width=WIDTH, height=HEIGHT, k_wave=K_WAVE, r_clamp=R_CLAMP):
        """Build the pixel grid."""
        self.width, self.height = width, height
        self.k_wave, self.r_clamp = k_wave, r_clamp
        px = np.arange(width,  dtype=np.float64)
        py = np.arange(height, dtype=np.float64)
        self.px_grid, self.py_grid = np.meshgrid(px, py)

    def _radiation_pattern(self, theta, a, boresight_deg):
        """D = (1-a) + a*max(0,cos(theta-boresight))^BEAM_POWER (no back lobe)."""
        a = np.clip(a, 0.0, 1.0)
        boresight = np.deg2rad(boresight_deg)
        forward = np.clip(np.cos(theta - boresight), 0.0, None)
        return (1.0 - a) + a * forward ** BEAM_POWER

    def _field_from_point(self, sx, sy, gain, a, boresight_deg,
                          phase_deg, valid_mask=None):
        """E-field of one point source at (sx,sy), fully propagated.

        valid_mask : optional bool array; False pixels contribute 0.
        """
        dx = self.px_grid - sx
        dy = self.py_grid - sy
        r  = np.maximum(np.sqrt(dx**2 + dy**2), self.r_clamp)
        theta = np.arctan2(dy, dx)
        D = self._radiation_pattern(theta, a, boresight_deg)
        contrib = gain * D * (1.0 / r) * np.cos(-self.k_wave * r
                                                + np.deg2rad(phase_deg))
        if valid_mask is not None:
            contrib = np.where(valid_mask, contrib, 0.0)
        return contrib

    @staticmethod
    def _mirror_point(sx, sy, refl):
        """Mirror a point across the reflector line: s' = s - 2(d)n."""
        ex, ey = refl.x2 - refl.x1, refl.y2 - refl.y1
        nx, ny = -ey, ex
        nlen = np.hypot(nx, ny)
        nx, ny = nx / nlen, ny / nlen
        d = (sx - refl.x1) * nx + (sy - refl.y1) * ny
        return sx - 2.0 * d * nx, sy - 2.0 * d * ny

    def _crosses_segment(self, ox, oy, refl):
        """Bool grid: True where the segment from origin (ox,oy) to the pixel
        crosses the finite reflector segment. Shared geometric test used for
        both blocking (origin = real source) and reflection (origin = image)."""
        x1, y1, x2, y2 = refl.x1, refl.y1, refl.x2, refl.y2
        ex, ey = x2 - x1, y2 - y1                      # reflector direction
        rx, ry = self.px_grid - ox, self.py_grid - oy  # origin -> pixel
        denom = rx * ey - ry * ex
        denom = np.where(np.abs(denom) < 1e-9, 1e-9, denom)
        t_hit = ((x1 - ox) * ey - (y1 - oy) * ex) / denom   # along origin->pixel
        u_hit = ((x1 - ox) * ry - (y1 - oy) * rx) / denom   # along reflector
        return (u_hit >= 0.0) & (u_hit <= 1.0) & (t_hit >= 0.0) & (t_hit <= 1.0)

    def render(self, antennas, reflector=None):
        """Steady-state field. The reflector acts as both reflector and blocker:
        reflected wave in front (gated to the segment), shadow behind."""
        field = np.zeros((self.height, self.width), dtype=np.float64)
        for ant in antennas:
            # Direct field, blocked behind the opaque mirror.
            direct_mask = None
            if reflector is not None:
                direct_mask = ~self._crosses_segment(ant.cx, ant.cy, reflector)
            field += self._field_from_point(
                ant.cx, ant.cy, ant.gain, ant.directionality,
                ant.direction_deg, ant.phase_deg, valid_mask=direct_mask)

            # Reflected field via the image source, gated to the segment.
            if reflector is not None:
                ix, iy = self._mirror_point(ant.cx, ant.cy, reflector)
                valid = self._crosses_segment(ix, iy, reflector)
                phase = ant.phase_deg + (180.0 if reflector.phase_inv else 0.0)
                field += self._field_from_point(
                    ix, iy, ant.gain * reflector.gamma,
                    ant.directionality, ant.direction_deg, phase,
                    valid_mask=valid)
        return field


def simulate_reflector(antennas, reflector, out_path="reflection_demo.png"):
    """Render a reflector scene and draw the mirror as a black line."""
    field = EMFieldRenderer().render(antennas, reflector=reflector)
    vmax = np.percentile(np.abs(field), 99) or 1.0
    rgb  = plt.get_cmap("RdBu")(mcolors.Normalize(-vmax, vmax)(field))[:, :, :3]

    fig, ax = plt.subplots(figsize=(5, 5))
    ax.imshow(rgb)
    ax.plot([reflector.x1, reflector.x2],
            [reflector.y1, reflector.y2], color="black", linewidth=3)
    ax.set_xlim(0, WIDTH); ax.set_ylim(HEIGHT, 0)
    ax.axis("off")
    fig.tight_layout(pad=0.1)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"reflector -> {Path(out_path).resolve()}")
    return field


# ===========================================================================
#  Moving-source Doppler: exact vs first-order approximation
# ===========================================================================

def _grids():
    px = np.arange(WIDTH, dtype=np.float64)
    py = np.arange(HEIGHT, dtype=np.float64)
    return np.meshgrid(px, py)


def doppler_exact(px, py, s_now, u):
    """Per-pixel exact retarded time via the quadratic solution."""
    dx, dy = px - s_now[0], py - s_now[1]
    d_dot_u = dx * u[0] + dy * u[1]
    d2 = dx * dx + dy * dy
    a = WAVE_SPEED**2 - (u[0]**2 + u[1]**2)
    tau = (d_dot_u + np.sqrt(np.maximum(d_dot_u**2 + a * d2, 0.0))) / a
    sx, sy = s_now[0] - u[0] * tau, s_now[1] - u[1] * tau
    r = np.maximum(np.sqrt((px - sx)**2 + (py - sy)**2), R_CLAMP)
    return (1.0 / r) * np.cos(-K_WAVE * r)


def doppler_approx(px, py, s_now, u):
    """First-order one-step approximation: single delay from current distance."""
    r0 = np.maximum(np.sqrt((px - s_now[0])**2 + (py - s_now[1])**2), R_CLAMP)
    tau0 = r0 / WAVE_SPEED
    sx, sy = s_now[0] - u[0] * tau0, s_now[1] - u[1] * tau0
    r = np.maximum(np.sqrt((px - sx)**2 + (py - sy)**2), R_CLAMP)
    return (1.0 / r) * np.cos(-K_WAVE * r)


def simulate_doppler(s_now, u, out_exact="doppler_exact.png",
                     out_approx="doppler_approx.png"):
    """Two separate borderless panels (no titles): exact quadratic and
    first-order approximation, on a shared colour scale."""
    px, py = _grids()
    exact = doppler_exact(px, py, s_now, u)
    approx = doppler_approx(px, py, s_now, u)

    vmax = max(np.percentile(np.abs(exact), 99),
               np.percentile(np.abs(approx), 99)) or 1.0
    norm = mcolors.Normalize(-vmax, vmax)
    cmap = plt.get_cmap("RdBu")

    for data, out_path in ((exact, out_exact), (approx, out_approx)):
        fig, ax = plt.subplots(figsize=(5, 5))
        ax.imshow(cmap(norm(data))[:, :, :3])
        ax.annotate("", xy=(s_now[0] + 38, s_now[1]), xytext=(s_now[0], s_now[1]),
                    arrowprops=dict(arrowstyle="->", color="black", lw=1.5))
        ax.set_xticks([]); ax.set_yticks([])
        ax.axis("off")
        fig.tight_layout(pad=0.1)
        fig.savefig(out_path, dpi=150)
        plt.close(fig)
        print(f"doppler -> {Path(out_path).resolve()}")


# ===========================================================================
#  Entry point
# ===========================================================================

if __name__ == "__main__":
    # Reflector + blocker: source left of a vertical mirror.
    simulate_reflector(
        [Antenna(cx=110, cy=180, gain=1.0)],
        Reflector(x1=250, y1=60, x2=250, y2=300, gamma=0.9),
        out_path="reflection_demo.png",
    )

    # Moving source at a moderate speed (|u|/v = 0.35), motion in +x.
    simulate_doppler(
        s_now=np.array([180.0, 180.0]),
        u=np.array([1.75, 0.0]),
        out_exact="doppler_exact.png",
        out_approx="doppler_approx.png"
    )
