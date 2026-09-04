import { useEffect, useRef, useState } from "react";
import { Stage, Layer, Text } from "react-konva";
import { checkHealth } from "./api";
import "./App.css";

type ConnectionStatus = "checking" | "connected" | "error";

export default function App() {
  const [status, setStatus] = useState<ConnectionStatus>("checking");
  const [stageSize, setStageSize] = useState({ width: 800, height: 600 });
  const containerRef = useRef<HTMLDivElement>(null);

  // Ping the backend health endpoint on mount
  useEffect(() => {
    checkHealth()
      .then(() => setStatus("connected"))
      .catch(() => setStatus("error"));
  }, []);

  // Resize the Konva stage to fill its container
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (entry) {
        setStageSize({
          width: entry.contentRect.width,
          height: entry.contentRect.height,
        });
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const statusLabel =
    status === "checking"
      ? "Backend: checking…"
      : status === "connected"
        ? "Backend: connected"
        : "Backend: unreachable";

  const statusClass =
    status === "checking"
      ? "indicator--checking"
      : status === "connected"
        ? "indicator--ok"
        : "indicator--error";

  return (
    <div className="app-shell">
      {/* ── Top bar ──────────────────────────────────────────── */}
      <header className="topbar">
        <div className="topbar__brand">
          <span className="topbar__logo">🚆</span>
          <span className="topbar__title">TrainNet ETA Simulator</span>
        </div>
        <div className={`indicator ${statusClass}`} id="backend-status-indicator">
          <span className="indicator__dot" />
          <span className="indicator__label">{statusLabel}</span>
        </div>
      </header>

      {/* ── Canvas area ──────────────────────────────────────── */}
      <main className="canvas-area" ref={containerRef}>
        <Stage
          width={stageSize.width}
          height={stageSize.height}
          id="main-stage"
        >
          <Layer>
            {/* Placeholder text — will be replaced by network geometry in later phases */}
            <Text
              text="Canvas ready — author your rail network here"
              x={stageSize.width / 2}
              y={stageSize.height / 2}
              offsetX={180}
              offsetY={10}
              fontSize={16}
              fill="#64748b"
              fontFamily="'Inter', sans-serif"
            />
          </Layer>
        </Stage>
      </main>
    </div>
  );
}
