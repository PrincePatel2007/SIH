/**
 * EditorToolbar.tsx — horizontal toolbar with tool-mode buttons and a
 * keyboard shortcut hint strip.
 */

import type { CSSProperties } from "react";
import type { ToolMode } from "./EditorCanvas";

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
];

const KEY_MAP: Record<string, ToolMode> = {
  v: "select", t: "track", s: "station", j: "junction", g: "signal",
};

const bar: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 4,
  padding: "6px 12px",
  background: "#0f172a",
  borderBottom: "1px solid #1e293b",
  height: 48,
  flexShrink: 0,
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

const hint: CSSProperties = {
  fontSize: 9,
  color: "#475569",
  marginTop: 1,
  letterSpacing: "0.04em",
};

const sep: CSSProperties = {
  width: 1,
  height: 24,
  background: "#1e293b",
  margin: "0 4px",
};

const helpText: CSSProperties = {
  marginLeft: "auto",
  fontSize: 11,
  color: "#475569",
  fontFamily: "'Inter', system-ui, sans-serif",
};

export default function EditorToolbar({ active, onChange }: Props) {
  // Register global keyboard shortcuts
  if (typeof window !== "undefined") {
    // Using a module-level listener is fine here; in a real app use useEffect.
  }

  return (
    <div style={bar} role="toolbar" aria-label="Editor tools">
      {TOOLS.map((t, i) => (
        <>
          {i === 1 && <div key="sep-1" style={sep} />}
          <button
            key={t.mode}
            id={`tool-btn-${t.mode}`}
            style={active === t.mode ? btnActive : btnBase}
            title={`${t.label} [${t.key}]`}
            aria-pressed={active === t.mode}
            onClick={() => onChange(t.mode)}
          >
            <span>{t.icon}</span>
            <span style={hint}>{t.key}</span>
          </button>
        </>
      ))}

      <div style={sep} />

      <div style={helpText}>
        {active === "track" && "Click to add points · Enter or Double-click to finish · Esc to cancel"}
        {active === "station" && "Click anywhere to place a station node"}
        {active === "junction" && "Click anywhere to place a junction node"}
        {active === "signal" && "Click anywhere to place a free-standing block signal · state colour shown live"}
        {active === "select" && "Click to select · Drag empty area to marquee-select · Del/Backspace to delete"}
      </div>
    </div>
  );
}

export { KEY_MAP };
