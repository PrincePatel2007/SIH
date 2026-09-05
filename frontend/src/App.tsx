import { useEffect, useState } from "react";
import { checkHealth } from "./api";
import { EditorStoreProvider } from "./editor/store";
import EditorCanvas, { type ToolMode } from "./editor/EditorCanvas";
import EditorToolbar, { KEY_MAP } from "./editor/EditorToolbar";
import PropertyPanel from "./editor/PropertyPanel";
import "./App.css";

type ConnectionStatus = "checking" | "connected" | "error";

export default function App() {
  const [status, setStatus] = useState<ConnectionStatus>("checking");
  const [tool, setTool] = useState<ToolMode>("select");

  // Backend health ping
  useEffect(() => {
    checkHealth()
      .then(() => setStatus("connected"))
      .catch(() => setStatus("error"));
  }, []);

  // Global keyboard shortcuts for tool switching
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ignore when typing in an input/select
      if (
        document.activeElement &&
        ["INPUT", "SELECT", "TEXTAREA"].includes(
          (document.activeElement as HTMLElement).tagName
        )
      )
        return;
      const next = KEY_MAP[e.key.toLowerCase()];
      if (next) setTool(next);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const statusLabel =
    status === "checking"
      ? "Backend: checking…"
      : status === "connected"
        ? "Backend: connected ✓"
        : "Backend: unreachable";

  const statusClass =
    status === "checking"
      ? "indicator--checking"
      : status === "connected"
        ? "indicator--ok"
        : "indicator--error";

  return (
    <EditorStoreProvider>
      <div className="app-shell">
        {/* ── Top bar ─────────────────────────────────────────── */}
        <header className="topbar">
          <div className="topbar__brand">
            <span className="topbar__logo">🚆</span>
            <span className="topbar__title">TrainNet ETA Simulator — Editor</span>
          </div>
          <div className={`indicator ${statusClass}`} id="backend-status-indicator">
            <span className="indicator__dot" />
            <span className="indicator__label">{statusLabel}</span>
          </div>
        </header>

        {/* ── Toolbar ─────────────────────────────────────────── */}
        <EditorToolbar active={tool} onChange={setTool} />

        {/* ── Editor layout ───────────────────────────────────── */}
        <div className="editor-layout">
          <main className="canvas-area" id="canvas-area">
            <EditorCanvas tool={tool} />
          </main>
          <PropertyPanel />
        </div>
      </div>
    </EditorStoreProvider>
  );
}
