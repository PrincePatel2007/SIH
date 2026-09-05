/**
 * store.tsx — in-memory editor state store (Phase 7).
 *
 * New in Phase 7:
 *  - trains (TrainWithPos)
 *  - segmentOverrides (speed cap + maintenance window per segment)
 *  - routeBuilding ephemeral state (add_route_hop validates connectivity inline)
 *  - LOAD_LAYOUT (bulk-replace from a loaded LayoutPayload)
 *  - UPDATE_TRAIN_DWELL (per-node dwell override for schedule authoring)
 */

import {
  createContext,
  useContext,
  useReducer,
  type Dispatch,
  type ReactNode,
} from "react";
import type {
  Track,
  Segment,
  Station,
  Junction,
  SignalState,
  Train,
  RouteHop,
  MaintenanceWindow,
} from "../types";
import type { Point } from "../types";
import { newId } from "./utils/ids";
import { findSnap, SNAP_DISTANCE_PX } from "./utils/geometry";

// ---------------------------------------------------------------------------
// Internal extended types — carry canvas positions (not serialised)
// ---------------------------------------------------------------------------

export interface JunctionWithPos extends Junction {
  _pos: Point;
}

export interface StationWithPos extends Station {
  _pos: Point;
}

export interface SignalWithPos extends SignalState {
  _pos: Point;
}

export interface TrainWithPos extends Train {
  _pos: Point;
  /** node_id → dwell minutes (editor-authored, used by backend build_schedule) */
  _dwell: Record<string, number>;
}

// ---------------------------------------------------------------------------
// Segment override (speed cap + maintenance window authored in editor)
// ---------------------------------------------------------------------------

export interface SegmentOverride {
  segment_id: string;
  speed_restriction: number | null;
  maintenance_window: MaintenanceWindow | null;
}

// ---------------------------------------------------------------------------
// Route building state
// ---------------------------------------------------------------------------

export interface RouteBuildingState {
  trainId: string;
  hops: RouteHop[];
  /** True when the last ADD_ROUTE_HOP was rejected (nodes not connected). */
  invalidLastHop: boolean;
}

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

export interface EditorState {
  tracks: Track[];
  segments: Segment[];
  stations: Station[];
  junctions: Junction[];
  signals: SignalState[];
  trains: Train[];
  segmentOverrides: SegmentOverride[];
  selection: Set<string>;
  routeBuilding: RouteBuildingState | null;
}

const initialState: EditorState = {
  tracks: [],
  segments: [],
  stations: [],
  junctions: [],
  signals: [],
  trains: [],
  segmentOverrides: [],
  selection: new Set(),
  routeBuilding: null,
};

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

