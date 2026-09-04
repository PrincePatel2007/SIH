"""
Domain model contracts for the train-network ETA-prediction simulator.

These are pure data models — they carry no simulation logic. The source of
truth for occupancy and timing lives in engine.py; these models are the
serialisation boundary between backend layers and between backend/frontend.

Field-type decisions for ambiguous fields are documented inline.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StationType(str, Enum):
    """Whether the station is a dead-end, pass-through, or sits at a junction."""

    TERMINUS = "terminus"
    THROUGH = "through"
    JUNCTION_STATION = "junction_station"


class TrackDirectionality(str, Enum):
    BIDIRECTIONAL = "bidirectional"
    ONE_WAY_FORWARD = "one_way_forward"
    ONE_WAY_REVERSE = "one_way_reverse"


class DriverDutyStatus(str, Enum):
    """
    Stored status; the ≥12h journey override is computed at runtime in
    arbitration.py, not stored here.
    """

    NORMAL = "normal"
    OVER_DUTY = "over_duty"


class SignalStateValue(str, Enum):
    GREEN = "green"
    RED = "red"
    CAUTION = "caution"


class PriorityTier(int, Enum):
    EXPRESS = 1
    ORDINARY = 2
    LOCAL = 3


# ---------------------------------------------------------------------------
# Sub-models / value objects
# ---------------------------------------------------------------------------


class Point(BaseModel):
    """2-D canvas coordinate (pixels in the editor coordinate space)."""

    x: float
    y: float


class MaintenanceWindow(BaseModel):
    """
    An ISO-8601-compatible time range.  Stored as plain strings so the model
    stays timezone-agnostic at the contract layer; callers must parse.
    """

    start_iso: str
    end_iso: str


class ScheduleEntry(BaseModel):
    """
    Per-train, per-node timing record.

    scheduled_*  – fixed at authoring time, never mutated by the engine.
    expected_*   – live-updated by arbitration / ETA propagation.
    actual_*     – set once the event actually occurs; then frozen.

    All four can be null (e.g. a train that skips a station has no
    scheduled_arrival for it).
    """

    scheduled_arrival: Optional[datetime] = None
    scheduled_departure: Optional[datetime] = None
    expected_arrival: Optional[datetime] = None
    actual_arrival: Optional[datetime] = None


class RouteHop(BaseModel):
    """
    One step in a train's route: the node to visit and which segment to use
    to get there from the previous node.  segment_id is null for the very
    first hop (the origin node).
    """

    node_id: str
    segment_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Core domain models
# ---------------------------------------------------------------------------


class WeatherCell(BaseModel):
    """
    A region of weather that covers one or more blocks.

    intensity – 0.0 (no effect) to 1.0 (full speed-reduction as defined by
                conditions.py).  The cell itself carries no speed-math; that
                lives in conditions.py.
    """

    id: str
    intensity: float = Field(ge=0.0, le=1.0)
    affected_block_ids: list[str] = Field(default_factory=list)


class SignalState(BaseModel):
    """
    A per-block lineside signal.

    controlled_by_junction_id – null means the signal is autonomous (e.g. a
    simple block-occupancy sensor) rather than explicitly set by a junction.

    This model is a renderable decoration at this phase; no simulation
    semantics are attached yet.
    """

    id: str
    block_id: str
    state: SignalStateValue = SignalStateValue.GREEN
    controlled_by_junction_id: Optional[str] = None


class Block(BaseModel):
    """
    The smallest track unit (~1 km).  Mutually exclusive occupancy is the
    core collision-prevention primitive.

    occupied_by     – train_id of the occupying train, or null if clear.
    speed_restriction – km/h cap that overrides a train's max_speed when
                        non-null (e.g. a permanent slow-order).
    maintenance_window – if set, the block is unavailable during this window.
    weather_cell_id – reference to a WeatherCell; conditions.py reads this to
                      compute effective speed.
    """

    id: str
    segment_id: str
    length_km: float = Field(gt=0.0)
    occupied_by: Optional[str] = None  # train_id | null
    weather_cell_id: Optional[str] = None
    speed_restriction: Optional[float] = Field(default=None, gt=0.0)  # km/h
    maintenance_window: Optional[MaintenanceWindow] = None


class Segment(BaseModel):
    """
    An ordered, non-branching chain of Blocks between two Nodes.

    ordered_block_ids – order matters: index 0 is adjacent to start_node_id,
                        last index is adjacent to end_node_id.
    """

    id: str
    start_node_id: str
    end_node_id: str
    ordered_block_ids: list[str] = Field(default_factory=list)


class Junction(BaseModel):
    """
    A merge / crossing point with ≥2 segments attached.

    The junction itself is treated as ONE atomic block for occupancy — engine.py
    is responsible for managing that occupancy; this model just carries the
    structural connectivity.

    signal_states – keyed by approach segment_id → SignalState id.  Records
                    which signal guards each approach into this junction.
    """

    id: str
    connected_segment_ids: list[str] = Field(default_factory=list)
    signal_states: dict[str, str] = Field(
        default_factory=dict,
        description="approach segment_id → signal_state id",
    )


class Station(BaseModel):
    """
    A node where trains schedule arrivals and departures.

    platform_tracks  – ordered list of Track ids that form the station's
                       platform roads.
    rotation_deg     – visual rotation of the station icon on the canvas.
    station_type     – see StationType enum.
    """

    id: str
    name: str
    platform_tracks: list[str] = Field(default_factory=list)
    rotation_deg: float = 0.0
    station_type: StationType = StationType.THROUGH


class Track(BaseModel):
    """
    Geometric / visual representation of a Segment's path.

    This is a RENDERING concern — the source of truth for where a train
    physically is lives in Block.occupied_by, not here.

    geometry             – ordered list of 2-D control points.  Interpretation
                           (polyline vs. Bézier) is up to the renderer.
    directionality       – see TrackDirectionality.
    restricted_to_priority – if non-null, only trains of this tier or higher
                             (lower number) may use this track.
    segment_id           – the Segment this track represents geometrically.
    """

    id: str
    segment_id: str
    geometry: list[Point] = Field(default_factory=list)
    directionality: TrackDirectionality = TrackDirectionality.BIDIRECTIONAL
    restricted_to_priority: Optional[int] = Field(
        default=None,
        ge=1,
        le=3,
        description="1=express only, 2=ordinary+express, 3=all; null=unrestricted",
    )


class Train(BaseModel):
    """
    A train entity.  Carries its full route, schedule, and physical state.

    priority           – 1=express, 2=ordinary, 3=local (lower = higher priority).
    driver_duty_status – over_duty overrides numeric priority at arbitration time.
    route              – ordered RouteHops from origin to final destination.
    schedule           – keyed by node_id → ScheduleEntry.
    current_block_id   – null when the train has not yet entered the network or
                         has completed its journey.
    current_position_in_block – 0.0 (block entry) to 1.0 (block exit); used by
                                the renderer for smooth interpolation.
    """

    id: str
    name: str
    color: str = Field(description="CSS hex color string, e.g. '#e63946'")
    priority: PriorityTier = PriorityTier.ORDINARY
    num_carriages: int = Field(ge=1)
    max_speed: float = Field(gt=0.0, description="km/h")
    avg_speed: float = Field(gt=0.0, description="km/h under normal conditions")
    route: list[RouteHop] = Field(default_factory=list)
    schedule: dict[str, ScheduleEntry] = Field(
        default_factory=dict,
        description="node_id → ScheduleEntry",
    )
    driver_duty_status: DriverDutyStatus = DriverDutyStatus.NORMAL
    current_block_id: Optional[str] = None
    current_position_in_block: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="0.0 = block entry, 1.0 = block exit",
    )
