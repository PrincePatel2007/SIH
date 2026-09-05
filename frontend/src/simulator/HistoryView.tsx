/**
 * HistoryView.tsx
 *
 * Fetches and renders the scheduled-vs-actual history for a selected train
 * by calling GET /api/simulations/{name}/history/{train_id}.
 *
 * Shows:
 *   - A per-stop table: station | sched arrival | actual arrival | delay (±min)
 *   - A mini bar chart of delay per stop (inline SVG, no external deps)
 *   - A raw-event accordion for detailed event-log review
 *
 * Used inside SidePanel.tsx when a train is selected in simulator mode.
 */

import { useEffect, useRef, useState, CSSProperties } from "react";
import { useTheme } from "../theme";

// ─── API types ────────────────────────────────────────────────────────────────

interface StopHistoryEntry {
  node_id:            string;
  node_name:          string;
  scheduled_arrival:  string | null;
  scheduled_departure:string | null;
  actual_arrival:     string | null;
  delay_seconds:      number | null;  // positive = late, negative = early
  events:             string[];
}

interface TrainHistoryResponse {
  train_id:   string;
  train_name: string;
  status:     string;
  stops:      StopHistoryEntry[];
  raw_events: Array<{
    event_type: string;
    timestamp:  string;
    train_id:   string;
    node_id?:   string;
    block_id?:  string;
    detail?:    string;
  }>;
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch { return iso.slice(11, 19) || "—"; }
}

function fmtDelay(sec: number | null): { text: string; color: string } {
  if (sec === null || sec === undefined) return { text: "—", color: "#64748b" };
  if (Math.abs(sec) < 10) return { text: "On time", color: "#22c55e" };
  const sign = sec > 0 ? "+" : "-";
  const abs  = Math.abs(sec);
  const min  = Math.floor(abs / 60);
  const s    = Math.round(abs % 60);
  const str  = min > 0 ? `${sign}${min}m ${s}s` : `${sign}${s}s`;
  return { text: str, color: sec > 0 ? "#ef4444" : "#22c55e" };
}

/** Mini inline SVG bar chart showing delay per stop */
function DelayChart({ stops }: { stops: StopHistoryEntry[] }) {
  const { theme } = useTheme();
  const delays = stops.map((s) => s.delay_seconds ?? 0);
  if (delays.every((d) => d === 0)) return null;

  const maxAbs = Math.max(...delays.map(Math.abs), 1);
  const W = 240, H = 60, barW = Math.max(4, Math.floor((W - 16) / Math.max(delays.length, 1)) - 2);
  const midY = H / 2;

  return (
    <div style={{ margin: "8px 0 12px" }}>
      <span style={{ fontSize: 10, color: theme.textSecondary, textTransform: "uppercase",
                     letterSpacing: "0.05em", fontWeight: 700 }}>
        Delay per stop (s)
      </span>
      <svg width={W} height={H} style={{ display: "block", marginTop: 4, overflow: "visible" }}>
        {/* zero line */}
        <line x1={8} y1={midY} x2={W - 8} y2={midY} stroke={theme.border} strokeWidth={1} />
        {delays.map((d, i) => {
          const barH = Math.abs(d) / maxAbs * (H / 2 - 4);
          const x = 8 + i * (barW + 2);
          const y = d >= 0 ? midY - barH : midY;
          const fill = d > 10 ? "#ef4444" : d < -10 ? "#22c55e" : "#94a3b8";
          return (
            <rect key={i} x={x} y={y} width={barW} height={Math.max(barH, 1)}
              fill={fill} rx={1} opacity={0.85}>
              <title>{stops[i].node_name}: {d >= 0 ? "+" : ""}{d}s</title>
            </rect>
          );
        })}
      </svg>
    </div>
  );
}

// ─── Event type badge colours ─────────────────────────────────────────────────

