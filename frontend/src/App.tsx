import { useEffect, useMemo, useState } from "react";
import type React from "react";
import { checkHealth } from "./api";
import { EditorStoreProvider } from "./editor/store";
import EditorCanvas, { type ToolMode } from "./editor/EditorCanvas";
import EditorToolbar, { KEY_MAP } from "./editor/EditorToolbar";
import PropertyPanel from "./editor/PropertyPanel";
import SimulatorCanvas from "./simulator/SimulatorCanvas";
import { ThemeContext, DARK_THEME, LIGHT_THEME } from "./theme";
import "./App.css";

type ConnectionStatus = "checking" | "connected" | "error";
type AppMode = "editor" | "simulator";

export default function App() {
  const [status, setStatus]         = useState<ConnectionStatus>("checking");
  const [tool, setTool]             = useState<ToolMode>("select");
  const [mode, setMode]             = useState<AppMode>("editor");
  const [themeMode, setThemeMode]   = useState<"dark" | "light">("dark");

  // simLayout   = text currently in the input box (updates every keystroke)
  // activeName  = the name actually given to SimulatorCanvas (only updates on commit)
  const [simLayout,  setSimLayout]  = useState("my-layout");
  const [activeName, setActiveName] = useState("");

  const theme = themeMode === "dark" ? DARK_THEME : LIGHT_THEME;
  const themeCtx = useMemo(() => ({
    theme,
    toggle: () => setThemeMode((m) => m === "dark" ? "light" : "dark"),
  }), [theme]);

  // Apply background colour to <body> so the full page matches
  useEffect(() => {
    document.body.style.background = theme.bgCanvas;
    document.body.style.color      = theme.textPrimary;
  }, [theme]);

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

  // ── Commit handler — opens simulator ────────────────────────────────────
  function commitAndSimulate() {
    const name = simLayout.trim();
    if (!name) return;
    setActiveName(name);
    setMode("simulator");
  }

  // ── Load Demo shortcut ───────────────────────────────────────────────────
  function loadDemoNetwork() {
    setSimLayout("demo");
    setActiveName("demo");
    setMode("simulator");
  }

  // ── Derived ──────────────────────────────────────────────────────────────
  const statusLabel = {
    checking:  "Backend: checking…",
    connected: "Backend: connected ✓",
    error:     "Backend: unreachable",
  }[status];

  const statusClass = {
    checking:  "indicator--checking",
    connected: "indicator--ok",
    error:     "indicator--error",
  }[status];

  const btnSt = (active: boolean): React.CSSProperties => ({
    background:   active ? theme.accent : "transparent",
    border:       `1px solid ${theme.border}`,
    borderRadius: 6,
    color:        theme.textPrimary,
    cursor:       "pointer",
    fontSize:     12,
    fontWeight:   600,
    padding:      "4px 12px",
    transition:   "background 0.12s",
  });

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <ThemeContext.Provider value={themeCtx}>
      <EditorStoreProvider>
        <div className="app-shell" style={{ background: theme.bgCanvas, color: theme.textPrimary }}>

          {/* ── Topbar ────────────────────────────────────────────── */}
          <header className="topbar" style={{ background: theme.bgSurface, borderBottom: `1px solid ${theme.border}` }}>
            <div className="topbar__brand">
              <span className="topbar__logo">🚆</span>
              <span className="topbar__title" style={{ color: theme.textPrimary }}>TrainNet ETA Simulator</span>
            </div>

            {/* Layout name + simulate button */}
            <div style={{ display: "flex", gap: 4, alignItems: "center", marginLeft: 24 }}>
              <button id="mode-editor" onClick={() => setMode("editor")} style={btnSt(mode === "editor")}>
                ✏ Editor
              </button>

              <input
                id="sim-layout-input"
                style={{
                  background:   theme.bgElevated,
                  border:       `1px solid ${theme.border}`,
                  borderRadius: 5,
                  color:        theme.textPrimary,
                  fontSize:     12,
                  padding:      "4px 8px",
                  width:        120,
                  outline:      "none",
                }}
                value={simLayout}
                onChange={(e) => setSimLayout(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") commitAndSimulate(); }}
                placeholder="layout name"
              />

              <button
                id="btn-simulate"
                onClick={commitAndSimulate}
                style={{
                  background:   "#0f766e",
                  border:       "none",
                  borderRadius: 5,
                  color:        "#fff",
                  cursor:       "pointer",
                  fontSize:     12,
                  fontWeight:   700,
                  padding:      "5px 12px",
                  transition:   "opacity 0.12s",
                }}
              >
                ▶ Simulate
              </button>

              {/* Load Demo button */}
              <button
                id="btn-load-demo"
                onClick={loadDemoNetwork}
                title="Load the built-in conflict demo network (3 stations, 1 junction, 4 trains)"
                style={{
                  background:   "#7c3aed",
                  border:       "none",
                  borderRadius: 5,
                  color:        "#fff",
                  cursor:       "pointer",
                  fontSize:     11,
                  fontWeight:   700,
                  padding:      "5px 10px",
                  transition:   "opacity 0.12s",
                  whiteSpace:   "nowrap",
                }}
              >
                ⚡ Demo Network
              </button>
            </div>

            {/* Theme toggle */}
            <button
              id="btn-theme-toggle"
              onClick={themeCtx.toggle}
              title={`Switch to ${themeMode === "dark" ? "light" : "dark"} mode`}
              style={{
                background:   "transparent",
                border:       `1px solid ${theme.border}`,
                borderRadius: 6,
                color:        theme.textSecondary,
                cursor:       "pointer",
                fontSize:     16,
                lineHeight:   1,
                padding:      "3px 8px",
                marginLeft:   8,
              }}
            >
              {themeMode === "dark" ? "☀" : "🌙"}
            </button>

            {/* Backend status badge */}
            <div className={`indicator ${statusClass}`} id="backend-status-indicator">
              <span className="indicator__dot" />
              <span className="indicator__label">{statusLabel}</span>
            </div>
          </header>

          {/* ── Editor mode ─────────────────────────────────────── */}
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

          {/* ── Simulator mode ──────────────────────────────────── */}
          {mode === "simulator" && activeName && (
            <div style={{ flex: 1, overflow: "hidden", display: "flex", flexDirection: "column" }}>
              <SimulatorCanvas
                key={activeName}
                layoutName={activeName}
                onClose={() => setMode("editor")}
              />
            </div>
          )}

          {/* Prompt when no name committed yet */}
          {mode === "simulator" && !activeName && (
            <div style={{
              flex: 1, display: "flex", alignItems: "center", justifyContent: "center",
              color: theme.textMuted, fontSize: 13, background: theme.bgCanvas,
              flexDirection: "column", gap: 12,
            }}>
              <span>Enter a layout name and click <strong style={{ color: theme.textPrimary }}>▶ Simulate</strong>.</span>
              <button onClick={commitAndSimulate} style={{
                background: theme.accent, border: "none", borderRadius: 6,
                color: "#fff", cursor: "pointer", fontSize: 13, fontWeight: 700, padding: "8px 20px",
              }}>
                ▶ Simulate "{simLayout}"
              </button>
            </div>
          )}

        </div>
      </EditorStoreProvider>
    </ThemeContext.Provider>
  );
}
