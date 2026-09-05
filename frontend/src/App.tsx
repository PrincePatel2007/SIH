import { useEffect, useState } from "react";
import type React from "react";
import { checkHealth } from "./api";
import { EditorStoreProvider } from "./editor/store";
import EditorCanvas, { type ToolMode } from "./editor/EditorCanvas";
import EditorToolbar, { KEY_MAP } from "./editor/EditorToolbar";
import PropertyPanel from "./editor/PropertyPanel";
import SimulatorCanvas from "./simulator/SimulatorCanvas";
import "./App.css";

type ConnectionStatus = "checking" | "connected" | "error";
type AppMode = "editor" | "simulator";

export default function App() {
  const [status, setStatus]     = useState<ConnectionStatus>("checking");
  const [tool, setTool]         = useState<ToolMode>("select");
  const [mode, setMode]         = useState<AppMode>("editor");

  // simLayout   = text currently in the input box (updates every keystroke)
  // activeName  = the name actually given to SimulatorCanvas (only updates on commit)
  // A new WebSocket is opened only when activeName changes.
  const [simLayout,  setSimLayout]  = useState("my-layout");
  const [activeName, setActiveName] = useState("");

  // ── Backend health ───────────────────────────────────────────────────────
  useEffect(() => {
    checkHealth()
      .then(() => setStatus("connected"))
      .catch(() => setStatus("error"));
  }, []);

  // ── Editor keyboard shortcuts ────────────────────────────────────────────
  useEffect(() => {
    if (mode !== "editor") return;
    const onKey = (e: KeyboardEvent) => {
      const tag = (document.activeElement as HTMLElement | null)?.tagName ?? "";
      if (["INPUT", "SELECT", "TEXTAREA"].includes(tag)) return;
      const next = KEY_MAP[e.key.toLowerCase()];
      if (next) setTool(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode]);

  // ── Commit handler — opens simulator ─────────────────────────────────────
  function commitAndSimulate() {
    const name = simLayout.trim();
    if (!name) return;
    setActiveName(name);      // ← only here does the WS reconnect
    setMode("simulator");
  }

  // ── Derived ──────────────────────────────────────────────────────────────
  const statusLabel = {
    checking: "Backend: checking…",
    connected: "Backend: connected ✓",
    error: "Backend: unreachable",
  }[status];

  const statusClass = {
    checking: "indicator--checking",
    connected: "indicator--ok",
    error: "indicator--error",
  }[status];

  const btnSt = (active: boolean): React.CSSProperties => ({
    background: active ? "#4f46e5" : "transparent",
    border: "1px solid #334155",
    borderRadius: 6,
    color: "#e2e8f0",
    cursor: "pointer",
    fontSize: 12,
    fontWeight: 600,
    padding: "4px 12px",
    transition: "background 0.12s",
  });

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <EditorStoreProvider>
      <div className="app-shell">

        {/* ── Topbar ────────────────────────────────────────────────── */}
        <header className="topbar">
          <div className="topbar__brand">
            <span className="topbar__logo">🚆</span>
            <span className="topbar__title">TrainNet ETA Simulator</span>
          </div>

          {/* Layout name + simulate button */}
          <div style={{ display: "flex", gap: 4, alignItems: "center", marginLeft: 24 }}>
            <button id="mode-editor" onClick={() => setMode("editor")} style={btnSt(mode === "editor")}>
              ✏ Editor
            </button>

            {/* Typing here does NOT open a new WebSocket */}
            <input
              id="sim-layout-input"
              style={{
                background: "#1e293b",
                border: "1px solid #334155",
                borderRadius: 5,
                color: "#e2e8f0",
                fontSize: 12,
                padding: "4px 8px",
                width: 120,
                outline: "none",
              }}
              value={simLayout}
              onChange={(e) => setSimLayout(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") commitAndSimulate(); }}
              placeholder="layout name"
            />

            {/* ▶ Simulate — commits the name and opens WS */}
            <button
              id="btn-simulate"
              onClick={commitAndSimulate}
              style={{
                background: "#0f766e",
                border: "none",
                borderRadius: 5,
                color: "#fff",
                cursor: "pointer",
                fontSize: 12,
                fontWeight: 700,
                padding: "5px 12px",
                transition: "opacity 0.12s",
              }}
            >
              ▶ Simulate
            </button>
          </div>

          {/* Backend status badge */}
          <div className={`indicator ${statusClass}`} id="backend-status-indicator">
            <span className="indicator__dot" />
            <span className="indicator__label">{statusLabel}</span>
          </div>
        </header>

        {/* ── Editor mode ───────────────────────────────────────────── */}
        {mode === "editor" && (
          <>
            <EditorToolbar active={tool} onChange={setTool} />
            <div className="editor-layout">
              <main className="canvas-area" id="canvas-area">
                <EditorCanvas tool={tool} />
              </main>
              <PropertyPanel />
            </div>
          </>
        )}

        {/* ── Simulator mode ────────────────────────────────────────── */}
        {mode === "simulator" && activeName && (
          <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
            {/* key=activeName forces a fresh mount (new WS) when the name changes */}
            <SimulatorCanvas
              key={activeName}
              layoutName={activeName}
              onClose={() => setMode("editor")}
            />
          </div>
        )}

        {/* Prompt when simulator mode is selected but no name committed yet */}
        {mode === "simulator" && !activeName && (
          <div style={{
            flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
            color: "#475569", fontSize: 13, background: "#0f172a", flexDirection: "column", gap: 12,
          }}>
            <span>Enter a layout name and click <strong style={{ color: "#e2e8f0" }}>▶ Simulate</strong>.</span>
            <button onClick={commitAndSimulate} style={{
              background: "#4f46e5", border: "none", borderRadius: 6,
              color: "#fff", cursor: "pointer", fontSize: 13, fontWeight: 700, padding: "8px 20px",
            }}>
              ▶ Simulate "{simLayout}"
            </button>
          </div>
        )}

      </div>
    </EditorStoreProvider>
  );
}
