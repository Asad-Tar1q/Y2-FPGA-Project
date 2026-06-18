// Protocol v0.1 — see ../../protocol.md

export const GRID_W = 200;
export const GRID_H = 200;
export const MAX_ANTENNAS = 32;

export const PATTERNS = [
  { id: 0, name: "isotropic" },
  { id: 1, name: "dipole" },
  { id: 2, name: "array" },
];

export const OUT_MODES = [
  { id: 0, name: "E"    },
  { id: 1, name: "|E|"  },
  { id: 2, name: "|E|²" },
  { id: 3, name: "φ"    },
];

export const PRESETS = ["two_slit", "phased_array", "dipole_pair"];

export function defaultGlobals() {
  return {
    frequency_hz: 2.4e9,
    time_rate: 1.0,
    paused: false,
    out_mode: 0,
  };
}

export function clampAntenna(a) {
  return {
    ...a,
    x: Math.max(0, Math.min(GRID_W - 1, a.x)),
    y: Math.max(0, Math.min(GRID_H - 1, a.y)),
  };
}

// Wavelength in metres, given frequency in Hz.
export const C_LIGHT = 2.998e8;
export function wavelength(freqHz) {
  return C_LIGHT / freqHz;
}