export type EditorAction =
  // Track
  | { type: "ADD_TRACK"; points: Point[] }
  | { type: "UPDATE_TRACK"; id: string; patch: Partial<Pick<Track, "directionality" | "restricted_to_priority">> }
  | { type: "UPDATE_TRACK_POINT"; trackId: string; pointIndex: number; point: Point }
  | { type: "FINISH_TRACK_POINT_DRAG"; trackId: string; pointIndex: number; point: Point }
  // Station
  | { type: "ADD_STATION"; position: Point; name?: string }
  | { type: "UPDATE_STATION"; id: string; patch: Partial<Pick<Station, "name" | "rotation_deg" | "station_type">> }
  | { type: "MOVE_STATION"; id: string; position: Point }
  // Junction
  | { type: "ADD_JUNCTION"; position: Point }
  | { type: "UPDATE_JUNCTION"; id: string; patch: Partial<Junction> }
  | { type: "MOVE_JUNCTION"; id: string; position: Point }
  // Signal
  | { type: "ADD_SIGNAL"; position: Point; blockId: string; junctionId?: string }
  | { type: "UPDATE_SIGNAL"; id: string; patch: Partial<Pick<SignalState, "state" | "controlled_by_junction_id">> }
  | { type: "MOVE_SIGNAL"; id: string; position: Point }
  // Train
  | { type: "ADD_TRAIN"; position: Point; name?: string }
  | {
      type: "UPDATE_TRAIN";
      id: string;
      patch: Partial<Pick<Train, "name" | "color" | "num_carriages" | "priority" | "max_speed" | "avg_speed" | "driver_duty_status">>;
    }
  | { type: "MOVE_TRAIN"; id: string; position: Point }
  // Route building
  | { type: "START_ROUTE_BUILD"; trainId: string; originNodeId: string }
  | { type: "ADD_ROUTE_HOP"; nodeId: string }
  | { type: "FINISH_ROUTE_BUILD" }
  | { type: "CANCEL_ROUTE_BUILD" }
  // Per-node dwell
  | { type: "UPDATE_TRAIN_DWELL"; trainId: string; nodeId: string; minutes: number }
  // Segment overrides
  | {
      type: "UPDATE_SEGMENT_OVERRIDE";
      segmentId: string;
      patch: Partial<Pick<SegmentOverride, "speed_restriction" | "maintenance_window">>;
    }
  // Selection
  | { type: "SET_SELECTION"; ids: string[] }
  | { type: "CLEAR_SELECTION" }
  | { type: "TOGGLE_SELECTION"; id: string }
  // Delete
  | { type: "REMOVE_ELEMENT"; id: string }
  // Load
  | { type: "LOAD_LAYOUT"; payload: LoadedLayout };

/** Shape coming back from GET /api/simulations/{name} (canvas positions included). */
export interface LoadedLayout {
  tracks: Track[];
  segments: Segment[];
  stations: Station[];
  junctions: Junction[];
  signals: SignalState[];
  trains: Train[];
  segmentOverrides?: SegmentOverride[];
  station_positions: Record<string, { x: number; y: number }>;
  junction_positions: Record<string, { x: number; y: number }>;
  signal_positions: Record<string, { x: number; y: number }>;
  train_positions: Record<string, { x: number; y: number }>;
  authoring_hints?: Array<{ train_id: string; origin_departure_iso: string; dwell_minutes: Record<string, number> }>;
}

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

function collectEndpoints(tracks: Track[], excludeTrackId?: string): Point[] {
  const pts: Point[] = [];
  for (const t of tracks) {
    if (t.id === excludeTrackId) continue;
    if (t.geometry.length > 0) pts.push(t.geometry[0]);
    if (t.geometry.length > 1) pts.push(t.geometry[t.geometry.length - 1]);
  }
  return pts;
}

function maybeSnap(
  tracks: Track[],
  trackId: string,
  pointIndex: number,
  point: Point
): Point {
  const track = tracks.find((t) => t.id === trackId);
  if (!track) return point;
  const isEndpoint = pointIndex === 0 || pointIndex === track.geometry.length - 1;
  if (!isEndpoint) return point;
  const anchors = collectEndpoints(tracks, trackId);
  return findSnap(point, anchors) ?? point;
}

function mergeJunctions(
  state: EditorState,
  trackId: string,
  finalPoint: Point
): Junction[] {
  let junctions = [...state.junctions];
  const movedTrack = state.tracks.find((t) => t.id === trackId);
  if (!movedTrack) return junctions;
  const movedSegId = movedTrack.segment_id;

  const existing = junctions.find((j) => {
    const pos = (j as JunctionWithPos)._pos;
    if (!pos) return false;
    const dx = pos.x - finalPoint.x;
    const dy = pos.y - finalPoint.y;
    return Math.sqrt(dx * dx + dy * dy) < SNAP_DISTANCE_PX;
  }) as JunctionWithPos | undefined;

  if (!existing) {
    const newJct: JunctionWithPos = {
      id: newId("jct"),
      connected_segment_ids: [movedSegId],
      signal_states: {},
      _pos: finalPoint,
    };
    junctions = [...junctions, newJct];
  } else {
    if (!existing.connected_segment_ids.includes(movedSegId)) {
      junctions = junctions.map((j) =>
        j.id === existing!.id
          ? { ...j, connected_segment_ids: [...j.connected_segment_ids, movedSegId] }
          : j
      );
    }
  }
  return junctions;
}

