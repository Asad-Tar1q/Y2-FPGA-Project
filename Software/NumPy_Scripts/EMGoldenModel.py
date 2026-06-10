"""
EM Field Golden Model — NumPy reference renderer
=================================================
Implements the analytic far-field superposition model from the project brief:

    E(r, t) = Σ_n  A_n · D_n(θ_n) · (1/r_n) · cos(ωt − k·r_n + φ_n)

where for each source n:
    r_n  = Euclidean distance from pixel to source n
    θ_n  = angle from source n to pixel (for radiation pattern)
    A_n  = amplitude
    φ_n  = phase offset
    D_n  = radiation pattern function (isotropic or dipole)
    k    = 2π/λ  (wavenumber)
    ωt   = current time phase
"""

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# ---------------------------------------------------------------------------
# Radiation pattern types
# ---------------------------------------------------------------------------

class RadiationPattern(Enum):
    ISOTROPIC = "isotropic"   # D(θ) = 1  (omnidirectional)
    DIPOLE    = "dipole"      # D(θ) = |sin(theta)|  (broadside null)


# ---------------------------------------------------------------------------
# Antenna dataclass
# ---------------------------------------------------------------------------

@dataclass
class Antenna:
    """A single point source on the 2-D grid.

    x, y      : position in grid pixels (floats allowed)
    amplitude : A_n  (linear, arbitrary units)
    phase_rad : φ_n  (radians)
    pattern   : RadiationPattern enum
    """
    x:         float
    y:         float
    amplitude: float = 1.0
    phase_rad: float = 0.0
    pattern:   RadiationPattern = RadiationPattern.ISOTROPIC


# ---------------------------------------------------------------------------
# Output mode
# ---------------------------------------------------------------------------

class OutputMode(Enum):
    E_SIGNED  = "E"        # raw signed field (positive + negative)
    E_ABS     = "|E|"      # absolute value
    E_SQ      = "|E|^2"    # intensity (power)
    PHASE     = "phi"      # instantaneous phase of the dominant source


# ---------------------------------------------------------------------------
# Core renderer
# ---------------------------------------------------------------------------

