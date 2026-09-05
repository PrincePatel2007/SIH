/**
 * PropertyPanel.tsx — Right-side panel for editing selected element properties.
 *
 * Phase 7 additions:
 *  - Train section: name, color, carriages, priority, max/avg speed, duty status,
 *    origin departure, per-node dwell, derived schedule preview.
 *  - Track section now also shows segment-level speed cap + maintenance window.
 *
 * Enum values mirror models.py exactly:
 *  StationType: terminus / through / junction_station
 *  TrackDirectionality: bidirectional / one_way_forward / one_way_reverse
 *  PriorityTier: 1 (express) / 2 (ordinary) / 3 (local)
 *  SignalStateValue: green / red / caution
 *  DriverDutyStatus: normal / over_duty
 */

import type { CSSProperties } from "react";
import {
  useEditorStore,
  type JunctionWithPos,
  type StationWithPos,
  type TrainWithPos,
} from "./store";
import type {
  StationType,
  TrackDirectionality,
  PriorityTier,
  SignalStateValue,
  DriverDutyStatus,
  Train,
} from "../types";

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const panel: CSSProperties = {
  width: 280,
  minHeight: "100%",
  background: "#0f172a",
  borderLeft: "1px solid #1e293b",
  color: "#e2e8f0",
  fontFamily: "'Inter', system-ui, sans-serif",
  fontSize: 13,
  overflowY: "auto",
  flexShrink: 0,
};
const section: CSSProperties = { padding: "16px 16px 0" };
const label: CSSProperties = {
  display: "block",
  color: "#94a3b8",
  fontSize: 11,
  fontWeight: 600,
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  marginBottom: 4,
};
const input: CSSProperties = {
  width: "100%",
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: 6,
  color: "#e2e8f0",
  fontSize: 13,
  padding: "6px 8px",
  marginBottom: 12,
  boxSizing: "border-box",
};
const select: CSSProperties = { ...input };
const badge: CSSProperties = {
  display: "inline-block",
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: 4,
  padding: "2px 8px",
  fontSize: 11,
  color: "#94a3b8",
  marginBottom: 12,
};
const h2: CSSProperties = {
  margin: "0 0 12px",
  fontSize: 14,
  fontWeight: 700,
  color: "#f1f5f9",
  borderBottom: "1px solid #1e293b",
  paddingBottom: 8,
};
const h3: CSSProperties = {
  margin: "12px 0 8px",
  fontSize: 12,
  fontWeight: 600,
  color: "#94a3b8",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
};
const empty: CSSProperties = { padding: 20, color: "#475569", textAlign: "center", fontSize: 12 };
const SIGNAL_DOT: Record<string, CSSProperties> = {
  green:   { display: "inline-block", width: 10, height: 10, borderRadius: "50%", background: "#22c55e", marginRight: 6 },
  red:     { display: "inline-block", width: 10, height: 10, borderRadius: "50%", background: "#ef4444", marginRight: 6 },
  caution: { display: "inline-block", width: 10, height: 10, borderRadius: "50%", background: "#f59e0b", marginRight: 6 },
};
const twoCol: CSSProperties = { display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 };
const derivedScheduleRow: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  fontSize: 11,
  color: "#64748b",
  fontStyle: "italic",
  marginBottom: 4,
};
const clearBtn: CSSProperties = {
  background: "transparent",
  border: "1px solid #334155",
  borderRadius: 4,
  color: "#94a3b8",
  cursor: "pointer",
  fontSize: 11,
  padding: "2px 8px",
  marginBottom: 12,
};

