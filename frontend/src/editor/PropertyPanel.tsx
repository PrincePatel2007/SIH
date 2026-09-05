/**
 * PropertyPanel.tsx — Right-side panel that shows editable fields for the
 * currently selected element.
 *
 * Uses the ACTUAL enum values from models.py:
 *   - StationType: terminus / through / junction_station
 *   - TrackDirectionality: bidirectional / one_way_forward / one_way_reverse
 *   - PriorityTier: 1 (express) / 2 (ordinary) / 3 (local)
 *   - SignalStateValue: green / red / caution  ← three states, not two
 *
 * Wired to local React state only (Phase 6). Persistence comes in Phase 7.
 */

import type { CSSProperties } from "react";
import { useEditorStore, type JunctionWithPos, type StationWithPos } from "./store";
import type {
  StationType,
  TrackDirectionality,
  PriorityTier,
  SignalStateValue,
} from "../types";

// ---------------------------------------------------------------------------
// Styles (inline — no external CSS file needed for this panel)
// ---------------------------------------------------------------------------

const panel: CSSProperties = {
  width: 260,
  minHeight: "100%",
  background: "#0f172a",
  borderLeft: "1px solid #1e293b",
  color: "#e2e8f0",
  fontFamily: "'Inter', system-ui, sans-serif",
  fontSize: 13,
  overflowY: "auto",
  flexShrink: 0,
};

const section: CSSProperties = {
  padding: "16px 16px 0",
};

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

const empty: CSSProperties = {
  padding: 20,
  color: "#475569",
  textAlign: "center",
  fontSize: 12,
};

