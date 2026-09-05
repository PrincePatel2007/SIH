/**
 * SidePanel.tsx — Train detail panel shown when a train is selected in the simulator.
 *
 * Displays:
 *  - Train metadata: name, colour swatch, priority tier, driver_duty_status
 *  - Upcoming stops table: scheduled vs expected arrival, signed delay in minutes
 *    (only shows stops where actual_arrival is null — future stops only)
 *    NOTE: No expected_departure column — ScheduleEntry has no such field.
 *  - Recent event history: last 5 event_log entries for this train from the snapshot.
 */

import type { CSSProperties } from "react";
import { useTheme } from "../theme";

// ---------------------------------------------------------------------------
// Types (mirroring WS snapshot + TrainSnap shapes)
// ---------------------------------------------------------------------------

interface ScheduleEntry {
  scheduled_arrival:   string | null;
  scheduled_departure: string | null;
  expected_arrival:    string | null;
  actual_arrival:      string | null;
}

interface TrainSnap {
  id:          string;
  name:        string;
  color:       string;
  priority:    number;   // 1=Express / 2=Ordinary / 3=Local
  status:      string;
  driver_duty_status?: string;
  route:       Array<{ node_id: string; segment_id: string | null }>;
  schedule:    Record<string, ScheduleEntry>;
}

interface EventLogEntry {
  timestamp:  string;
  event_type: string;
  train_id:   string | null;
  node_id:    string | null;
  block_id:   string | null;
  detail:     string;
}

interface LayoutStation {
  id:   string;
  name: string;
}

interface Props {
  train:       TrainSnap;
  simClock:    string;              // ISO string of current simulation clock
  stationMap:  Record<string, LayoutStation>;
  eventLog:    EventLogEntry[];
  onClose:     () => void;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const PRIORITY_LABEL: Record<number, string> = {
  1: "1 — Express",
  2: "2 — Ordinary",
  3: "3 — Local",
};

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
  catch { return "—"; }
}

/** Signed delay in minutes: positive = late, negative = early. */
function delayMins(expected: string | null, scheduled: string | null): number | null {
  if (!expected || !scheduled) return null;
  try {
    const e = new Date(expected).getTime();
    const s = new Date(scheduled).getTime();
    return Math.round((e - s) / 60_000);
  } catch { return null; }
}

