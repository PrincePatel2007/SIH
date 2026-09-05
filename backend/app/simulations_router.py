"""
simulations_router.py — POST/GET /api/simulations/{name}

Save pipeline (POST):
  1. Deserialize LayoutPayload.
  2. Build a NetworkGraph from tracks/segments/stations/junctions.
  3. For each train, call build_schedule() using the author's origin_departure + avg_speed
     to fill scheduled_arrival/departure at EVERY route node.
  4. Apply per-node dwell overrides.
  5. Run validate_schedule() across all trains.
  6. Return 400 with { "errors": [...] } if any violations found.
  7. Write simulations/{name}.json on success.

Load pipeline (GET):
  Read simulations/{name}.json, return LayoutPayload verbatim.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Path
from pydantic import BaseModel, Field

from app.models import (
    Block,
    Junction,
    MaintenanceWindow,
    Point,
    RouteHop,
    ScheduleEntry,
    Segment,
    SignalState,
    Station,
    Track,
    Train,
)
from app.network import (
    NetworkGraph,
    NetworkError,
    BlockRuntime,
    SegmentRuntime,
    TrackRuntime,
    StationRuntime,
    JunctionRuntime,
)
from app.train import TrainRuntime, ScheduleError, build_schedule, validate_schedule


router = APIRouter(prefix="/api/simulations", tags=["simulations"])

# Simulations are written here (relative to the backend directory).
SIMULATIONS_DIR = pathlib.Path(__file__).parent.parent / "simulations"
SIMULATIONS_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------


class SegmentOverride(BaseModel):
    """Per-segment speed / maintenance overrides authored in the editor."""

    segment_id: str
    speed_restriction: Optional[float] = Field(default=None, gt=0.0)
    maintenance_window: Optional[MaintenanceWindow] = None


class TrainAuthoringHint(BaseModel):
    """
    What the editor explicitly authored for one train's schedule.

    The backend uses this (plus avg_speed and segment distances) to call
    build_schedule() and fill scheduled_arrival/departure at every node.
    """

    train_id: str
    origin_departure_iso: str  # ISO-8601, e.g. "2026-09-06T06:00:00Z"
    dwell_minutes: dict[str, float] = Field(
        default_factory=dict,
        description="node_id → minutes of dwell at that stop (0 = no dwell)",
    )


class LayoutPayload(BaseModel):
    """
    Full serialised canvas state — the save/load contract between editor and backend.

    Canvas positions (station_positions, junction_positions, signal_positions,
    train_positions) are stored for round-trip fidelity but are ignored by the
    simulation engine.
    """

    tracks: list[Track] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    stations: list[Station] = Field(default_factory=list)
    junctions: list[Junction] = Field(default_factory=list)
    signals: list[SignalState] = Field(default_factory=list)
    trains: list[Train] = Field(default_factory=list)
    authoring_hints: list[TrainAuthoringHint] = Field(default_factory=list)
    segment_overrides: list[SegmentOverride] = Field(default_factory=list)
    # Canvas positions — opaque dicts, preserved verbatim on round-trip.
    station_positions: dict[str, dict] = Field(default_factory=dict)
    junction_positions: dict[str, dict] = Field(default_factory=dict)
    signal_positions: dict[str, dict] = Field(default_factory=dict)
    train_positions: dict[str, dict] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_network(payload: LayoutPayload) -> NetworkGraph:
    """
    Construct a NetworkGraph from the payload's structural elements.

    Stations and junctions are registered as nodes first; then segments are
    added with their blocks.  Tracks are registered last (visual layer only).
    """
    graph = NetworkGraph()

    # Register stations.
    for sta in payload.stations:
        graph._stations[sta.id] = StationRuntime(sta)
        graph._register_node(sta.id)

    # Register junctions.
    for jct in payload.junctions:
        graph._junctions[jct.id] = JunctionRuntime(jct)
        graph._register_node(jct.id)

    # Build a quick override map.
    override_map: dict[str, SegmentOverride] = {
        o.segment_id: o for o in payload.segment_overrides
    }

    # Register segments + blocks.

    for seg in payload.segments:
        override = override_map.get(seg.id)
        # Create blocks for this segment (1 block per segment for simplicity;
        # the engine's block-length model is set during runtime add_track calls,
        # but for validation we need at least a placeholder block with a length).
        block_length_km = 1.0  # default; real length comes from network.add_track at runtime
        num_blocks = max(1, len(seg.ordered_block_ids))
        blocks: list[BlockRuntime] = []
        for i, blk_id in enumerate(seg.ordered_block_ids):
            blk_model = Block(
                id=blk_id,
                segment_id=seg.id,
                length_km=block_length_km,
                speed_restriction=(override.speed_restriction if override else None),
                maintenance_window=(override.maintenance_window if override else None),
            )
            blk_rt = BlockRuntime(blk_model)
            blocks.append(blk_rt)
            graph._blocks[blk_id] = blk_rt

        # If no blocks exist yet (editor didn't create them), create one placeholder.
        if not blocks:
            placeholder_id = f"blk-{seg.id}-0"
            blk_model = Block(
                id=placeholder_id,
                segment_id=seg.id,
                length_km=block_length_km,
            )
            blk_rt = BlockRuntime(blk_model)
            blocks.append(blk_rt)
            graph._blocks[placeholder_id] = blk_rt

        seg_rt = SegmentRuntime(seg, blocks)
        graph._segments[seg.id] = seg_rt
        # Link nodes in adjacency map.
        if seg.start_node_id and seg.end_node_id:
            graph._link_nodes(seg.start_node_id, seg.end_node_id, seg.id)

    # Register tracks (visual only).
    for trk in payload.tracks:
        graph._tracks[trk.id] = TrackRuntime(trk)

    return graph


def _parse_iso(iso_str: str) -> datetime:
    """Parse an ISO-8601 string into a timezone-aware datetime."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fill_and_validate(
    payload: LayoutPayload,
    graph: NetworkGraph,
) -> list[str]:
    """
    For each train:
      1. Parse origin departure from authoring hints.
      2. Call build_schedule() to fill ALL nodes' scheduled_arrival/departure.
      3. Apply dwell overrides.
      4. Run validate_schedule() for feasibility.

    Returns a list of human-readable error strings (empty = valid).
    """
    hint_map: dict[str, TrainAuthoringHint] = {
        h.train_id: h for h in payload.authoring_hints
    }
    errors: list[str] = []

    for train_model in payload.trains:
        if not train_model.route:
            continue

        hint = hint_map.get(train_model.id)
        departure_time: Optional[datetime] = None
        if hint and hint.origin_departure_iso:
            try:
                departure_time = _parse_iso(hint.origin_departure_iso)
            except ValueError as exc:
                errors.append(
                    f"Train {train_model.id!r}: invalid origin_departure_iso: {exc}"
                )
                continue

        train_rt = TrainRuntime(train_model)

        # Build a full schedule using physics.
        try:
            computed_schedule = build_schedule(
                train=train_rt,
                route=train_model.route,
                network=graph,
                avg_speed=train_model.avg_speed,
                target_padding_minutes=5.0,
                departure_time=departure_time,
            )
        except (ScheduleError, ValueError, NetworkError) as exc:
            errors.append(f"Train {train_model.name!r} ({train_model.id}): {exc}")
            continue

        # Apply dwell overrides: scheduled_departure += dwell_minutes.
        if hint:
            for node_id, dwell_min in hint.dwell_minutes.items():
                if node_id in computed_schedule and dwell_min > 0:
                    entry = computed_schedule[node_id]
                    base_arr = entry.scheduled_arrival or departure_time
                    if base_arr:
                        computed_schedule[node_id] = ScheduleEntry(
                            scheduled_arrival=entry.scheduled_arrival,
                            scheduled_departure=base_arr + timedelta(minutes=dwell_min),
                            expected_arrival=None,
                            actual_arrival=None,
                        )

        # Write the fully-computed schedule back into the train model.
        train_model.schedule = computed_schedule

        # Validate feasibility of the computed (dwell-adjusted) schedule.
        # Rebuild runtime with updated schedule.
        train_rt_updated = TrainRuntime(train_model)
        violations = validate_schedule(train_rt_updated, graph)
        for v in violations:
            errors.append(
                f"Train {train_model.name!r}: {v.detail}"
            )

    return errors


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/{name}", status_code=200)
async def save_simulation(
    name: str = Path(pattern=r"^[a-zA-Z0-9_\-]{1,80}$"),
    payload: LayoutPayload = ...,
) -> dict:
    """
    Validate and persist a layout.

    Returns { "ok": true } on success, or HTTP 400 with
    { "errors": ["..."] } if the schedule is infeasible.
    """
    # 1. Build graph.
    try:
        graph = _build_network(payload)
    except (NetworkError, Exception) as exc:
        raise HTTPException(status_code=400, detail={"errors": [str(exc)]})

    # 2. Fill schedules and validate.
    errors = _fill_and_validate(payload, graph)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # 3. Persist.
    dest = SIMULATIONS_DIR / f"{name}.json"
    dest.write_text(
        payload.model_dump_json(indent=2),
        encoding="utf-8",
    )

    return {"ok": True, "name": name}


@router.get("/{name}")
async def load_simulation(
    name: str = Path(pattern=r"^[a-zA-Z0-9_\-]{1,80}$"),
) -> LayoutPayload:
    """
    Load a previously saved layout by name.

    Returns the full LayoutPayload.  Returns 404 if the name is unknown.
    """
    src = SIMULATIONS_DIR / f"{name}.json"
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Simulation {name!r} not found.")

    try:
        data = json.loads(src.read_text(encoding="utf-8"))
        return LayoutPayload(**data)
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail=f"Failed to parse saved layout: {exc}"
        )