/**
 * Find a segment connecting nodeA ↔ nodeB (either direction).
 * Returns the segment or undefined if none exists.
 */
function findConnectingSegment(
  segments: Segment[],
  nodeA: string,
  nodeB: string
): Segment | undefined {
  return segments.find(
    (s) =>
      (s.start_node_id === nodeA && s.end_node_id === nodeB) ||
      (s.start_node_id === nodeB && s.end_node_id === nodeA)
  );
}

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

function reducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {

    // ── Track ─────────────────────────────────────────────────────────────────

    case "ADD_TRACK": {
      if (action.points.length < 2) return state;
      const trackId = newId("trk");
      const segId = newId("seg");
      const newTrack: Track = {
        id: trackId,
        segment_id: segId,
        geometry: action.points,
        directionality: "bidirectional",
        restricted_to_priority: null,
      };
      const newSeg: Segment = {
        id: segId,
        start_node_id: "",
        end_node_id: "",
        ordered_block_ids: [],
      };
      return {
        ...state,
        tracks: [...state.tracks, newTrack],
        segments: [...state.segments, newSeg],
      };
    }

    case "UPDATE_TRACK": {
      const tracks = state.tracks.map((t) =>
        t.id === action.id ? { ...t, ...action.patch } : t
      );
      return { ...state, tracks };
    }

    case "UPDATE_TRACK_POINT": {
      const tracks = state.tracks.map((t) => {
        if (t.id !== action.trackId) return t;
        const geometry = t.geometry.map((p, i) =>
          i === action.pointIndex ? action.point : p
        );
        return { ...t, geometry };
      });
      return { ...state, tracks };
    }

    case "FINISH_TRACK_POINT_DRAG": {
      const snapped = maybeSnap(state.tracks, action.trackId, action.pointIndex, action.point);
      const tracks = state.tracks.map((t) => {
        if (t.id !== action.trackId) return t;
        const geometry = t.geometry.map((p, i) =>
          i === action.pointIndex ? snapped : p
        );
        return { ...t, geometry };
      });
      const stateWithTrack = { ...state, tracks };
      const trk = state.tracks.find((t) => t.id === action.trackId);
      const isEndpoint = trk
        ? action.pointIndex === 0 || action.pointIndex === trk.geometry.length - 1
        : false;
      if (isEndpoint) {
        const junctions = mergeJunctions(stateWithTrack, action.trackId, snapped);
        return { ...stateWithTrack, junctions };
      }
      return stateWithTrack;
    }

    // ── Station ───────────────────────────────────────────────────────────────

    case "ADD_STATION": {
      const id = newId("sta");
      const station: StationWithPos = {
        id,
        name: action.name ?? `Station ${id.slice(-4)}`,
        platform_tracks: [],
        rotation_deg: 0,
        station_type: "through",
        _pos: action.position,
      };
      return { ...state, stations: [...state.stations, station] };
    }

    case "UPDATE_STATION": {
      const stations = state.stations.map((s) =>
        s.id === action.id ? { ...s, ...action.patch } : s
      );
      return { ...state, stations };
    }

    case "MOVE_STATION": {
      const stations = state.stations.map((s) =>
        s.id === action.id ? { ...s, _pos: action.position } : s
      );
      return { ...state, stations };
    }

    // ── Junction ──────────────────────────────────────────────────────────────

    case "ADD_JUNCTION": {
      const id = newId("jct");
      const jct: JunctionWithPos = {
        id,
        connected_segment_ids: [],
        signal_states: {},
        _pos: action.position,
      };
      return { ...state, junctions: [...state.junctions, jct] };
    }

    case "UPDATE_JUNCTION": {
      const junctions = state.junctions.map((j) =>
        j.id === action.id ? { ...j, ...action.patch } : j
      );
      return { ...state, junctions };
    }

    case "MOVE_JUNCTION": {
      const junctions = state.junctions.map((j) =>
        j.id === action.id ? { ...j, _pos: action.position } : j
      );
      return { ...state, junctions };
    }

    // ── Signal ────────────────────────────────────────────────────────────────

    case "ADD_SIGNAL": {
      const id = newId("sig");
      const signal: SignalWithPos = {
        id,
        block_id: action.blockId,
        state: "green",
        controlled_by_junction_id: action.junctionId ?? null,
        _pos: action.position,
      };
      return { ...state, signals: [...state.signals, signal] };
    }

    case "UPDATE_SIGNAL": {
      const signals = state.signals.map((s) =>
        s.id === action.id ? { ...s, ...action.patch } : s
      );
      return { ...state, signals };
    }

    case "MOVE_SIGNAL": {
      const signals = state.signals.map((s) =>
        s.id === action.id ? { ...s, _pos: action.position } : s
      );
      return { ...state, signals };
    }

    // ── Train ─────────────────────────────────────────────────────────────────

    case "ADD_TRAIN": {
      const id = newId("trn");
      const train: TrainWithPos = {
        id,
        name: action.name ?? `Train ${id.slice(-4)}`,
        color: "#3b82f6",
        priority: 2,
        num_carriages: 4,
        max_speed: 120,
        avg_speed: 80,
        route: [],
        schedule: {},
        driver_duty_status: "normal",
        current_block_id: null,
        current_position_in_block: 0.0,
        _pos: action.position,
        _dwell: {},
      };
      return { ...state, trains: [...state.trains, train] };
    }

    case "UPDATE_TRAIN": {
      const trains = state.trains.map((t) =>
        t.id === action.id ? { ...t, ...action.patch } : t
      );
      return { ...state, trains };
    }

    case "MOVE_TRAIN": {
      const trains = state.trains.map((t) =>
        t.id === action.id ? { ...t, _pos: action.position } : t
      );
      return { ...state, trains };
    }

    // ── Route building ────────────────────────────────────────────────────────

    case "START_ROUTE_BUILD": {
      return {
        ...state,
        routeBuilding: {
          trainId: action.trainId,
          hops: [{ node_id: action.originNodeId, segment_id: null }],
          invalidLastHop: false,
        },
      };
    }

    case "ADD_ROUTE_HOP": {
      const rb = state.routeBuilding;
      if (!rb) return state;

      const prevHop = rb.hops[rb.hops.length - 1];
      const prevNodeId = prevHop.node_id;
      const newNodeId = action.nodeId;

      // Don't allow clicking the same node twice in a row.
      if (newNodeId === prevNodeId) return state;

      const connecting = findConnectingSegment(state.segments, prevNodeId, newNodeId);
      if (!connecting) {
        // Reject — nodes are not connected by any segment.
        return {
          ...state,
          routeBuilding: { ...rb, invalidLastHop: true },
        };
      }

      return {
        ...state,
        routeBuilding: {
          ...rb,
          hops: [...rb.hops, { node_id: newNodeId, segment_id: connecting.id }],
          invalidLastHop: false,
        },
      };
    }

    case "FINISH_ROUTE_BUILD": {
      const rb = state.routeBuilding;
      if (!rb || rb.hops.length < 1) return { ...state, routeBuilding: null };
      const trains = state.trains.map((t) =>
        t.id === rb.trainId ? { ...t, route: rb.hops } : t
      );
      return { ...state, trains, routeBuilding: null };
    }

    case "CANCEL_ROUTE_BUILD":
      return { ...state, routeBuilding: null };

    // ── Per-node dwell ────────────────────────────────────────────────────────

    case "UPDATE_TRAIN_DWELL": {
      const trains = state.trains.map((t) => {
        if (t.id !== action.trainId) return t;
        const twp = t as TrainWithPos;
        const dwell = { ...twp._dwell, [action.nodeId]: action.minutes };
        return { ...twp, _dwell: dwell };
      });
      return { ...state, trains };
    }

    // ── Segment overrides ─────────────────────────────────────────────────────

    case "UPDATE_SEGMENT_OVERRIDE": {
      const existing = state.segmentOverrides.find(
        (o) => o.segment_id === action.segmentId
      );
      if (existing) {
        const segmentOverrides = state.segmentOverrides.map((o) =>
          o.segment_id === action.segmentId ? { ...o, ...action.patch } : o
        );
        return { ...state, segmentOverrides };
      }
      return {
        ...state,
        segmentOverrides: [
          ...state.segmentOverrides,
          { segment_id: action.segmentId, speed_restriction: null, maintenance_window: null, ...action.patch },
        ],
      };
    }

    // ── Selection ─────────────────────────────────────────────────────────────

    case "SET_SELECTION":
      return { ...state, selection: new Set(action.ids) };

    case "CLEAR_SELECTION":
      return { ...state, selection: new Set() };

    case "TOGGLE_SELECTION": {
      const next = new Set(state.selection);
      if (next.has(action.id)) next.delete(action.id);
      else next.add(action.id);
      return { ...state, selection: next };
    }

    // ── Delete ────────────────────────────────────────────────────────────────

    case "REMOVE_ELEMENT": {
      const id = action.id;
      const next = new Set(state.selection);
      next.delete(id);
      const removedTrack = state.tracks.find((t) => t.id === id);
      return {
        ...state,
        tracks: state.tracks.filter((t) => t.id !== id),
        segments: state.segments.filter(
          (s) => s.id !== id && !(removedTrack && removedTrack.segment_id === s.id)
        ),
        stations: state.stations.filter((s) => s.id !== id),
        junctions: state.junctions.filter((j) => j.id !== id),
        signals: state.signals.filter((s) => s.id !== id),
        trains: state.trains.filter((t) => t.id !== id),
        selection: next,
      };
    }

    // ── Load layout ───────────────────────────────────────────────────────────

    case "LOAD_LAYOUT": {
      const p = action.payload;

      // Rehydrate canvas positions into the _pos fields.
      const stations = p.stations.map((s) => {
        const pos = p.station_positions[s.id];
        return pos ? { ...s, _pos: pos } : { ...s, _pos: { x: 100, y: 100 } };
      });
      const junctions = p.junctions.map((j) => {
        const pos = p.junction_positions[j.id];
        return pos ? { ...j, _pos: pos } : { ...j, _pos: { x: 200, y: 200 } };
      });
      const signals = p.signals.map((s) => {
        const pos = p.signal_positions[s.id];
        return pos ? { ...s, _pos: pos } : { ...s, _pos: { x: 300, y: 300 } };
      });
      const trains = p.trains.map((t) => {
        const pos = p.train_positions[t.id];
        const hint = p.authoring_hints?.find((h) => h.train_id === t.id);
        return {
          ...t,
          _pos: pos ?? { x: 150, y: 150 },
          _dwell: hint?.dwell_minutes ?? {},
        };
      });

      return {
        tracks: p.tracks,
        segments: p.segments,
        stations,
        junctions,
        signals,
        trains,
        segmentOverrides: p.segmentOverrides ?? [],
        selection: new Set(),
        routeBuilding: null,
      };
    }

    default:
      return state;
  }
}

// ---------------------------------------------------------------------------
// Context
// ---------------------------------------------------------------------------

const StoreContext = createContext<{
  state: EditorState;
  dispatch: Dispatch<EditorAction>;
} | null>(null);

export function EditorStoreProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reducer, initialState);
  return (
    <StoreContext.Provider value={{ state, dispatch }}>
      {children}
    </StoreContext.Provider>
  );
}

export function useEditorStore() {
  const ctx = useContext(StoreContext);
  if (!ctx) throw new Error("useEditorStore must be used inside EditorStoreProvider");
  return ctx;
}