function DelayBadge({ mins }: { mins: number | null }) {
  if (mins === null) return <span style={{ color: "#64748b" }}>—</span>;
  if (mins === 0) return <span style={{ color: "#22c55e" }}>On time</span>;
  const sign  = mins > 0 ? "+" : "";
  const color = mins > 0 ? "#ef4444" : "#22c55e";
  return <span style={{ color, fontWeight: 600 }}>{sign}{mins} min</span>;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function SidePanel({ train, simClock, stationMap, eventLog, onClose }: Props) {
  const { theme } = useTheme();

  const panelStyle: CSSProperties = {
    width:         300,
    height:        "100%",
    background:    theme.bgSurface,
    borderLeft:    `1px solid ${theme.border}`,
    color:         theme.textPrimary,
    fontFamily:    "'Inter', system-ui, sans-serif",
    fontSize:      13,
    display:       "flex",
    flexDirection: "column",
    flexShrink:    0,
    overflowY:     "auto",
  };

  const sectionStyle: CSSProperties = {
    padding:      "14px 16px 0",
    borderBottom: `1px solid ${theme.border}`,
    paddingBottom: 14,
  };

  const labelStyle: CSSProperties = {
    display:       "block",
    color:         theme.textSecondary,
    fontSize:      10,
    fontWeight:    700,
    textTransform: "uppercase",
    letterSpacing: "0.06em",
    marginBottom:  3,
  };

  const valueStyle: CSSProperties = {
    color:        theme.textPrimary,
    fontSize:     13,
    marginBottom: 10,
  };

  const headingStyle: CSSProperties = {
    fontSize:     11,
    fontWeight:   700,
    color:        theme.textSecondary,
    textTransform:"uppercase",
    letterSpacing:"0.05em",
    marginBottom: 8,
    display:      "block",
  };

  // ── Upcoming stops (future only — actual_arrival is null) ─────────────────
  const clock = new Date(simClock);
  const upcomingStops = train.route
    .filter((h) => {
      const entry = train.schedule[h.node_id];
      if (!entry) return false;
      // Show stops that haven't been reached yet
      if (entry.actual_arrival) return false;
      // Must have a scheduled_arrival (terminal departure-only hop excluded)
      return !!entry.scheduled_arrival;
    })
    .slice(0, 8);  // cap at 8 rows

  // ── Recent event log for this train ──────────────────────────────────────
  const trainEvents = eventLog
    .filter((e) => e.train_id === train.id)
    .slice(-5)
    .reverse();

  const dutyColor = train.driver_duty_status === "over_duty" ? "#ef4444" : theme.signalGreen;

  return (
    <div style={panelStyle} role="complementary" aria-label={`Details for ${train.name}`}>

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "12px 16px",
                    borderBottom: `1px solid ${theme.border}`, flexShrink: 0 }}>
        <span style={{ width: 14, height: 14, borderRadius: 3, background: train.color,
                        display: "inline-block", flexShrink: 0, border: `1px solid ${theme.border}` }} />
        <span style={{ fontWeight: 700, fontSize: 14, color: theme.textPrimary, flex: 1 }}>
          {train.name}
        </span>
        <span style={{ fontSize: 11, padding: "2px 6px", borderRadius: 4,
                        background: theme.bgElevated, color: theme.textSecondary }}>
          {train.status}
        </span>
        <button
          onClick={onClose}
          aria-label="Close side panel"
          style={{ background: "none", border: "none", cursor: "pointer",
                   color: theme.textSecondary, fontSize: 16, lineHeight: 1, padding: 0 }}
        >
          ✕
        </button>
      </div>

      {/* Metadata */}
      <div style={sectionStyle}>
        <span style={headingStyle}>Train Info</span>

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0 12px" }}>
          <div>
            <span style={labelStyle}>Priority</span>
            <div style={valueStyle}>{PRIORITY_LABEL[train.priority] ?? train.priority}</div>
          </div>
          <div>
            <span style={labelStyle}>Driver Status</span>
            <div style={{ ...valueStyle, color: dutyColor }}>
              {train.driver_duty_status ?? "normal"}
            </div>
          </div>
        </div>
      </div>

      {/* Upcoming stops */}
      <div style={{ padding: "14px 16px 0", flex: 1, minHeight: 0 }}>
        <span style={headingStyle}>Upcoming Stops</span>

        {upcomingStops.length === 0 ? (
          <p style={{ color: theme.textMuted, fontSize: 12, marginTop: 4 }}>
            No upcoming stops.
          </p>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 11 }}>
            <thead>
              <tr style={{ color: theme.textSecondary }}>
                <th style={{ textAlign: "left", paddingBottom: 6, fontWeight: 600 }}>Station</th>
                <th style={{ textAlign: "right", paddingBottom: 6, fontWeight: 600 }}>Sched.</th>
                <th style={{ textAlign: "right", paddingBottom: 6, fontWeight: 600 }}>ETA</th>
                <th style={{ textAlign: "right", paddingBottom: 6, fontWeight: 600 }}>Delay</th>
              </tr>
            </thead>
            <tbody>
              {upcomingStops.map((hop) => {
                const entry = train.schedule[hop.node_id]!;
                const sta   = stationMap[hop.node_id];
                const delay = delayMins(entry.expected_arrival, entry.scheduled_arrival);
                const isNext = upcomingStops[0]?.node_id === hop.node_id;
                return (
                  <tr
                    key={hop.node_id}
                    style={{
                      borderTop:  `1px solid ${theme.border}`,
                      background: isNext ? `${theme.accent}15` : "transparent",
                    }}
                  >
                    <td style={{ padding: "5px 0", color: isNext ? theme.accent : theme.textPrimary, fontWeight: isNext ? 600 : 400 }}>
                      {isNext && "→ "}{sta?.name ?? hop.node_id}
                    </td>
                    <td style={{ textAlign: "right", color: theme.textSecondary, padding: "5px 0" }}>
                      {fmtTime(entry.scheduled_arrival)}
                    </td>
                    <td style={{ textAlign: "right", color: theme.textPrimary, padding: "5px 0" }}>
                      {fmtTime(entry.expected_arrival ?? entry.scheduled_arrival)}
                    </td>
                    <td style={{ textAlign: "right", padding: "5px 0" }}>
                      <DelayBadge mins={delay} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Event history */}
      {trainEvents.length > 0 && (
        <div style={{ padding: "14px 16px", borderTop: `1px solid ${theme.border}`, flexShrink: 0 }}>
          <span style={headingStyle}>Recent Events</span>
          <ul style={{ listStyle: "none", margin: 0, padding: 0, fontSize: 11 }}>
            {trainEvents.map((ev, i) => (
              <li key={i} style={{ display: "flex", gap: 6, marginBottom: 5, color: theme.textSecondary }}>
                <span style={{ color: theme.textMuted, flexShrink: 0 }}>
                  {fmtTime(ev.timestamp)}
                </span>
                <span style={{ color: theme.textSecondary }}>
                  <span style={{ color: theme.textPrimary, fontWeight: 600 }}>
                    {ev.event_type.replace(/_/g, " ")}
                  </span>
                  {ev.node_id && ` @ ${stationMap[ev.node_id]?.name ?? ev.node_id}`}
                  {ev.detail && ` — ${ev.detail}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

    </div>
  );
}
