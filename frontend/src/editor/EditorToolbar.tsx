/**
 * EditorToolbar.tsx — horizontal toolbar with tool-mode buttons,
 * keyboard shortcut hints, and the Save/Load bar (Phase 7).
 */

import { useState, type CSSProperties } from "react";
import type { ToolMode } from "./EditorCanvas";
import { useEditorStore, type TrainWithPos, type StationWithPos, type JunctionWithPos, type SignalWithPos, type SegmentOverride } from "./store";
import type { Track, Segment } from "../types";
import { saveLayout, loadLayout, type SaveResult } from "../api";

interface Props {
  active: ToolMode;
  onChange: (t: ToolMode) => void;
}

const TOOLS: { mode: ToolMode; icon: string; label: string; key: string }[] = [
  { mode: "select",   icon: "↖",  label: "Select / Marquee", key: "V" },
  { mode: "track",    icon: "╱",  label: "Draw Track",       key: "T" },
  { mode: "station",  icon: "▣",  label: "Place Station",    key: "S" },
  { mode: "junction", icon: "⬡",  label: "Place Junction",   key: "J" },
  { mode: "signal",   icon: "●",  label: "Place Signal",     key: "G" },
  { mode: "train",    icon: "🚂", label: "Place Train",      key: "R" },
];

export const KEY_MAP: Record<string, ToolMode> = {
  v: "select", t: "track", s: "station", j: "junction", g: "signal", r: "train",
};

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------
const bar: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  background: "#0f172a",
  borderBottom: "1px solid #1e293b",
  flexShrink: 0,
};
const toolRow: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 4,
  padding: "6px 12px",
  height: 48,
};
const btnBase: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  justifyContent: "center",
  width: 52,
  height: 36,
  border: "1px solid transparent",
  borderRadius: 6,
  cursor: "pointer",
  fontSize: 16,
  fontFamily: "monospace",
  transition: "background 0.12s, border-color 0.12s",
  background: "transparent",
  color: "#94a3b8",
  position: "relative",
};
const btnActive: CSSProperties = {
  ...btnBase,
  background: "#1e293b",
  borderColor: "#6366f1",
  color: "#e2e8f0",
};
const hint: CSSProperties = { fontSize: 9, color: "#475569", marginTop: 1, letterSpacing: "0.04em" };
const sep: CSSProperties  = { width: 1, height: 24, background: "#1e293b", margin: "0 4px" };
const helpText: CSSProperties = {
  marginLeft: "auto",
  fontSize: 11,
  color: "#475569",
  fontFamily: "'Inter', system-ui, sans-serif",
};
const saveBar: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "4px 12px 6px",
  borderTop: "1px solid #1e293b",
};
const nameInput: CSSProperties = {
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: 5,
  color: "#e2e8f0",
  fontSize: 12,
  padding: "4px 8px",
  width: 160,
  outline: "none",
};
const actionBtn = (color: string): CSSProperties => ({
  background: color,
  border: "none",
  borderRadius: 5,
  color: "#fff",
  cursor: "pointer",
  fontSize: 12,
  fontWeight: 600,
  padding: "4px 12px",
  transition: "opacity 0.12s",
});
const statusText = (ok: boolean): CSSProperties => ({
  fontSize: 11,
  color: ok ? "#22c55e" : "#ef4444",
  marginLeft: 4,
  fontFamily: "'Inter', system-ui, sans-serif",
});

// ---------------------------------------------------------------------------
// SaveLoadBar sub-component
// ---------------------------------------------------------------------------