class EMFieldRenderer:

    def __init__(
        self,
        width:   int   = 200,
        height:  int   = 200,
        k_wave:  float = 0.2,    # λ ≈ 31 pixels at default
        omega_t: float = 0.0,
        r_clamp: float = 1.0,
    ):
        self.width   = width
        self.height  = height
        self.k_wave  = k_wave
        self.omega_t = omega_t
        self.r_clamp = r_clamp

        # Pre-build pixel coordinate grids — shape (H, W)
        # Origin at grid centre so sources can be placed naturally
        cx, cy = width / 2.0, height / 2.0
        px = np.arange(width,  dtype=np.float64) - cx   # (W,)
        py = np.arange(height, dtype=np.float64) - cy   # (H,)
        self.px_grid, self.py_grid = np.meshgrid(px, py)  # both (H, W)

    # ------------------------------------------------------------------
    # Radiation pattern helper
    # ------------------------------------------------------------------

    def _radiation_pattern(
        self,
        theta: np.ndarray,
        pattern: RadiationPattern,
    ) -> np.ndarray:
        """Evaluate D_n(θ) for the given pattern type.

        theta : array of angles in radians (any shape)
        Returns array of same shape, values in [0, 1].
        """
        if pattern == RadiationPattern.ISOTROPIC:
            return np.ones_like(theta)
        elif pattern == RadiationPattern.DIPOLE:
            # |sin(θ)| — null along the axis, maximum broadside
            return np.abs(np.sin(theta))
        else:
            raise ValueError(f"Unknown pattern: {pattern}")

    # ------------------------------------------------------------------
    # Per-antenna contribution
    # ------------------------------------------------------------------

    def _antenna_field(self, ant: Antenna) -> np.ndarray:
        """Return the E-field contribution of a single antenna, shape (H, W).

        Physics:
            E_n(r, t) = A_n · D_n(θ_n) · (1/r_n) · cos(ωt − k·r_n + φ_n)
        """
        # Displacement from antenna to each pixel
        dx = self.px_grid - (ant.x - self.width  / 2.0)
        dy = self.py_grid - (ant.y - self.height / 2.0)

        # Distance r_n — clamp to avoid 1/r blow-up at the source location
        r = np.sqrt(dx**2 + dy**2)
        r = np.maximum(r, self.r_clamp)

        # Angle θ_n (measured from the +x axis, CCW)
        theta = np.arctan2(dy, dx)

        # Radiation pattern D_n(θ_n)
        D = self._radiation_pattern(theta, ant.pattern)

        # Phase argument:  ωt − k·r_n + φ_n
        phase_arg = self.omega_t - self.k_wave * r + ant.phase_rad

        # Assemble contribution
        return ant.amplitude * D * (1.0 / r) * np.cos(phase_arg)

    # ------------------------------------------------------------------
    # Full frame render
    # ------------------------------------------------------------------

    def render(
        self,
        antennas:    list,
        output_mode: OutputMode = OutputMode.E_SIGNED,
    ) -> np.ndarray:
        """Render one frame for the given antenna list.

        Returns a float64 array of shape (H, W).
        Values are NOT normalised — the caller handles tone-mapping.
        """
        if not antennas:
            return np.zeros((self.height, self.width), dtype=np.float64)

        # Superposition: E = Σ E_n
        field = np.zeros((self.height, self.width), dtype=np.float64)
        for ant in antennas:
            field += self._antenna_field(ant)

        # Apply output mode
        if output_mode == OutputMode.E_SIGNED:
            return field
        elif output_mode == OutputMode.E_ABS:
            return np.abs(field)
        elif output_mode == OutputMode.E_SQ:
            return field ** 2
        elif output_mode == OutputMode.PHASE:
            # Instantaneous phase of the dominant (first) antenna
            dx = self.px_grid - (antennas[0].x - self.width  / 2.0)
            dy = self.py_grid - (antennas[0].y - self.height / 2.0)
            r  = np.maximum(np.sqrt(dx**2 + dy**2), self.r_clamp)
            return (self.omega_t - self.k_wave * r + antennas[0].phase_rad) % (2 * np.pi)
        else:
            raise ValueError(f"Unknown output mode: {output_mode}")


# ---------------------------------------------------------------------------
# Tone-mapping helpers
# ---------------------------------------------------------------------------

def tonemap_rdbu(field: np.ndarray) -> np.ndarray:
    """Map a signed field to an RGB image using the RdBu diverging colormap.

    Symmetric around zero: negative → red, zero → white, positive → blue.
    Returns uint8 array of shape (H, W, 3).
    """
    cmap  = plt.get_cmap("RdBu")
    vmax  = np.percentile(np.abs(field), 99) or 1.0  # robust normalisation
    norm  = mcolors.Normalize(vmin=-vmax, vmax=vmax)
    rgb   = cmap(norm(field))[:, :, :3]              # drop alpha channel
    return (rgb * 255).astype(np.uint8)


