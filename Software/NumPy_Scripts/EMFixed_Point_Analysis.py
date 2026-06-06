"""
Fixed-point emulation — NumPy PSNR verification
================================================
Emulates the word lengths from the project brief (§6) so you can confirm
PSNR > 40 dB before writing a single line of RTL.

Fixed-point format notation: Qm.f
    m = integer bits (signed, two's complement, includes sign bit)
    f = fractional bits
    total width = m + f bits

Corrected format table (deviations from brief noted with *):
    Variable        Range               Format      Total bits
    x, y, Δx, Δy   −256 … +256         Q9.7        16 b
    r²              0 … 2^17            Q18.14      32 b
    r               0 … ~360            Q9.7        16 b
    θ               −π … +π             Q4.12       16 b      * Q3.13 in brief
    k·r (pre-mod)   0 … k·r_max≈57      Q7.12       19 b      * not in brief
    phase (post-mod) 0 … 2π             Q4.12       16 b      * Q3.13 in brief
    cos(·), sin(·)  −1 … +1             Q1.15       16 b
    1/r             ~0.003 … 1          Q1.15       16 b
    accumulator     −N … +N             Q5.18       24 b

NOTE on phase quantisation (important for RTL design):
    The brief suggests Q3.13 for the phase register.  Q3 signed gives a max
    value of ~4.0, but values after (mod 2π) reach 2π ≈ 6.28, causing
    overflow and corruption of cos(·).  The minimum correct format is Q4.12
    (same 16-bit total).  This was confirmed by PSNR measurement.

NOTE on k·r quantisation:
    k·r_max = 0.2 × 283 ≈ 57 rad, which requires at least 7 integer bits
    BEFORE the mod 2π reduction.  Use a Q7.12 (19-bit) intermediate, then
    reduce mod 2π and re-quantise to Q4.12 for the cos LUT address.
    This intermediate does not need to be stored — it is a pipeline wire.

Usage
-----
    python em_fixed_point.py           # runs PSNR check, prints table
    python em_fixed_point.py --plot    # also saves error maps as PNGs

Import for interactive exploration:
    from em_fixed_point import FixedPointEMRenderer, run_psnr_check
"""

import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from pathlib import Path

from EMGoldenModel import (
    EMFieldRenderer, Antenna, RadiationPattern, OutputMode,
    make_test_scenes, psnr, tonemap_rdbu,
)


# ---------------------------------------------------------------------------
# Fixed-point quantisation helper
# ---------------------------------------------------------------------------

def quantise(x: np.ndarray, int_bits: int, frac_bits: int,
             signed: bool = True) -> np.ndarray:
    """Quantise a float64 array to a Qm.f fixed-point representation.

    Parameters
    ----------
    x         : input float64 array (any shape)
    int_bits  : integer bits (includes sign bit when signed=True)
    frac_bits : fractional bits
    signed    : True for two's-complement signed, False for unsigned

    Returns float64 array quantised to multiples of 2^{-frac_bits},
    saturated at the representable range.
    """
    scale = 2.0 ** frac_bits
    if signed:
        lo = -(2 ** (int_bits - 1))
        hi =  (2 ** (int_bits - 1)) - 2.0 ** (-frac_bits)
    else:
        lo = 0.0
        hi = (2 ** int_bits) - 2.0 ** (-frac_bits)
    return np.clip(np.round(x * scale) / scale, lo, hi)


# ---------------------------------------------------------------------------
# Fixed-point renderer
# ---------------------------------------------------------------------------