const EVENT_COLORS: Record<string, string> = {
  DEPARTURE:       "#6366f1",
  ARRIVAL:         "#22c55e",
  SIGNAL_HOLD:     "#ef4444",
  SIDING_OVERTAKE: "#f59e0b",
  SIDING_RELEASE:  "#84cc16",
  BLOCK_ENTER:     "#38bdf8",
  BLOCK_EXIT:      "#94a3b8",
  WEATHER_HALT:    "#a78bfa",
  WEATHER_RESUME:  "#34d399",
  OCCUPANCY_RACE:  "#ff0000",
  CANCELLED:       "#64748b",
};

// ─── Main component ───────────────────────────────────────────────────────────

interface HistoryViewProps {
  layoutName:  string;
  trainId:     string;
  trainName:   string;
  /** Sim seconds to run headless (default 3600 = 1 sim-hour) */
  runSeconds?: number;
}

export default function HistoryView({
  layoutName,
  trainId,
  trainName,
  runSeconds = 3600,
}: HistoryViewProps) {
  const { theme } = useTheme();
  const [data,      setData]      = useState<TrainHistoryResponse | null>(null);
  const [loading,   setLoading]   = useState(false);
  const [error,     setError]     = useState<string | null>(null);
  const [rawOpen,   setRawOpen]   = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!layoutName || !trainId) return;
    setLoading(true);
    setError(null);
    setData(null);

    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;

    fetch(
      `/api/simulations/${encodeURIComponent(layoutName)}/history/${encodeURIComponent(trainId)}?run_seconds=${runSeconds}`,
      { signal: ac.signal }
    )
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d: TrainHistoryResponse) => { setData(d); setLoading(false); })
      .catch((e) => {
        if (e.name === "AbortError") return;
        setError(String(e.message ?? e));
        setLoading(false);
      });

    return () => ac.abort();
  }, [layoutName, trainId, runSeconds]);

  // ── Styles ──────────────────────────────────────────────────────────────────
  const card: CSSProperties = {
    background:   theme.bgElevated,
    border:       `1px solid ${theme.border}`,
    borderRadius: 8,
    padding:      "10px 12px",
    marginTop:    8,
  };
  const th: CSSProperties = {
    fontSize:    10,
    fontWeight:  700,
    color:       theme.textSecondary,
    textTransform: "uppercase",
    letterSpacing: "0.05em",
    padding:     "0 4px 6px",
    textAlign:   "left" as const,
    borderBottom:`1px solid ${theme.border}`,
  };
  const td: CSSProperties = {
    fontSize:    11,
    padding:     "5px 4px",
    borderBottom:`1px solid ${theme.border}`,
    color:       theme.textPrimary,
  };

  // ── Loading / error states ───────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{ ...card, textAlign: "center", color: theme.textSecondary, fontSize: 12 }}>
        <div style={{ animation: "spin 1s linear infinite", display: "inline-block",
                      fontSize: 18, marginBottom: 4 }}>⏳</div>
        <div>Running headless replay…</div>
        <div style={{ fontSize: 10, marginTop: 2 }}>({runSeconds}s sim time)</div>
      </div>
    );
  }

  if (error) {
    return (
      <div style={{ ...card, color: "#ef4444", fontSize: 12 }}>
        <b>History unavailable:</b> {error}
        <br />
        <span style={{ fontSize: 10, color: theme.textSecondary }}>
          The backend runs a fresh headless replay for each request.
          Ensure the layout is saved and the backend is running.
        </span>
      </div>
    );
  }

  if (!data) return null;

  // ── Event type summary chips ──────────────────────────────────────────────
  const eventCounts: Record<string, number> = {};
  for (const ev of data.raw_events) {
    eventCounts[ev.event_type] = (eventCounts[ev.event_type] ?? 0) + 1;
  }
  const NOTABLE = ["SIGNAL_HOLD", "SIDING_OVERTAKE", "OCCUPANCY_RACE", "WEATHER_HALT"];
  const notableEvents = Object.entries(eventCounts).filter(([k]) => NOTABLE.includes(k));

  return (
    <div style={{ marginTop: 4 }}>
      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                    marginBottom: 6 }}>
        <span style={{ fontSize: 11, fontWeight: 700, color: theme.textSecondary,
                       textTransform: "uppercase", letterSpacing: "0.05em" }}>
          History Replay
        </span>
        <span style={{ fontSize: 10, padding: "2px 6px", borderRadius: 4,
                       background: theme.bgElevated, border: `1px solid ${theme.border}`,
                       color: theme.textSecondary }}>
          {data.status}
        </span>
      </div>

      {/* Notable events chips */}
      {notableEvents.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 }}>
          {notableEvents.map(([type, count]) => (
            <span key={type} style={{
              fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 10,
              background: (EVENT_COLORS[type] ?? "#94a3b8") + "22",
              color: EVENT_COLORS[type] ?? "#94a3b8",
              border: `1px solid ${(EVENT_COLORS[type] ?? "#94a3b8")}55`,
            }}>
              {type.replace("_", " ")} ×{count}
            </span>
          ))}
        </div>
      )}

      {/* Delay chart */}
      <DelayChart stops={data.stops} />

      {/* Stop table */}
      <div style={card}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={th}>Stop</th>
              <th style={{ ...th, textAlign: "right" }}>Sched.</th>
              <th style={{ ...th, textAlign: "right" }}>Actual</th>
              <th style={{ ...th, textAlign: "right" }}>Delay</th>
            </tr>
          </thead>
          <tbody>
            {data.stops.map((stop) => {
              const { text: delayText, color: delayColor } = fmtDelay(stop.delay_seconds);
              const hasConflict = stop.events.some((e) =>
                ["SIGNAL_HOLD", "SIDING_OVERTAKE", "OCCUPANCY_RACE"].includes(e));
              return (
                <tr key={stop.node_id}
                    style={{ background: hasConflict ? "#ef444411" : undefined }}>
                  <td style={td}>
                    {hasConflict && <span title={stop.events.join(", ")} style={{ marginRight: 3 }}>⚠</span>}
                    {stop.node_name}
                  </td>
                  <td style={{ ...td, textAlign: "right", color: theme.textSecondary }}>
                    {fmtTime(stop.scheduled_arrival)}
                  </td>
                  <td style={{ ...td, textAlign: "right" }}>
                    {fmtTime(stop.actual_arrival)}
                  </td>
                  <td style={{ ...td, textAlign: "right", color: delayColor, fontWeight: 700 }}>
                    {delayText}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Raw event log accordion */}
      <button
        onClick={() => setRawOpen((o) => !o)}
        style={{
          width:        "100%",
          marginTop:    8,
          background:   "none",
          border:       `1px solid ${theme.border}`,
          borderRadius: 6,
          color:        theme.textSecondary,
          cursor:       "pointer",
          fontSize:     11,
          padding:      "5px 10px",
          textAlign:    "left",
        }}
      >
        {rawOpen ? "▲" : "▼"} Raw events ({data.raw_events.length})
      </button>

      {rawOpen && (
        <div style={{
          ...card,
          maxHeight:  200,
          overflowY:  "auto",
          fontFamily: "monospace",
          fontSize:   10,
          marginTop:  4,
        }}>
          {data.raw_events.map((ev, i) => {
            const color = EVENT_COLORS[ev.event_type] ?? "#94a3b8";
            const t = ev.timestamp.slice(11, 19);
            const loc = ev.node_id ? `@ ${ev.node_id}` : ev.block_id ? `blk:${ev.block_id}` : "";
            return (
              <div key={i} style={{ padding: "2px 0", borderBottom: `1px solid ${theme.border}33`,
                                    display: "flex", gap: 6 }}>
                <span style={{ color: theme.textSecondary, minWidth: 60 }}>{t}</span>
                <span style={{ color, minWidth: 120, fontWeight: 700 }}>{ev.event_type}</span>
                <span style={{ color: theme.textSecondary }}>{loc}</span>
                {ev.detail && (
                  <span style={{ color: theme.textPrimary, opacity: 0.7 }}
                        title={ev.detail}>
                    {ev.detail.slice(0, 50)}
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
