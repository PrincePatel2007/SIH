/**
 * store.tsx — in-memory editor state store (Phase 6).
 *
 * Uses React context + useReducer. No backend calls — everything lives in
 * component state until Phase 7 wires up save/load.
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
  TrackDirectionality,
  PriorityTier,
  Segment,
  Station,
  Junction,
  SignalState,
  SignalStateValue,
  StationType,
} from "../types";
import type { Point } from "../types";
import { newId } from "./utils/ids";
import { findSnap, SNAP_DISTANCE_PX } from "./utils/geometry";

// ---------------------------------------------------------------------------
// Internal extended types — carry canvas position for rendering
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

// ---------------------------------------------------------------------------
// State shape
// ---------------------------------------------------------------------------

export interface EditorState {
  tracks: Track[];
  segments: Segment[];
  stations: Station[];
  junctions: Junction[];
  signals: SignalState[];
  selection: Set<string>;
}

const initialState: EditorState = {
  tracks: [],
  segments: [],
  stations: [],
  junctions: [],
  signals: [],
  selection: new Set(),
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
  // Selection
  | { type: "SET_SELECTION"; ids: string[] }
  | { type: "CLEAR_SELECTION" }
  | { type: "TOGGLE_SELECTION"; id: string }
  // Delete
  | { type: "REMOVE_ELEMENT"; id: string };

// ---------------------------------------------------------------------------
// Helpers
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

  let existing = junctions.find((j) => {
    const pos = (j as JunctionWithPos)._pos;
    if (!pos) return false;
    const dx = pos.x - finalPoint.x;
    const dy = pos.y - finalPoint.y;
    return Math.sqrt(dx * dx + dy * dy) < SNAP_DISTANCE_PX;
  }) as JunctionWithPos | undefined;

  if (!existing) {
    const jctId = newId("jct");
    const newJct: JunctionWithPos = {
      id: jctId,
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

// ---------------------------------------------------------------------------
// Reducer
// ---------------------------------------------------------------------------

function reducer(state: EditorState, action: EditorAction): EditorState {
  switch (action.type) {

    // ── Track ────────────────────────────────────────────────────────────────

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

    // ── Station ──────────────────────────────────────────────────────────────

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

    // ── Junction ─────────────────────────────────────────────────────────────

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

    // ── Signal ───────────────────────────────────────────────────────────────

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

    // ── Selection ────────────────────────────────────────────────────────────

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

    // ── Delete ───────────────────────────────────────────────────────────────

    case "REMOVE_ELEMENT": {
      const id = action.id;
      // Remove from whichever collection contains it; clean up selection too.
      const next = new Set(state.selection);
      next.delete(id);
      return {
        ...state,
        tracks:    state.tracks.filter((t) => t.id !== id),
        segments:  state.segments.filter((s) => s.id !== id &&
                     !state.tracks.find((t) => t.id === id && t.segment_id === s.id)),
        stations:  state.stations.filter((s) => s.id !== id),
        junctions: state.junctions.filter((j) => j.id !== id),
        signals:   state.signals.filter((s) => s.id !== id),
        selection: next,
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