class FixedPointEMRenderer:
    """
    Emulates the §6 fixed-point PE pipeline in NumPy.

    Each arithmetic stage is quantised to the word length given in the
    format parameters.  Edit these and re-run run_psnr_check() to explore
    trade-offs before committing to RTL.

    Key corrections vs. the project brief
    --------------------------------------
    phase_int / phase_frac    Must be Q4.12, NOT Q3.13.
                              Q3 signed max ≈ 4.0 < 2π ≈ 6.28 → overflow.

    kr_int / kr_frac          New intermediate: k·r before mod 2π.
                              Needs Q7.12 to hold values up to ~57 rad.
                              This is a pipeline wire, not a stored register.
    """

    def __init__(
        self,
        width:   int   = 200,
        height:  int   = 200,
        k_wave:  float = 0.2,
        omega_t: float = 0.0,
        r_clamp: float = 1.0,
        # ---- coordinate / geometry formats (§6 brief values) ----------
        xy_int:     int = 9,    xy_frac:    int = 7,
        r2_int:     int = 18,   r2_frac:    int = 14,
        r_int:      int = 9,    r_frac:     int = 7,
        theta_int:  int = 4,    theta_frac: int = 12,   # corrected: was Q3.13
        # ---- phase path formats ---------------------------------------
        kr_int:     int = 7,    kr_frac:    int = 12,   # k*r before mod 2pi
        phase_int:  int = 4,    phase_frac: int = 12,   # corrected: was Q3.13
        # ---- output stage formats ------------------------------------
        trig_int:   int = 1,    trig_frac:  int = 15,
        inv_r_int:  int = 1,    inv_r_frac: int = 15,
        acc_int:    int = 5,    acc_frac:   int = 18,
    ):
        self.width   = width
        self.height  = height
        self.k_wave  = k_wave
        self.omega_t = omega_t
        self.r_clamp = r_clamp

        self.fmt_xy    = (xy_int,    xy_frac)
        self.fmt_r2    = (r2_int,    r2_frac)
        self.fmt_r     = (r_int,     r_frac)
        self.fmt_theta = (theta_int, theta_frac)
        self.fmt_kr    = (kr_int,    kr_frac)
        self.fmt_phase = (phase_int, phase_frac)
        self.fmt_trig  = (trig_int,  trig_frac)
        self.fmt_inv_r = (inv_r_int, inv_r_frac)
        self.fmt_acc   = (acc_int,   acc_frac)

        # Pre-compute pixel grids
        cx, cy = width / 2.0, height / 2.0
        px = np.arange(width,  dtype=np.float64) - cx
        py = np.arange(height, dtype=np.float64) - cy
        raw_px, raw_py = np.meshgrid(px, py)
        self.px_grid = quantise(raw_px, *self.fmt_xy)
        self.py_grid = quantise(raw_py, *self.fmt_xy)

    def _antenna_field_fp(self, ant: Antenna) -> np.ndarray:
        """Emulate the per-PE fixed-point datapath for one antenna.

        Datapath stages (each output quantised):
            1.  Δx, Δy  — subtract antenna position from pixel grid
            2.  r²      — two DSP multiplies and add
            3.  r, θ    — CORDIC (emulated by sqrt / atan2)
            4.  k·r     — DSP multiply (pre-mod intermediate)
            5.  phase   — (ωt − k·r + φ) mod 2π, re-quantised
            6.  cos(·)  — CORDIC / LUT (emulated by np.cos)
            7.  1/r     — LUT
            8.  D(θ)    — radiation-pattern LUT
            9.  product — A · D · (1/r) · cos
            10. accum   — add to running sum
        """
        # Stage 1 — geometry
        ax_q = quantise(np.array(ant.x - self.width  / 2.0), *self.fmt_xy)
        ay_q = quantise(np.array(ant.y - self.height / 2.0), *self.fmt_xy)
        dx   = quantise(self.px_grid - ax_q, *self.fmt_xy)
        dy   = quantise(self.py_grid - ay_q, *self.fmt_xy)

        # Stage 2 — r²
        r2 = quantise(dx**2 + dy**2, *self.fmt_r2)

        # Stage 3 — r and θ (CORDIC)
        r_fp  = quantise(np.sqrt(r2.clip(0)), *self.fmt_r)
        r_fp  = np.maximum(r_fp, self.r_clamp)
        theta = quantise(np.arctan2(dy, dx), *self.fmt_theta)

        # Stage 4 — k·r (pre-mod; needs Q7.12 to hold up to ~57 rad)
        kr = quantise(self.k_wave * r_fp, *self.fmt_kr)

        # Stage 5 — phase mod 2π
        raw_phase = self.omega_t - kr + ant.phase_rad
        phi_mod   = raw_phase % (2.0 * np.pi)          # float64 mod, exact
        phi_arg   = quantise(phi_mod, *self.fmt_phase)  # re-quantise to Q4.12

        # Stage 6 — cos (CORDIC / LUT)
        cos_val = quantise(np.cos(phi_arg), *self.fmt_trig)

        # Stage 7 — 1/r (LUT)
        inv_r = quantise(1.0 / r_fp, *self.fmt_inv_r)

        # Stage 8 — radiation pattern D(θ)
        if ant.pattern == RadiationPattern.ISOTROPIC:
            D = np.ones_like(theta)
        else:  # DIPOLE
            D = quantise(np.abs(np.sin(theta)), *self.fmt_trig)

        # Stage 9 — combine: A · D · (1/r) · cos
        amp_q   = quantise(np.array(ant.amplitude), *self.fmt_trig)
        product = quantise(amp_q * D * inv_r * cos_val, *self.fmt_trig)

        # Stage 10 — accumulate
        return quantise(product, *self.fmt_acc)

    def render(self, antennas: list) -> np.ndarray:
        """Render one frame. Returns float64 field array of shape (H, W)."""
        field = np.zeros((self.height, self.width), dtype=np.float64)
        for ant in antennas:
            contrib = self._antenna_field_fp(ant)
            field   = quantise(field + contrib, *self.fmt_acc)
        return field