// ---------------------------------------------------------------------------
// Helper: get node name from id
// ---------------------------------------------------------------------------
function useNodeName(nodeId: string) {
  const { state } = useEditorStore();
  const sta = state.stations.find((s) => s.id === nodeId);
  if (sta) return sta.name;
  const jct = state.junctions.find((j) => j.id === nodeId);
  if (jct) return `Junction ${jct.id.slice(-4)}`;
  return nodeId;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function PropertyPanel() {
  const { state, dispatch } = useEditorStore();
  const { selection } = state;

  if (selection.size === 0) {
    return (
      <aside style={panel} id="property-panel" aria-label="Property Panel">
        <p style={empty}>Select an element on the canvas to edit its properties.</p>
      </aside>
    );
  }
  if (selection.size > 1) {
    return (
      <aside style={panel} id="property-panel" aria-label="Property Panel">
        <BulkEditPanel />
      </aside>
    );
  }

  const id = [...selection][0];
  const track   = state.tracks.find((t) => t.id === id);
  const station = state.stations.find((s) => s.id === id);
  const junction = state.junctions.find((j) => j.id === id);
  const signal  = state.signals.find((s) => s.id === id);
  const train   = state.trains.find((t) => t.id === id);

  // Segment override for the selected track
  const segOverride = track
    ? state.segmentOverrides.find((o) => o.segment_id === track.segment_id)
    : null;

  return (
    <aside style={panel} id="property-panel" aria-label="Property Panel">

      {/* ── Track ──────────────────────────────────────────────────────── */}
      {track && (
        <div style={section}>
          <h2 style={h2}>Track</h2>
          <span style={badge}>{track.id}</span>

          <label style={label} htmlFor="pp-track-directionality">Directionality</label>
          <select id="pp-track-directionality" style={select} value={track.directionality}
            onChange={(e) => dispatch({ type: "UPDATE_TRACK", id: track.id, patch: { directionality: e.target.value as TrackDirectionality } })}>
            <option value="bidirectional">Bidirectional ↔</option>
            <option value="one_way_forward">One-way → (forward)</option>
            <option value="one_way_reverse">One-way ← (reverse)</option>
          </select>

          <label style={label} htmlFor="pp-track-priority">Min priority tier allowed</label>
          <select id="pp-track-priority" style={select} value={track.restricted_to_priority ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              const val: PriorityTier | null = v === "" ? null : (Number(v) as PriorityTier);
              dispatch({ type: "UPDATE_TRACK", id: track.id, patch: { restricted_to_priority: val } });
            }}>
            <option value="">Unrestricted (all trains)</option>
            <option value="1">1 — Express only</option>
            <option value="2">2 — Ordinary + Express</option>
            <option value="3">3 — All (local permitted)</option>
          </select>

          <p style={{ ...badge, display: "block" }}>{track.geometry.length} geometry points</p>

          {/* Segment speed/maintenance overrides */}
          <h3 style={h3}>Block Overrides (segment {track.segment_id.slice(-6)})</h3>

          <label style={label} htmlFor="pp-seg-speed">Speed restriction (km/h)</label>
          <input id="pp-seg-speed" style={input} type="number" min={1} step={5}
            placeholder="No restriction"
            value={segOverride?.speed_restriction ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              dispatch({
                type: "UPDATE_SEGMENT_OVERRIDE",
                segmentId: track.segment_id,
                patch: { speed_restriction: v === "" ? null : Number(v) },
              });
            }}
          />

          <label style={label}>Maintenance window</label>
          <div style={twoCol}>
            <div>
              <label style={{ ...label, marginBottom: 2 }}>Start</label>
              <input style={{ ...input, marginBottom: 4 }} type="datetime-local"
                value={segOverride?.maintenance_window?.start_iso?.slice(0, 16) ?? ""}
                onChange={(e) => {
                  const start = e.target.value ? e.target.value + ":00Z" : null;
                  const existing = segOverride?.maintenance_window;
                  dispatch({
                    type: "UPDATE_SEGMENT_OVERRIDE",
                    segmentId: track.segment_id,
                    patch: { maintenance_window: start ? { start_iso: start, end_iso: existing?.end_iso ?? start } : null },
                  });
                }}
              />
            </div>
            <div>
              <label style={{ ...label, marginBottom: 2 }}>End</label>
              <input style={{ ...input, marginBottom: 4 }} type="datetime-local"
                value={segOverride?.maintenance_window?.end_iso?.slice(0, 16) ?? ""}
                onChange={(e) => {
                  const end = e.target.value ? e.target.value + ":00Z" : null;
                  const existing = segOverride?.maintenance_window;
                  if (!existing && !end) return;
                  dispatch({
                    type: "UPDATE_SEGMENT_OVERRIDE",
                    segmentId: track.segment_id,
                    patch: { maintenance_window: end ? { start_iso: existing?.start_iso ?? end, end_iso: end } : null },
                  });
                }}
              />
            </div>
          </div>
          {segOverride?.maintenance_window && (
            <button style={clearBtn} onClick={() =>
              dispatch({ type: "UPDATE_SEGMENT_OVERRIDE", segmentId: track.segment_id, patch: { maintenance_window: null } })
            }>
              Clear maintenance window
            </button>
          )}
        </div>
      )}

      {/* ── Station ────────────────────────────────────────────────────── */}
      {station && (
        <div style={section}>
          <h2 style={h2}>Station</h2>
          <span style={badge}>{station.id}</span>

          <label style={label} htmlFor="pp-sta-name">Name</label>
          <input id="pp-sta-name" style={input} type="text" value={station.name}
            onChange={(e) => dispatch({ type: "UPDATE_STATION", id: station.id, patch: { name: e.target.value } })} />

          <label style={label} htmlFor="pp-sta-type">Station type</label>
          <select id="pp-sta-type" style={select} value={station.station_type}
            onChange={(e) => dispatch({ type: "UPDATE_STATION", id: station.id, patch: { station_type: e.target.value as StationType } })}>
            <option value="through">Through</option>
            <option value="terminus">Terminus (dead-end)</option>
            <option value="junction_station">Junction station</option>
          </select>

          <label style={label} htmlFor="pp-sta-rotation">Rotation (°)</label>
          <input id="pp-sta-rotation" style={input} type="number" min={0} max={359} step={15}
            value={station.rotation_deg}
            onChange={(e) => dispatch({ type: "UPDATE_STATION", id: station.id, patch: { rotation_deg: Number(e.target.value) } })} />

          <p style={{ ...badge, display: "block" }}>
            {station.platform_tracks.length || 1} platform track(s)
          </p>
          <p style={{ color: "#475569", fontSize: 11, marginTop: -8, marginBottom: 12 }}>
            Platform tracks linked by connecting track endpoints to this station.
          </p>
        </div>
      )}

      {/* ── Junction ───────────────────────────────────────────────────── */}
      {junction && (
        <div style={section}>
          <h2 style={h2}>Junction</h2>
          <span style={badge}>{junction.id}</span>

          <label style={label}>Connected segments</label>
          {junction.connected_segment_ids.length === 0 ? (
            <p style={{ color: "#475569", fontSize: 12 }}>
              No segments yet. Drag a track endpoint near this junction to connect it.
            </p>
          ) : (
            <ul style={{ padding: "0 0 0 16px", margin: "0 0 12px", color: "#94a3b8", fontSize: 12 }}>
              {junction.connected_segment_ids.map((sid) => <li key={sid}>{sid}</li>)}
            </ul>
          )}

          <label style={label}>Approach signals</label>
          {Object.keys((junction as JunctionWithPos).signal_states).length === 0 ? (
            <p style={{ color: "#475569", fontSize: 12 }}>
              Auto-created by backend when tracks are connected.
            </p>
          ) : (
            <ul style={{ padding: "0 0 0 16px", margin: 0, fontSize: 12, color: "#94a3b8" }}>
              {Object.entries((junction as JunctionWithPos).signal_states).map(([seg, sigId]) => (
                <li key={seg}>{seg} → {sigId}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* ── Signal ─────────────────────────────────────────────────────── */}
      {signal && (
        <div style={section}>
          <h2 style={h2}>Signal</h2>
          <span style={badge}>{signal.id}</span>

          <label style={label}>Block</label>
          <p style={badge}>{signal.block_id || "—"}</p>

          <label style={label} htmlFor="pp-sig-state">State</label>
          <select id="pp-sig-state" style={select} value={signal.state}
            onChange={(e) => dispatch({ type: "UPDATE_SIGNAL", id: signal.id, patch: { state: e.target.value as SignalStateValue } })}>
            <option value="green">🟢 Green — clear</option>
            <option value="caution">🟡 Caution — approach slow</option>
            <option value="red">🔴 Red — stop</option>
          </select>
          <div style={{ display: "flex", alignItems: "center", marginBottom: 12 }}>
            <span style={SIGNAL_DOT[signal.state]} />
            <span style={{ color: "#94a3b8", fontSize: 12 }}>
              {signal.state.charAt(0).toUpperCase() + signal.state.slice(1)}
            </span>
          </div>

          <label style={label} htmlFor="pp-sig-junction">Controlling junction</label>
          <select id="pp-sig-junction" style={select} value={signal.controlled_by_junction_id ?? ""}
            onChange={(e) => dispatch({ type: "UPDATE_SIGNAL", id: signal.id, patch: { controlled_by_junction_id: e.target.value || null } })}>
            <option value="">Free-standing (no junction)</option>
            {state.junctions.map((j) => <option key={j.id} value={j.id}>{j.id}</option>)}
          </select>
        </div>
      )}

      {/* ── Train ──────────────────────────────────────────────────────── */}
      {train && (
        <TrainSection train={train as TrainWithPos} />
      )}
    </aside>
  );
}

// ---------------------------------------------------------------------------
// Train section sub-component (large enough to merit its own function)
// ---------------------------------------------------------------------------

function TrainSection({ train }: { train: TrainWithPos }) {
  const { state, dispatch } = useEditorStore();

  const originNodeId  = train.route[0]?.node_id ?? null;
  const originDeparture = originNodeId
    ? (train.schedule[originNodeId]?.scheduled_departure ?? null)
    : null;

  return (
    <div style={section}>
      <h2 style={h2}>Train</h2>
      <span style={badge}>{train.id}</span>

      <label style={label} htmlFor="pp-trn-name">Name</label>
      <input id="pp-trn-name" style={input} type="text" value={train.name}
        onChange={(e) => dispatch({ type: "UPDATE_TRAIN", id: train.id, patch: { name: e.target.value } })} />

      <label style={label} htmlFor="pp-trn-color">Colour</label>
      <input id="pp-trn-color" style={{ ...input, padding: "2px 4px", height: 36 }} type="color"
        value={train.color}
        onChange={(e) => dispatch({ type: "UPDATE_TRAIN", id: train.id, patch: { color: e.target.value } })} />

      <div style={twoCol}>
        <div>
          <label style={label} htmlFor="pp-trn-carriages">Carriages</label>
          <input id="pp-trn-carriages" style={input} type="number" min={1} max={30}
            value={train.num_carriages}
            onChange={(e) => dispatch({ type: "UPDATE_TRAIN", id: train.id, patch: { num_carriages: Number(e.target.value) } })} />
        </div>
        <div>
          <label style={label} htmlFor="pp-trn-priority">Priority tier</label>
          <select id="pp-trn-priority" style={select} value={train.priority}
            onChange={(e) => dispatch({ type: "UPDATE_TRAIN", id: train.id, patch: { priority: Number(e.target.value) as 1 | 2 | 3 } })}>
            <option value={1}>1 — Express</option>
            <option value={2}>2 — Ordinary</option>
            <option value={3}>3 — Local</option>
          </select>
        </div>
      </div>

      <div style={twoCol}>
        <div>
          <label style={label} htmlFor="pp-trn-maxspd">Max speed (km/h)</label>
          <input id="pp-trn-maxspd" style={input} type="number" min={1} step={10}
            value={train.max_speed}
            onChange={(e) => dispatch({ type: "UPDATE_TRAIN", id: train.id, patch: { max_speed: Number(e.target.value) } })} />
        </div>
        <div>
          <label style={label} htmlFor="pp-trn-avgspd">Avg speed (km/h)</label>
          <input id="pp-trn-avgspd" style={input} type="number" min={1} step={10}
            value={train.avg_speed}
            onChange={(e) => dispatch({ type: "UPDATE_TRAIN", id: train.id, patch: { avg_speed: Number(e.target.value) } })} />
        </div>
      </div>

      <label style={label} htmlFor="pp-trn-duty">Driver duty status</label>
      <select id="pp-trn-duty" style={select} value={train.driver_duty_status}
        onChange={(e) => dispatch({ type: "UPDATE_TRAIN", id: train.id, patch: { driver_duty_status: e.target.value as DriverDutyStatus } })}>
        <option value="normal">Normal</option>
        <option value="over_duty">Over duty (12h+ override)</option>
      </select>

      {/* Route summary */}
      <h3 style={h3}>Route ({train.route.length} hops)</h3>
      {train.route.length === 0 ? (
        <p style={{ color: "#475569", fontSize: 12, marginBottom: 12 }}>
          Select the Train tool and click this train, then click nodes to build a route. Press Enter to commit.
        </p>
      ) : (
        <>
          {/* Origin departure — the ONLY time the user authors directly */}
          <label style={label} htmlFor="pp-trn-origin-dep">
            Origin departure <span style={{ color: "#64748b", fontWeight: 400 }}>(required)</span>
          </label>
          <input
            id="pp-trn-origin-dep"
            style={input}
            type="datetime-local"
            value={originDeparture ? originDeparture.replace("Z", "").slice(0, 16) : ""}
            onChange={(e) => {
              if (!originNodeId) return;
              const iso = e.target.value ? e.target.value + ":00Z" : null;
              dispatch({
                type: "UPDATE_TRAIN",
                id: train.id,
                patch: {
                  schedule: {
                    ...train.schedule,
                    [originNodeId]: {
                      ...(train.schedule[originNodeId] ?? { scheduled_arrival: null, expected_arrival: null, actual_arrival: null }),
                      scheduled_departure: iso,
                    },
                  } as Train["schedule"],
                },
              });
            }}
          />

          {/* Per-node dwell */}
          <h3 style={h3}>Stop dwells</h3>
          <p style={{ color: "#475569", fontSize: 11, marginBottom: 8 }}>
            Minutes the train waits at each stop. Backend computes all ETAs.
          </p>
          {train.route.slice(1).map((hop) => (
            <DwellRow key={hop.node_id} trainId={train.id} nodeId={hop.node_id} dwell={train._dwell} />
          ))}

          {/* Derived schedule preview (read-only, shown after save) */}
          {Object.keys(train.schedule).length > 0 && (
            <>
              <h3 style={h3}>Computed schedule (read-only)</h3>
              <p style={{ color: "#475569", fontSize: 11, marginBottom: 6 }}>
                Filled by backend after Save. Shows scheduled arrival at each node.
              </p>
              {train.route.map((hop) => {
                const entry = train.schedule[hop.node_id];
                if (!entry?.scheduled_arrival) return null;
                const dt = new Date(entry.scheduled_arrival);
                return (
                  <div key={hop.node_id} style={derivedScheduleRow}>
                    <span><NodeName nodeId={hop.node_id} state={state} /></span>
                    <span>{dt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
                  </div>
                );
              })}
            </>
          )}
        </>
      )}
    </div>
  );
}

// Tiny sub-components to avoid hook-in-loop

function DwellRow({ trainId, nodeId, dwell }: { trainId: string; nodeId: string; dwell: Record<string, number> }) {
  const { state, dispatch } = useEditorStore();
  const nodeName = (() => {
    const sta = state.stations.find((s) => s.id === nodeId);
    if (sta) return sta.name;
    const jct = state.junctions.find((j) => j.id === nodeId);
    if (jct) return `Junction ${jct.id.slice(-4)}`;
    return nodeId.slice(-6);
  })();
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
      <span style={{ flex: 1, fontSize: 11, color: "#94a3b8", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{nodeName}</span>
      <input
        style={{ width: 64, background: "#1e293b", border: "1px solid #334155", borderRadius: 4, color: "#e2e8f0", fontSize: 12, padding: "4px 6px" }}
        type="number" min={0} max={120} step={1}
        placeholder="0"
        value={dwell[nodeId] ?? ""}
        onChange={(e) => dispatch({ type: "UPDATE_TRAIN_DWELL", trainId, nodeId, minutes: Number(e.target.value) })}
      />
      <span style={{ fontSize: 11, color: "#475569" }}>min</span>
    </div>
  );
}

function NodeName({ nodeId, state }: { nodeId: string; state: ReturnType<typeof useEditorStore>["state"] }) {
  const sta = state.stations.find((s) => s.id === nodeId);
  if (sta) return <>{sta.name}</>;
  const jct = state.junctions.find((j) => j.id === nodeId);
  if (jct) return <>Junction {jct.id.slice(-4)}</>;
  return <>{nodeId.slice(-6)}</>;
}

// ---------------------------------------------------------------------------
// BulkEditPanel — shown when multiple elements are marquee-selected
//
// Algorithm:
//  1. Determine which type(s) are in the selection.
//  2. If all selected elements are the SAME type → show common fields editable.
//  3. If mixed types → show a friendly "same type required" message.
//  4. Each field change dispatches once per selected ID.
// ---------------------------------------------------------------------------

function BulkEditPanel() {
  const { state, dispatch } = useEditorStore();
  const { selection } = state;
  const ids = [...selection];

  // Classify each selected id
  const types = ids.map((id) => {
    if (state.tracks.find((t) => t.id === id))    return "track";
    if (state.stations.find((s) => s.id === id))  return "station";
    if (state.junctions.find((j) => j.id === id)) return "junction";
    if (state.signals.find((s) => s.id === id))   return "signal";
    if (state.trains.find((t) => t.id === id))    return "train";
    return "unknown";
  });

  const uniqueTypes = [...new Set(types)];
  const commonType  = uniqueTypes.length === 1 ? uniqueTypes[0] : null;

  const hdr: CSSProperties = { ...h2, marginBottom: 8 };

  return (
    <div style={{ padding: 16 }}>
      <h2 style={hdr}>Bulk Edit</h2>
      <span style={badge}>{ids.length} elements selected</span>

      {/* Mixed types */}
      {!commonType && (
        <p style={{ color: "#64748b", fontSize: 12, marginTop: 12 }}>
          Mixed element types selected ({uniqueTypes.join(", ")}).
          <br />Marquee-select only tracks, only stations, or only trains to bulk-edit shared fields.
        </p>
      )}

      {/* ── Bulk: Tracks ────────────────────────────────────────────── */}
      {commonType === "track" && (() => {
        const tracks = ids.map((id) => state.tracks.find((t) => t.id === id)!).filter(Boolean);
        // Common directionality (shown if all same; otherwise show blank)
        const dirs = [...new Set(tracks.map((t) => t.directionality))];
        const commonDir = dirs.length === 1 ? dirs[0] : "";
        const pris = [...new Set(tracks.map((t) => String(t.restricted_to_priority ?? "")))];
        const commonPri = pris.length === 1 ? pris[0] : "";
        return (
          <>
            <p style={{ color: "#64748b", fontSize: 11, marginTop: 8 }}>
              Editing {tracks.length} tracks — only shared fields shown.
            </p>

            <label style={label} htmlFor="bulk-track-dir">Directionality</label>
            <select id="bulk-track-dir" style={select}
              value={commonDir}
              onChange={(e) => {
                tracks.forEach((t) =>
                  dispatch({ type: "UPDATE_TRACK", id: t.id,
                    patch: { directionality: e.target.value as TrackDirectionality } }));
              }}>
              {commonDir === "" && <option value="">(mixed — select to apply)</option>}
              <option value="bidirectional">Bidirectional ↔</option>
              <option value="one_way_forward">One-way → (forward)</option>
              <option value="one_way_reverse">One-way ← (reverse)</option>
            </select>

            <label style={label} htmlFor="bulk-track-priority">Min priority tier</label>
            <select id="bulk-track-priority" style={select}
              value={commonPri}
              onChange={(e) => {
                const v = e.target.value;
                const val: PriorityTier | null = v === "" ? null : (Number(v) as PriorityTier);
                tracks.forEach((t) =>
                  dispatch({ type: "UPDATE_TRACK", id: t.id,
                    patch: { restricted_to_priority: val } }));
              }}>
              {commonPri === "" && <option value="">(mixed — select to apply)</option>}
              <option value="">Unrestricted (all trains)</option>
              <option value="1">1 — Express only</option>
              <option value="2">2 — Ordinary + Express</option>
              <option value="3">3 — All</option>
            </select>
          </>
        );
      })()}

      {/* ── Bulk: Stations ──────────────────────────────────────────── */}
      {commonType === "station" && (() => {
        const stations = ids.map((id) => state.stations.find((s) => s.id === id)!).filter(Boolean);
        const stTypes  = [...new Set(stations.map((s) => s.station_type))];
        const commonSt = stTypes.length === 1 ? stTypes[0] : "";
        return (
          <>
            <p style={{ color: "#64748b", fontSize: 11, marginTop: 8 }}>
              Editing {stations.length} stations.
            </p>
            <label style={label} htmlFor="bulk-sta-type">Station type</label>
            <select id="bulk-sta-type" style={select}
              value={commonSt}
              onChange={(e) => {
                stations.forEach((s) =>
                  dispatch({ type: "UPDATE_STATION", id: s.id,
                    patch: { station_type: e.target.value as StationType } }));
              }}>
              {commonSt === "" && <option value="">(mixed — select to apply)</option>}
              <option value="through">Through</option>
              <option value="terminus">Terminus (dead-end)</option>
              <option value="junction_station">Junction station</option>
            </select>
          </>
        );
      })()}

      {/* ── Bulk: Signals ───────────────────────────────────────────── */}
      {commonType === "signal" && (() => {
        const signals = ids.map((id) => state.signals.find((s) => s.id === id)!).filter(Boolean);
        const sts = [...new Set(signals.map((s) => s.state))];
        const commonSt = sts.length === 1 ? sts[0] : "";
        return (
          <>
            <p style={{ color: "#64748b", fontSize: 11, marginTop: 8 }}>
              Editing {signals.length} signals.
            </p>
            <label style={label} htmlFor="bulk-sig-state">Signal state</label>
            <select id="bulk-sig-state" style={select}
              value={commonSt}
              onChange={(e) => {
                signals.forEach((s) =>
                  dispatch({ type: "UPDATE_SIGNAL", id: s.id,
                    patch: { state: e.target.value as SignalStateValue } }));
              }}>
              {commonSt === "" && <option value="">(mixed — select to apply)</option>}
              <option value="green">🟢 Green — clear</option>
              <option value="caution">🟡 Caution</option>
              <option value="red">🔴 Red — stop</option>
            </select>
          </>
        );
      })()}

      {/* ── Bulk: Trains ────────────────────────────────────────────── */}
      {commonType === "train" && (() => {
        const trains = ids.map((id) => state.trains.find((t) => t.id === id)!).filter(Boolean);
        const pris = [...new Set(trains.map((t) => t.priority))];
        const commonPri = pris.length === 1 ? String(pris[0]) : "";
        const duties = [...new Set(trains.map((t) => t.driver_duty_status))];
        const commonDuty = duties.length === 1 ? duties[0] : "";
        return (
          <>
            <p style={{ color: "#64748b", fontSize: 11, marginTop: 8 }}>
              Editing {trains.length} trains.
            </p>
            <label style={label} htmlFor="bulk-train-priority">Priority tier</label>
            <select id="bulk-train-priority" style={select}
              value={commonPri}
              onChange={(e) => {
                trains.forEach((t) =>
                  dispatch({ type: "UPDATE_TRAIN", id: t.id,
                    patch: { priority: Number(e.target.value) as PriorityTier } }));
              }}>
              {commonPri === "" && <option value="">(mixed — select to apply)</option>}
              <option value="1">1 — Express</option>
              <option value="2">2 — Ordinary</option>
              <option value="3">3 — Local</option>
            </select>

            <label style={label} htmlFor="bulk-train-duty">Driver duty status</label>
            <select id="bulk-train-duty" style={select}
              value={commonDuty}
              onChange={(e) => {
                trains.forEach((t) =>
                  dispatch({ type: "UPDATE_TRAIN", id: t.id,
                    patch: { driver_duty_status: e.target.value as Train["driver_duty_status"] } }));
              }}>
              {commonDuty === "" && <option value="">(mixed — select to apply)</option>}
              <option value="normal">Normal</option>
              <option value="over_duty">Over duty (fatigued)</option>
            </select>
          </>
        );
      })()}

      {/* Unknown / unsupported */}
      {commonType === "unknown" && (
        <p style={{ color: "#64748b", fontSize: 12, marginTop: 8 }}>
          Unknown element type. Cannot bulk-edit.
        </p>
      )}
    </div>
  );
}