const SIGNAL_DOT: Record<string, CSSProperties> = {
  green: { display: "inline-block", width: 10, height: 10, borderRadius: "50%", background: "#22c55e", marginRight: 6 },
  red: { display: "inline-block", width: 10, height: 10, borderRadius: "50%", background: "#ef4444", marginRight: 6 },
  caution: { display: "inline-block", width: 10, height: 10, borderRadius: "50%", background: "#f59e0b", marginRight: 6 },
};

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
        <div style={{ padding: 16 }}>
          <h2 style={h2}>Multiple selected</h2>
          <span style={badge}>{selection.size} elements</span>
          <p style={{ color: "#64748b", fontSize: 12 }}>
            Select a single element to edit its properties.
          </p>
        </div>
      </aside>
    );
  }

  const id = [...selection][0];

  // Identify which entity type this is.
  const track = state.tracks.find((t) => t.id === id);
  const station = state.stations.find((s) => s.id === id);
  const junction = state.junctions.find((j) => j.id === id);
  const signal = state.signals.find((s) => s.id === id);

  return (
    <aside style={panel} id="property-panel" aria-label="Property Panel">
      {track && (
        <div style={section}>
          <h2 style={h2}>Track</h2>
          <span style={badge}>{track.id}</span>

          <label style={label} htmlFor="pp-track-directionality">Directionality</label>
          <select
            id="pp-track-directionality"
            style={select}
            value={track.directionality}
            onChange={(e) => {
              const val = e.target.value as TrackDirectionality;
              dispatch({ type: "UPDATE_TRACK", id: track.id, patch: { directionality: val } });
            }}
          >
            <option value="bidirectional">Bidirectional ↔</option>
            <option value="one_way_forward">One-way → (forward)</option>
            <option value="one_way_reverse">One-way ← (reverse)</option>
          </select>

          <label style={label} htmlFor="pp-track-priority">
            Min priority tier allowed
            <span style={{ fontWeight: 400, color: "#64748b", marginLeft: 4 }}>
              (1=express only · 3=anyone)
            </span>
          </label>
          <select
            id="pp-track-priority"
            style={select}
            value={track.restricted_to_priority ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              const val: PriorityTier | null = v === "" ? null : (Number(v) as PriorityTier);
              dispatch({ type: "UPDATE_TRACK", id: track.id, patch: { restricted_to_priority: val } });
            }}
          >
            <option value="">Unrestricted (all trains)</option>
            <option value="1">1 — Express only</option>
            <option value="2">2 — Ordinary + Express</option>
            <option value="3">3 — All (local permitted)</option>
          </select>

          <label style={label}>Points in geometry</label>
          <p style={{ ...badge, display: "block" }}>{track.geometry.length} points</p>
        </div>
      )}

      {station && (
        <div style={section}>
          <h2 style={h2}>Station</h2>
          <span style={badge}>{station.id}</span>

          <label style={label} htmlFor="pp-sta-name">Name</label>
          <input
            id="pp-sta-name"
            style={input}
            type="text"
            value={station.name}
            onChange={(e) =>
              dispatch({ type: "UPDATE_STATION", id: station.id, patch: { name: e.target.value } })
            }
          />

          <label style={label} htmlFor="pp-sta-type">Station type</label>
          <select
            id="pp-sta-type"
            style={select}
            value={station.station_type}
            onChange={(e) =>
              dispatch({
                type: "UPDATE_STATION",
                id: station.id,
                patch: { station_type: e.target.value as StationType },
              })
            }
          >
            <option value="through">Through</option>
            <option value="terminus">Terminus (dead-end)</option>
            <option value="junction_station">Junction station</option>
          </select>

          <label style={label} htmlFor="pp-sta-rotation">Rotation (°)</label>
          <input
            id="pp-sta-rotation"
            style={input}
            type="number"
            min={0}
            max={359}
            step={15}
            value={station.rotation_deg}
            onChange={(e) =>
              dispatch({
                type: "UPDATE_STATION",
                id: station.id,
                patch: { rotation_deg: Number(e.target.value) },
              })
            }
          />

          <label style={label} htmlFor="pp-sta-platforms">Platform count</label>
          <input
            id="pp-sta-platforms"
            style={input}
            type="number"
            min={1}
            max={8}
            value={station.platform_tracks.length || 1}
            readOnly
          />
          <p style={{ color: "#475569", fontSize: 11, marginTop: -8, marginBottom: 12 }}>
            Platform tracks are linked by connecting track endpoints to this station.
          </p>
        </div>
      )}

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
              {junction.connected_segment_ids.map((sid) => (
                <li key={sid}>{sid}</li>
              ))}
            </ul>
          )}

          <label style={label}>Approach signals</label>
          {Object.keys((junction as JunctionWithPos).signal_states).length === 0 ? (
            <p style={{ color: "#475569", fontSize: 12 }}>
              Auto-created by backend when tracks are connected (Phase 7).
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

      {signal && (
        <div style={section}>
          <h2 style={h2}>Signal</h2>
          <span style={badge}>{signal.id}</span>

          <label style={label}>Block</label>
          <p style={badge}>{signal.block_id || "—"}</p>

          <label style={label} htmlFor="pp-sig-state">State</label>
          <select
            id="pp-sig-state"
            style={select}
            value={signal.state}
            onChange={(e) =>
              dispatch({
                type: "UPDATE_SIGNAL",
                id: signal.id,
                patch: { state: e.target.value as SignalStateValue },
              })
            }
          >
            <option value="green">🟢 Green — clear</option>
            <option value="caution">🟡 Caution — approach slow</option>
            <option value="red">🔴 Red — stop</option>
          </select>
          {/* Colour preview */}
          <div style={{ display: "flex", alignItems: "center", marginBottom: 12 }}>
            <span style={SIGNAL_DOT[signal.state]} />
            <span style={{ color: "#94a3b8", fontSize: 12 }}>
              {signal.state.charAt(0).toUpperCase() + signal.state.slice(1)}
            </span>
          </div>

          <label style={label} htmlFor="pp-sig-junction">Controlling junction</label>
          <select
            id="pp-sig-junction"
            style={select}
            value={signal.controlled_by_junction_id ?? ""}
            onChange={(e) =>
              dispatch({
                type: "UPDATE_SIGNAL",
                id: signal.id,
                patch: { controlled_by_junction_id: e.target.value || null },
              })
            }
          >
            <option value="">Free-standing (no junction)</option>
            {state.junctions.map((j) => (
              <option key={j.id} value={j.id}>
                {j.id}
              </option>
            ))}
          </select>
          <p style={{ color: "#475569", fontSize: 11, marginTop: -8 }}>
            Junction-approach signals are auto-managed by the backend.
            Only use this for free-standing block signals.
          </p>
        </div>
      )}
    </aside>
  );
}