# ---------------------------------------------------------------------------
# PSNR verification
# ---------------------------------------------------------------------------

def run_psnr_check(
    width:   int   = 200,
    height:  int   = 200,
    k_wave:  float = 0.2,
    save_plots: bool = False,
) -> bool:
    """Compare fixed-point output against float64 golden model.

    Prints a PSNR table.  Target: all scenes ≥ 40 dB.
    Returns True if all scenes pass.
    """
    float_r = EMFieldRenderer(width=width, height=height,
                               k_wave=k_wave, omega_t=0.0)
    fp_r    = FixedPointEMRenderer(width=width, height=height,
                                   k_wave=k_wave, omega_t=0.0)

    scenes = make_test_scenes(width, height, k_wave)

    print()
    print("=" * 62)
    print("Fixed-point emulation — PSNR vs float64 golden model")
    print("=" * 62)
    print(f"  Grid: {width}×{height}   k_wave: {k_wave}   (λ ≈ {2*np.pi/k_wave:.1f} px)")
    print()
    print(f"  {'Scene':<32}  {'PSNR (dB)':>10}  {'Pass?':>6}")
    print("  " + "-" * 54)

    all_pass = True
    results  = []
    for name, antennas, mode in scenes:
        ref    = float_r.render(antennas, output_mode=OutputMode.E_SIGNED)
        test   = fp_r.render(antennas)
        score  = psnr(ref, test)
        passed = score >= 40.0
        all_pass = all_pass and passed
        results.append((name, antennas, ref, test, score, passed))
        flag = "✓" if passed else "✗ FAIL"
        print(f"  {name:<32}  {score:>10.1f}  {flag:>6}")

    print("  " + "-" * 54)
    print()
    if all_pass:
        print("  ✓ All scenes PASS (PSNR ≥ 40 dB) — word lengths are adequate.")
    else:
        print("  Some scenes FAILED.  Widen the failing formats and re-run.")
    print("=" * 62)
    print()

    if save_plots:
        _save_error_maps(results)

    return all_pass


def _save_error_maps(results):
    """Save side-by-side (reference | fixed-point | error) images."""
    out_dir = Path("golden_outputs")
    out_dir.mkdir(exist_ok=True)

    n = len(results)
    fig, axes = plt.subplots(n, 3, figsize=(10, 3.2 * n))

    for row, (name, ants, ref, test, score, passed) in enumerate(results):
        err = ref - test
        vmax = np.percentile(np.abs(ref), 99) or 1.0
        norm = mcolors.Normalize(vmin=-vmax, vmax=vmax)
        cmap = plt.get_cmap("RdBu")

        axes[row, 0].imshow(cmap(norm(ref))[:, :, :3])
        axes[row, 0].set_title(f"{name}\n(float64)", fontsize=8)

        axes[row, 1].imshow(cmap(norm(test))[:, :, :3])
        status = "✓" if passed else "✗"
        axes[row, 1].set_title(f"Fixed-point\nPSNR={score:.1f}dB {status}", fontsize=8)

        err_norm = mcolors.Normalize(
            vmin=-np.percentile(np.abs(err), 99),
            vmax= np.percentile(np.abs(err), 99),
        )
        axes[row, 2].imshow(cmap(err_norm(err))[:, :, :3])
        axes[row, 2].set_title("Error (×1)", fontsize=8)

        for ax in axes[row]:
            ax.axis("off")

    plt.tight_layout()
    path = out_dir / "fp_verification.png"
    plt.savefig(str(path), dpi=150)
    plt.close()
    print(f"  Error maps saved to {path}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    save_plots = "--plot" in sys.argv
    run_psnr_check(save_plots=save_plots)