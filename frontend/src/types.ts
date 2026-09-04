/**
 * Shared TypeScript interfaces — mirrors backend/app/models.py field-for-field.
 *
 * Rules:
 *  - Field names must be identical to Pydantic model fields.
 *  - Optional Python fields  →  `field?: type | null`
 *  - datetime fields         →  ISO-8601 string (JSON serialisation boundary)
 *  - Pydantic enums          →  TypeScript string-literal union types
 *
 * When adding a field to models.py, add it here in the same position.
 * The eyeball-diff is intentional — it's the contract check.
 */

// ---------------------------------------------------------------------------
// Enums / literal union types
// ---------------------------------------------------------------------------

export type StationType = "terminus" | "through" | "junction_station";

export type TrackDirectionality =
  | "bidirectional"
  | "one_way_forward"
  | "one_way_reverse";

export type DriverDutyStatus = "normal" | "over_duty";

export type SignalStateValue = "green" | "red" | "caution";

/** 1 = express (highest), 2 = ordinary, 3 = local (lowest) */
export type PriorityTier = 1 | 2 | 3;

// ---------------------------------------------------------------------------
// Sub-models / value objects
// ---------------------------------------------------------------------------

/** 2-D canvas coordinate (pixels in editor coordinate space). */
export interface Point {
  x: number;
  y: number;
}

/** ISO-8601-compatible time range stored as plain strings. */
export interface MaintenanceWindow {
  start_iso: string;
  end_iso: string;
}

/**
 * Per-train, per-node timing record.
 *
 * scheduled_*  — fixed at authoring time.
 * expected_*   — live-updated by arbitration / ETA propagation.
 * actual_*     — set once the event occurs; then frozen.
 */
export interface ScheduleEntry {
  scheduled_arrival: string | null;
  scheduled_departure: string | null;
  expected_arrival: string | null;
  actual_arrival: string | null;
}

/**
 * One step in a train's route: the node to visit and which segment
 * to use to get there from the previous node.
 * segment_id is null for the very first hop (origin node).
 */
export interface RouteHop {
  node_id: string;
  segment_id: string | null;
}

// ---------------------------------------------------------------------------
// Core domain models
// ---------------------------------------------------------------------------

/**
 * A region of weather covering one or more blocks.
 * intensity: 0.0 (no effect) → 1.0 (full speed-reduction).
 */
export interface WeatherCell {
  id: string;
  intensity: number;
  affected_block_ids: string[];
}

/**
 * A per-block lineside signal.
 * controlled_by_junction_id: null = autonomous (block-occupancy sensor).
 */
export interface SignalState {
  id: string;
  block_id: string;
  state: SignalStateValue;
  controlled_by_junction_id: string | null;
}

/**
 * Smallest track unit (~1 km). Mutually exclusive occupancy.
 *
 * occupied_by       — train_id of occupying train, null if clear.
 * speed_restriction — km/h cap; null = no restriction.
 * maintenance_window — null = no window.
 */
export interface Block {
  id: string;
  segment_id: string;
  length_km: number;
  occupied_by: string | null;
  weather_cell_id: string | null;
  speed_restriction: number | null;
  maintenance_window: MaintenanceWindow | null;
}

/**
 * An ordered, non-branching chain of Blocks between two Nodes.
 * ordered_block_ids[0] is adjacent to start_node_id.
 */
export interface Segment {
  id: string;
  start_node_id: string;
  end_node_id: string;
  ordered_block_ids: string[];
}

/**
 * A merge/crossing point with ≥2 segments attached.
 * signal_states: approach segment_id → signal_state id.
 */
export interface Junction {
  id: string;
  connected_segment_ids: string[];
  signal_states: Record<string, string>;
}

/**
 * A node where trains schedule arrivals and departures.
 * platform_tracks: ordered list of Track ids.
 * rotation_deg: visual rotation on canvas.
 */
export interface Station {
  id: string;
  name: string;
  platform_tracks: string[];
  rotation_deg: number;
  station_type: StationType;
}

/**
 * Geometric/visual representation of a Segment's path.
 * This is a RENDERING concern — occupancy truth lives in Block.
 *
 * geometry: ordered control points (polyline or Bézier; renderer decides).
 * restricted_to_priority: null = unrestricted.
 */
export interface Track {
  id: string;
  segment_id: string;
  geometry: Point[];
  directionality: TrackDirectionality;
  restricted_to_priority: PriorityTier | null;
}

/**
 * A train entity with its full route, schedule, and physical state.
 *
 * priority                  — 1=express, 2=ordinary, 3=local.
 * driver_duty_status        — over_duty overrides numeric priority at arbitration.
 * route                     — ordered RouteHops from origin to destination.
 * schedule                  — node_id → ScheduleEntry.
 * current_block_id          — null when not yet on network / journey complete.
 * current_position_in_block — 0.0 (block entry) to 1.0 (block exit).
 */
export interface Train {
  id: string;
  name: string;
  color: string;
  priority: PriorityTier;
  num_carriages: number;
  max_speed: number;
  avg_speed: number;
  route: RouteHop[];
  schedule: Record<string, ScheduleEntry>;
  driver_duty_status: DriverDutyStatus;
  current_block_id: string | null;
  current_position_in_block: number;
}
