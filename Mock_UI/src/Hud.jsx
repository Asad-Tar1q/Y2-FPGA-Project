import React from "react";
import { OUT_MODES, wavelength } from "./protocol.js";

export default function Hud({ telemetry, globals, antennaCount }) {
  const lambda_cm = (wavelength(globals.frequency_hz) * 100).toFixed(2);
  return (
    <div style={{
      position: "absolute", top: 16, left: 16,
      background: "rgba(8,12,18,0.75)",
      border: "1px solid var(--panel-edge)",
      borderRadius: 8,
      padding: "8px 12px",
      fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
      fontSize: 12,
      lineHeight: 1.6,
      pointerEvents: "none",
      backdropFilter: "blur(4px)",
    }}>
      <div><b>f</b>     {(globals.frequency_hz / 1e9).toFixed(3)} GHz</div>
      <div><b>λ</b>     {lambda_cm} cm</div>
      <div><b>mode</b>  {OUT_MODES[globals.out_mode].name}</div>
      <div><b>N</b>     {antennaCount}</div>
      <div style={{ opacity: 0.6, marginTop: 4 }}>
        ωt {telemetry?.omega_t?.toFixed(3) ?? "—"}
      </div>
      <div style={{ marginTop: 4, color: "var(--accent)" }}>
        <b>fps</b>   {telemetry?.fps?.toFixed(1) ?? "—"}
      </div>
      <div style={{ color: "var(--muted)" }}>
        CPU  {telemetry?.cpu_baseline_fps?.toFixed(1) ?? "—"}
        &nbsp;→&nbsp; {telemetry?.ratio?.toFixed(1) ?? "—"}×
      </div>
    </div>
  );
}
