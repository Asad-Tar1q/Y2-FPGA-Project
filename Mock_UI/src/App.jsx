import React, { useState, useRef, useEffect, useCallback } from "react";
import AntennaCanvas from "./AntennaCanvas.jsx";
import Sidebar from "./Sidebar.jsx";
import Hud from "./Hud.jsx";
import { useWebSocket } from "./useWebSocket.js";
import { defaultGlobals } from "./protocol.js";

// Server URL. Defaults to the same host the page was served from,
// on port 8765. Override with ?ws=ws://other.host:8765 if needed.
function defaultWsUrl() {
  const params = new URLSearchParams(window.location.search);
  if (params.has("ws")) return params.get("ws");
  const host = window.location.hostname || "localhost";
  return `ws://${host}:8765/`;
}

export default function App() {
  const [globals, setGlobals] = useState(defaultGlobals());
  const [antennas, setAntennas] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [telemetry, setTelemetry] = useState(null);

  const seqRef = useRef(0);
  const sendThrottleRef = useRef({ last: 0, pending: null });

  const onMessage = useCallback((msg) => {
    switch (msg.type) {
      case "hello":
        // Adopt server's view of the world.
        if (msg.state) {
          if (msg.state.globals) setGlobals(msg.state.globals);
          if (msg.state.antennas) setAntennas(msg.state.antennas);
        }
        break;
      case "telemetry":
        setTelemetry(msg);
        break;
      case "state_ack":
        // Could track pending seqs here for a "saved" indicator.
        break;
      case "state_error":
        console.warn("state_error:", msg.reason);
        break;
      default:
        break;
    }
  }, []);

  const { status, send } = useWebSocket(defaultWsUrl(), onMessage);

  // Throttle outbound state updates to ~30 Hz, always send the final value.
  const sendState = useCallback((nextGlobals, nextAntennas) => {
    const payload = {
      type: "state_update",
      seq: ++seqRef.current,
      globals: nextGlobals,
      antennas: nextAntennas,
    };
    const now = performance.now();
    const since = now - sendThrottleRef.current.last;
    if (since >= 33) {
      send(payload);
      sendThrottleRef.current.last = now;
      sendThrottleRef.current.pending = null;
    } else {
      sendThrottleRef.current.pending = payload;
      setTimeout(() => {
        const p = sendThrottleRef.current.pending;
        if (p && performance.now() - sendThrottleRef.current.last >= 33) {
          send(p);
          sendThrottleRef.current.last = performance.now();
          sendThrottleRef.current.pending = null;
        }
      }, 33 - since);
    }
  }, [send]);

  // Send whenever globals or antennas change AND we're connected.
  useEffect(() => {
    if (status !== "open") return;
    sendState(globals, antennas);
  }, [globals, antennas, status, sendState]);

  // ----- antenna manipulation -----

  const onAdd     = a => setAntennas(list => [...list, a]);
  const onMove    = (id, x, y) =>
    setAntennas(list => list.map(a => a.id === id ? { ...a, x, y } : a));
  const onDelete  = id => {
    setAntennas(list => list.filter(a => a.id !== id));
    if (selectedId === id) setSelectedId(null);
  };
  const onAntennaChange = (id, patch) =>
    setAntennas(list => list.map(a => a.id === id ? { ...a, ...patch } : a));

  // ----- commands (server-side actions) -----

  const onCommand = (msg) => {
    send(msg);
    // Optimistic local mirror for the simple cases.
    if (msg.action === "reset" || msg.action === "clear_antennas") {
      setAntennas([]); setSelectedId(null);
    }
    // For load_preset we just wait — the next hello/state_ack cycle isn't
    // implemented yet, so as a stopgap we mirror locally too.
    if (msg.action === "load_preset") {
      const PRESETS_LOCAL = {
        two_slit: [
          { id: 0, x: 80,  y: 100, amplitude: 1.0, phase: 0.0, pattern: 0 },
          { id: 1, x: 120, y: 100, amplitude: 1.0, phase: 0.0, pattern: 0 },
        ],
        phased_array: Array.from({ length: 4 }, (_, i) => ({
          id: i, x: 60 + i * 20, y: 100, amplitude: 1.0,
          phase: i * Math.PI / 4, pattern: 0,
        })),
        dipole_pair: [
          { id: 0, x: 90,  y: 100, amplitude: 1.0, phase: 0.0,        pattern: 1 },
          { id: 1, x: 110, y: 100, amplitude: 1.0, phase: Math.PI,    pattern: 1 },
        ],
      };
      if (PRESETS_LOCAL[msg.preset]) {
        setAntennas(PRESETS_LOCAL[msg.preset]);
        setSelectedId(null);
      }
    }
  };

  return (
    <div style={{ display: "flex", height: "100%", width: "100%" }}>
      <div style={{ position: "relative", flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <Hud telemetry={telemetry} globals={globals} antennaCount={antennas.length} />
        <AntennaCanvas
          antennas={antennas}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onAdd={onAdd}
          onMove={onMove}
          onDelete={onDelete}
        />
      </div>
      <Sidebar
        globals={globals}
        onGlobalsChange={setGlobals}
        antennas={antennas}
        selectedId={selectedId}
        onSelect={setSelectedId}
        onAntennaChange={onAntennaChange}
        onDelete={onDelete}
        onCommand={onCommand}
        status={status}
      />
    </div>
  );
}