def tonemap_viridis(field: np.ndarray) -> np.ndarray:
    """Map a non-negative field (|E| or |E|²) to RGB using viridis.

    Returns uint8 array of shape (H, W, 3).
    """
    cmap = plt.get_cmap("viridis")
    vmin, vmax = field.min(), np.percentile(field, 99)
    if vmax == vmin:
        vmax = vmin + 1.0
    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
    rgb  = cmap(norm(field))[:, :, :3]
    return (rgb * 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# PSNR utility — used for fixed-point verification
# ---------------------------------------------------------------------------

def psnr(reference: np.ndarray, test: np.ndarray) -> float:
    """Peak signal-to-noise ratio between two float64 field arrays (dB).

    Uses the reference signal peak as the 'max signal' value.
    Acceptance criterion from brief: PSNR > 40 dB.
    """
    peak  = np.max(np.abs(reference))
    if peak == 0:
        return float("inf")
    mse = np.mean((reference - test) ** 2)
    if mse == 0:
        return float("inf")
    return 20.0 * np.log10(peak / np.sqrt(mse))


# ---------------------------------------------------------------------------
# Canonical test scenes
# ---------------------------------------------------------------------------

def make_test_scenes(width: int = 200, height: int = 200, k_wave: float = 0.2):
    """Return a list of (name, antennas, output_mode) tuples.

    These are the four reference configurations for RTL verification.
    All antenna positions are in grid pixel coordinates (origin = top-left).
    """
    cx, cy = width / 2, height / 2

    scenes = [
        (
            "single_isotropic",
            [Antenna(x=cx, y=cy, amplitude=1.0, phase_rad=0.0,
                     pattern=RadiationPattern.ISOTROPIC)],
            OutputMode.E_SIGNED,
        ),
        (
            "two_source_fringes",
            [
                Antenna(x=cx - 30, y=cy, amplitude=1.0, phase_rad=0.0,
                        pattern=RadiationPattern.ISOTROPIC),
                Antenna(x=cx + 30, y=cy, amplitude=1.0, phase_rad=0.0,
                        pattern=RadiationPattern.ISOTROPIC),
            ],
            OutputMode.E_SIGNED,
        ),
        (
            "three_source_beamforming",
            [
                Antenna(x=cx - 40, y=cy, amplitude=1.0, phase_rad=0.0,
                        pattern=RadiationPattern.ISOTROPIC),
                Antenna(x=cx,      y=cy, amplitude=1.0, phase_rad=np.pi / 3,
                        pattern=RadiationPattern.ISOTROPIC),
                Antenna(x=cx + 40, y=cy, amplitude=1.0, phase_rad=2 * np.pi / 3,
                        pattern=RadiationPattern.ISOTROPIC),
            ],
            OutputMode.E_SIGNED,
        ),
        (
            "dipole_pattern",
            [
                Antenna(x=cx, y=cy, amplitude=1.0, phase_rad=0.0,
                        pattern=RadiationPattern.DIPOLE),
            ],
            OutputMode.E_SIGNED,
        ),
    ]
    return scenes


# ---------------------------------------------------------------------------
# Main — generate and save all canonical scenes
# ---------------------------------------------------------------------------

def main():
    WIDTH, HEIGHT = 200, 200
    K_WAVE        = 0.2      # λ ≈ 31 px
    OMEGA_T       = 0.0      # static snapshot at t=0

    renderer = EMFieldRenderer(
        width=WIDTH, height=HEIGHT,
        k_wave=K_WAVE, omega_t=OMEGA_T,
    )

    scenes = make_test_scenes(WIDTH, HEIGHT, K_WAVE)
    out_dir = Path("golden_outputs")
    out_dir.mkdir(exist_ok=True)

    fig, axes = plt.subplots(1, len(scenes), figsize=(4 * len(scenes), 4))

    for ax, (name, antennas, mode) in zip(axes, scenes):
        field = renderer.render(antennas, output_mode=mode)
        rgb   = tonemap_rdbu(field)

        # Save raw float64 array for RTL comparison
        np.save(out_dir / f"{name}.npy", field)

        # Save PNG for visual inspection
        plt.imsave(str(out_dir / f"{name}.png"), rgb)

        ax.imshow(rgb)
        ax.set_title(name.replace("_", "\n"), fontsize=9)
        ax.axis("off")

        print(f"[{name}]  field range: [{field.min():.4f}, {field.max():.4f}]")

    plt.tight_layout()
    plt.savefig(str(out_dir / "all_scenes.png"), dpi=150)
    plt.close()
    print(f"\nOutputs written to {out_dir.resolve()}/")
    print("  *.npy  — float64 field arrays (use these for PSNR comparison)")
    print("  *.png  — RGB images for visual verification")
    print("  all_scenes.png — side-by-side overview")


if __name__ == "__main__":
    main()