function SaveLoadBar() {
  const { state, dispatch } = useEditorStore();
  const [name,    setName]    = useState("my-layout");
  const [status,  setStatus]  = useState<{ ok: boolean; msg: string } | null>(null);
  const [loading, setLoading] = useState(false);

  /** Serialise the full canvas state into a LayoutPayload for the backend. */
  function buildPayload() {
    const station_positions: Record<string, { x: number; y: number }> = {};
    const junction_positions: Record<string, { x: number; y: number }> = {};
    const signal_positions: Record<string, { x: number; y: number }> = {};
    const train_positions: Record<string, { x: number; y: number }> = {};
    const authoring_hints: object[] = [];

    for (const s of state.stations) {
      const p = (s as StationWithPos)._pos;
      if (p) station_positions[s.id] = p;
    }
    for (const j of state.junctions) {
      const p = (j as JunctionWithPos)._pos;
      if (p) junction_positions[j.id] = p;
    }
    for (const sig of state.signals) {
      const p = (sig as SignalWithPos)._pos;
      if (p) signal_positions[sig.id] = p;
    }
    for (const t of state.trains) {
      const twp = t as TrainWithPos;
      if (twp._pos) train_positions[t.id] = twp._pos;
      authoring_hints.push({
        train_id: t.id,
        // Use the first scheduled_departure as origin departure if available.
        origin_departure_iso:
          t.route[0]
            ? (t.schedule[t.route[0].node_id]?.scheduled_departure ?? new Date().toISOString())
            : new Date().toISOString(),
        dwell_minutes: twp._dwell ?? {},
      });
    }

    // Strip _pos and _dwell from serialised trains.
    const trains = state.trains.map((t) => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { _pos, _dwell, ...rest } = t as TrainWithPos;
      return rest;
    });
    const stations = state.stations.map((s) => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { _pos, ...rest } = s as StationWithPos;
      return rest;
    });
    const junctions = state.junctions.map((j) => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { _pos, ...rest } = j as JunctionWithPos;
      return rest;
    });
    const signals = state.signals.map((s) => {
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { _pos, ...rest } = s as SignalWithPos;
      return rest;
    });

    return {
      tracks: state.tracks,
      segments: state.segments,
      stations,
      junctions,
      signals,
      trains,
      authoring_hints,
      segment_overrides: state.segmentOverrides,
      station_positions,
      junction_positions,
      signal_positions,
      train_positions,
    };
  }

  async function handleSave() {
    if (!name.trim()) { setStatus({ ok: false, msg: "Enter a layout name" }); return; }
    setLoading(true);
    setStatus(null);
    try {
      const result = await saveLayout(name.trim(), buildPayload()) as SaveResult;
      if (result.ok) {
        setStatus({ ok: true, msg: `✓ Saved as "${name}"` });
      } else {
        setStatus({ ok: false, msg: result.errors.join(" | ") });
      }
    } catch (err) {
      setStatus({ ok: false, msg: String(err) });
    }
    setLoading(false);
  }

  async function handleLoad() {
    if (!name.trim()) { setStatus({ ok: false, msg: "Enter a layout name" }); return; }
    setLoading(true);
    setStatus(null);
    try {
      const payload = await loadLayout(name.trim());
      if (!payload) {
        setStatus({ ok: false, msg: `Not found: "${name}"` });
      } else {
        dispatch({ type: "LOAD_LAYOUT", payload: payload as Parameters<typeof dispatch>[0] extends { type: "LOAD_LAYOUT"; payload: infer P } ? P : never });
        setStatus({ ok: true, msg: `✓ Loaded "${name}"` });
      }
    } catch (err) {
      setStatus({ ok: false, msg: String(err) });
    }
    setLoading(false);
  }

  return (
    <div style={saveBar}>
      <span style={{ fontSize: 11, color: "#475569", fontFamily: "'Inter', system-ui, sans-serif", marginRight: 4 }}>Layout:</span>
      <input
        id="layout-name-input"
        style={nameInput}
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="my-layout"
        onKeyDown={(e) => { if (e.key === "Enter") handleSave(); }}
      />
      <button id="btn-save" style={actionBtn("#4f46e5")} onClick={handleSave} disabled={loading}>
        {loading ? "…" : "Save"}
      </button>
      <button id="btn-load" style={actionBtn("#0f766e")} onClick={handleLoad} disabled={loading}>
        {loading ? "…" : "Load"}
      </button>
      {status && (
        <span style={statusText(status.ok)}>{status.msg}</span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main toolbar
// ---------------------------------------------------------------------------

export default function EditorToolbar({ active, onChange }: Props) {
  return (
    <div style={bar} role="toolbar" aria-label="Editor tools">
      <div style={toolRow}>
        {TOOLS.map((t, i) => (
          <span key={t.mode}>
            {i === 1 && <span style={sep} />}
            {i === 5 && <span style={sep} />}
            <button
              id={`tool-btn-${t.mode}`}
              style={active === t.mode ? btnActive : btnBase}
              title={`${t.label} [${t.key}]`}
              aria-pressed={active === t.mode}
              onClick={() => onChange(t.mode)}
            >
              <span>{t.icon}</span>
              <span style={hint}>{t.key}</span>
            </button>
          </span>
        ))}

        <div style={sep} />

        <div style={helpText}>
          {active === "track"    && "Click to add points · Enter or Double-click to finish · Esc to cancel"}
          {active === "station"  && "Click anywhere to place a station node"}
          {active === "junction" && "Click anywhere to place a junction node"}
          {active === "signal"   && "Click anywhere to place a block signal · state colour shown live"}
          {active === "train"    && "Click to place train · click stations/junctions to build route · Enter to commit · Esc to cancel"}
          {active === "select"   && "Click to select · Drag empty area to marquee-select · Del/Backspace to delete"}
        </div>
      </div>

      <SaveLoadBar />
    </div>
  );
}
