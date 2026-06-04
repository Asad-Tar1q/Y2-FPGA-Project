import React from "react";
import { PATTERNS, OUT_MODES, PRESETS, wavelength } from "./protocol.js";

function Row({ label, children }) {
  return (
    <div style={{ display: "grid", gridTemplateColumns: "110px 1fr 60px", gap: 8, alignItems: "center", marginBottom: 8 }}>
      <label style={{ color: "var(--muted)" }}>{label}</label>
      {children}
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div style={{
      background: "var(--panel)",
      border: "1px solid var(--panel-edge)",
      borderRadius: 8,
      padding: 12,
      marginBottom: 12,
    }}>
      <div style={{ fontWeight: 600, marginBottom: 8 }}>{title}</div>
      {children}
    </div>
  );
}

export default function Sidebar({
  globals, onGlobalsChange,
  antennas, selectedId, onSelect, onAntennaChange, onDelete,
  onCommand,
  status,
}) {
  const selected = antennas.find(a => a.id === selectedId) || null;
  const lambda_m = wavelength(globals.frequency_hz);

  return (
    <div style={{
      width: 320,
      padding: 12,
      borderLeft: "1px solid var(--panel-edge)",
      overflowY: "auto",
      background: "#0c1015",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
        <div style={{ fontWeight: 700, fontSize: 16 }}>Controls</div>
        <ConnStatus status={status} />
      </div>

      <Section title="Globals">
        <Row label="Frequency">
          <input
            type="range" min={1e8} max={6e9} step={1e7}
            value={globals.frequency_hz}
            onChange={e => onGlobalsChange({ ...globals, frequency_hz: parseFloat(e.target.value) })}
          />
          <span style={{ textAlign: "right" }}>{(globals.frequency_hz / 1e9).toFixed(2)} GHz</span>
        </Row>
        <Row label="λ">
          <div style={{ color: "var(--muted)" }}>derived</div>
          <span style={{ textAlign: "right" }}>{(lambda_m * 100).toFixed(2)} cm</span>
        </Row>
        <Row label="Time rate">
          <input
            type="range" min={0} max={3} step={0.05}
            value={globals.time_rate}
            onChange={e => onGlobalsChange({ ...globals, time_rate: parseFloat(e.target.value) })}
          />
          <span style={{ textAlign: "right" }}>{globals.time_rate.toFixed(2)}×</span>
        </Row>
        <Row label="Mode">
          <select
            value={globals.out_mode}
            onChange={e => onGlobalsChange({ ...globals, out_mode: parseInt(e.target.value) })}
            style={{ width: "100%", background: "var(--panel)", color: "inherit", border: "1px solid var(--panel-edge)", borderRadius: 4, padding: 4 }}
          >
            {OUT_MODES.map(m => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
          <span />
        </Row>
        <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
          <button onClick={() => onGlobalsChange({ ...globals, paused: !globals.paused })}>
            {globals.paused ? "▶ Play" : "❚❚ Pause"}
          </button>
          <button onClick={() => onCommand({ type: "command", action: "reset" })}>Reset</button>
        </div>
      </Section>

      <Section title="Antennas">
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 8 }}>
          {antennas.length === 0 && (
            <div style={{ color: "var(--muted)", fontSize: 12 }}>
              Click empty space on the canvas to add an antenna.
            </div>
          )}
          {antennas.map((a, idx) => (
            <button
              key={a.id}
              onClick={() => onSelect(a.id)}
              style={{
                textAlign: "left",
                borderColor: a.id === selectedId ? "var(--accent)" : "var(--panel-edge)",
                background: a.id === selectedId ? "#162536" : "var(--panel)",
              }}
            >
              <b>A{idx + 1}</b> &nbsp; ({a.x.toFixed(1)}, {a.y.toFixed(1)}) &nbsp;
              <span style={{ color: "var(--muted)" }}>
                A={a.amplitude.toFixed(2)}, φ={a.phase.toFixed(2)}, {PATTERNS[a.pattern].name}
              </span>
            </button>
          ))}
        </div>
        <button
          onClick={() => onCommand({ type: "command", action: "clear_antennas" })}
          disabled={antennas.length === 0}
        >Clear all</button>
      </Section>

      {selected && (
        <Section title={`Selected: A${antennas.findIndex(a => a.id === selectedId) + 1}`}>
          <Row label="Amplitude">
            <input
              type="range" min={0} max={2} step={0.01}
              value={selected.amplitude}
              onChange={e => onAntennaChange(selected.id, { amplitude: parseFloat(e.target.value) })}
            />
            <span style={{ textAlign: "right" }}>{selected.amplitude.toFixed(2)}</span>
          </Row>
          <Row label="Phase">
            <input
              type="range" min={-Math.PI} max={Math.PI} step={0.01}
              value={selected.phase}
              onChange={e => onAntennaChange(selected.id, { phase: parseFloat(e.target.value) })}
            />
            <span style={{ textAlign: "right" }}>{selected.phase.toFixed(2)}</span>
          </Row>
          <Row label="Pattern">
            <select
              value={selected.pattern}
              onChange={e => onAntennaChange(selected.id, { pattern: parseInt(e.target.value) })}
              style={{ width: "100%", background: "var(--panel)", color: "inherit", border: "1px solid var(--panel-edge)", borderRadius: 4, padding: 4 }}
            >
              {PATTERNS.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            <span />
          </Row>
          <button onClick={() => onDelete(selected.id)} style={{ marginTop: 4 }}>Delete antenna</button>
        </Section>
      )}

      <Section title="Presets">
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {PRESETS.map(name => (
            <button
              key={name}
              onClick={() => onCommand({ type: "command", action: "load_preset", preset: name })}
            >{name.replace(/_/g, " ")}</button>
          ))}
        </div>
      </Section>
    </div>
  );
}

function ConnStatus({ status }) {
  const colour = status === "open" ? "#7be07b" : status === "connecting" ? "var(--warn)" : "var(--error)";
  const label = status === "open" ? "connected" : status;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, color: "var(--muted)" }}>
      <span style={{ width: 8, height: 8, borderRadius: 4, background: colour, display: "inline-block" }} />
      {label}
    </div>
  );
}
