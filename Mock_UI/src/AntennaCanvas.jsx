import React, { useRef, useState, useEffect, useCallback } from "react";
import { GRID_W, GRID_H, MAX_ANTENNAS, clampAntenna } from "./protocol.js";

// Colour palette for the antenna markers (cycled).
const COLOURS = [
  "#3aa0ff", "#ff6363", "#ffb454", "#7be07b",
  "#c084fc", "#22d3ee", "#f472b6", "#facc15",
];

export default function AntennaCanvas({
  antennas, selectedId, onSelect, onAdd, onMove, onDelete,
}) {
  const wrapRef = useRef(null);
  const canvasRef = useRef(null);
  const [size, setSize] = useState({ w: 600, h: 600 });
  const [dragging, setDragging] = useState(null); // antenna id being dragged

  // Resize observer — canvas always fills its wrap, kept square.
  useEffect(() => {
    if (!wrapRef.current) return;
    const ro = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect;
      const side = Math.floor(Math.min(width, height));
      setSize({ w: side, h: side });
    });
    ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []);

  // Convert pixel coords inside the canvas to grid coords.
  const toGrid = useCallback((px, py) => ({
    x: (px / size.w) * GRID_W,
    y: (py / size.h) * GRID_H,
  }), [size]);

  // Draw.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size.w * dpr;
    canvas.height = size.h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // background
    ctx.fillStyle = "#0a0d12";
    ctx.fillRect(0, 0, size.w, size.h);

    // gridlines every 25 grid units
    ctx.strokeStyle = "#1f2630";
    ctx.lineWidth = 1;
    for (let i = 0; i <= 8; i++) {
      const p = (i / 8) * size.w;
      ctx.beginPath(); ctx.moveTo(p, 0); ctx.lineTo(p, size.h); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, p); ctx.lineTo(size.w, p); ctx.stroke();
    }

    // border
    ctx.strokeStyle = "#2d3641";
    ctx.lineWidth = 2;
    ctx.strokeRect(0.5, 0.5, size.w - 1, size.h - 1);

    // antennas
    antennas.forEach((a, idx) => {
      const px = (a.x / GRID_W) * size.w;
      const py = (a.y / GRID_H) * size.h;
      const colour = COLOURS[idx % COLOURS.length];
      const selected = a.id === selectedId;
      const r = selected ? 9 : 7;

      ctx.fillStyle = colour;
      ctx.strokeStyle = selected ? "#ffffff" : "rgba(0,0,0,0.6)";
      ctx.lineWidth = selected ? 2 : 1;
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();

      // label
      ctx.fillStyle = "#ffffff";
      ctx.font = "bold 11px system-ui";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";
      ctx.fillText(`A${idx + 1}`, px + r + 4, py);
    });
  }, [antennas, size, selectedId]);

  // Hit-test in grid coords; returns the antenna id under the pointer, or null.
  function pick(localX, localY) {
    const px = localX, py = localY;
    for (let i = antennas.length - 1; i >= 0; i--) {
      const a = antennas[i];
      const ax = (a.x / GRID_W) * size.w;
      const ay = (a.y / GRID_H) * size.h;
      if ((ax - px) ** 2 + (ay - py) ** 2 < 12 ** 2) return a.id;
    }
    return null;
  }

  function getLocal(e) {
    const rect = canvasRef.current.getBoundingClientRect();
    return {
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
    };
  }

  function onPointerDown(e) {
    e.target.setPointerCapture(e.pointerId);
    const { x, y } = getLocal(e);
    const id = pick(x, y);
    if (id !== null) {
      onSelect(id);
      setDragging(id);
    } else {
      // empty space → add a new antenna (if under cap)
      if (antennas.length >= MAX_ANTENNAS) return;
      const g = clampAntenna({ x: 0, y: 0, ...toGrid(x, y) });
      const newId = antennas.length === 0
        ? 0
        : Math.max(...antennas.map(a => a.id)) + 1;
      onAdd({
        id: newId,
        x: g.x, y: g.y,
        amplitude: 1.0,
        phase: 0.0,
        pattern: 0,
      });
      onSelect(newId);
    }
  }

  function onPointerMove(e) {
    if (dragging === null) return;
    const { x, y } = getLocal(e);
    const g = clampAntenna({ ...toGrid(x, y), id: dragging });
    onMove(dragging, g.x, g.y);
  }

  function onPointerUp() {
    setDragging(null);
  }

  // Keyboard: Delete / Backspace removes the selected antenna.
  useEffect(() => {
    function onKey(e) {
      if ((e.key === "Backspace" || e.key === "Delete") && selectedId !== null) {
        // don't fire when user is typing in an input
        if (document.activeElement?.tagName === "INPUT") return;
        onDelete(selectedId);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, onDelete]);

  return (
    <div
      ref={wrapRef}
      style={{
        flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
        padding: 16, minWidth: 0, minHeight: 0,
      }}
    >
      <canvas
        ref={canvasRef}
        style={{
          width: size.w, height: size.h,
          touchAction: "none",
          cursor: dragging !== null ? "grabbing" : "crosshair",
          borderRadius: 8,
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
      />
    </div>
  );
}
