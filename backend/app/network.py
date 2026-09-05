"""
network.py — Rail network graph: runtime objects and structural operations.

Architecture contract
---------------------
* This module knows about Blocks, Segments, Junctions, Stations, and Tracks.
  It does NOT know about Trains, time, or arbitration.
* Pydantic models (from models.py) are the serialisation boundary.
  NetworkGraph wraps them in runtime objects that hold mutable state and
  graph-structural behaviour.
* The ONLY place block/junction occupancy is *committed* is engine.py.
  network.py exposes occupy() / release() on runtime objects so that engine.py
  can call them — but network.py itself never decides WHEN to occupy; it only
  enforces the exclusivity invariant.
* If you find yourself importing engine.py from here, stop — dependency leak.

Node vocabulary
---------------
A "node" in the graph is either a Junction or a Station (both share a string id
and act as endpoints of Segments).  They are unified under NodeRuntime so
pathfinding can treat them identically.
"""

from __future__ import annotations

import uuid
from collections import deque
from typing import Optional, Union

from app.models import (
    Block,
    Junction,
    MaintenanceWindow,
    Point,
    Segment,
    SignalState,
    SignalStateValue,
    Station,
    StationType,
    Track,
    TrackDirectionality,
    WeatherCell,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class OccupancyError(Exception):
    """Raised when a train tries to occupy an already-occupied block or junction."""


class NetworkError(Exception):
    """Raised for structural violations (bad connectivity, missing ids, etc.)."""


# ---------------------------------------------------------------------------
# Runtime wrappers
# ---------------------------------------------------------------------------


class BlockRuntime:
    """
    Mutable runtime wrapper around the Block Pydantic model.

    Responsibility: enforce mutually-exclusive occupancy.
    engine.py is the *only* caller that should call occupy() / release().
    """

    DEFAULT_LENGTH_KM: float = 1.0

    def __init__(self, model: Block) -> None:
        self._model = model

    # -- identity / structural -----------------------------------------------

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def segment_id(self) -> str:
        return self._model.segment_id

    @property
    def length_km(self) -> float:
        return self._model.length_km

    # -- conditions ----------------------------------------------------------

    @property
    def weather_cell_id(self) -> Optional[str]:
        return self._model.weather_cell_id

    @weather_cell_id.setter
    def weather_cell_id(self, value: Optional[str]) -> None:
        self._model.weather_cell_id = value

    @property
    def speed_restriction(self) -> Optional[float]:
        return self._model.speed_restriction

    @speed_restriction.setter
    def speed_restriction(self, value: Optional[float]) -> None:
        self._model.speed_restriction = value

    @property
    def maintenance_window(self) -> Optional[MaintenanceWindow]:
        return self._model.maintenance_window

    @maintenance_window.setter
    def maintenance_window(self, value: Optional[MaintenanceWindow]) -> None:
        self._model.maintenance_window = value

    # -- occupancy -----------------------------------------------------------

    @property
    def occupied_by(self) -> Optional[str]:
        return self._model.occupied_by

    @property
    def is_free(self) -> bool:
        return self._model.occupied_by is None

    def occupy(self, train_id: str) -> None:
        """
        Mark this block as occupied by *train_id*.

        Raises OccupancyError if already occupied by a *different* train.
        Idempotent if the same train re-claims the same block (no-op).
        """
        current = self._model.occupied_by
        if current is not None and current != train_id:
            raise OccupancyError(
                f"Block {self.id!r} is already occupied by train {current!r}; "
                f"train {train_id!r} cannot enter."
            )
        self._model.occupied_by = train_id

    def release(self, train_id: str) -> None:
        """
        Clear occupancy.  Only the currently-occupying train may release.

        Raises OccupancyError if *train_id* is not the current occupant.
        """
        current = self._model.occupied_by
        if current != train_id:
            raise OccupancyError(
                f"Block {self.id!r} is occupied by {current!r}, not {train_id!r}; "
                "cannot release."
            )
        self._model.occupied_by = None

    # -- serialisation -------------------------------------------------------

    def to_model(self) -> Block:
        return self._model.model_copy()

    def __repr__(self) -> str:
        occ = self._model.occupied_by or "free"
        return f"BlockRuntime(id={self.id!r}, len={self.length_km}km, occ={occ!r})"


class JunctionRuntime:
    """
    Mutable runtime wrapper around the Junction Pydantic model.

    A junction has NO internal blocks.  The junction *itself* is the atomic
    occupancy unit — exactly one train or none.  This mirrors the domain rule
    that a crossing point can't be split between two trains.
    """

    def __init__(self, model: Junction) -> None:
        self._model = model
        # Junction-level occupancy is tracked HERE, not in a Block object,
        # because junctions have no block of their own.
        self._occupied_by: Optional[str] = None

    # -- identity ------------------------------------------------------------

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def connected_segment_ids(self) -> list[str]:
        return list(self._model.connected_segment_ids)

    # -- signals -------------------------------------------------------------

    def set_signal(self, approach_segment_id: str, signal_state_id: str) -> None:
        self._model.signal_states[approach_segment_id] = signal_state_id

    def get_signal(self, approach_segment_id: str) -> Optional[str]:
        return self._model.signal_states.get(approach_segment_id)

    # -- occupancy -----------------------------------------------------------

    @property
    def occupied_by(self) -> Optional[str]:
        return self._occupied_by

    @property
    def is_free(self) -> bool:
        return self._occupied_by is None

    def occupy(self, train_id: str) -> None:
        """
        Claim the junction atomically for *train_id*.

        Raises OccupancyError if already occupied by a different train.
        The entire junction is claimed at once — no partial occupancy.
        """
        if self._occupied_by is not None and self._occupied_by != train_id:
            raise OccupancyError(
                f"Junction {self.id!r} is already occupied by train "
                f"{self._occupied_by!r}; train {train_id!r} cannot enter."
            )
        self._occupied_by = train_id

    def release(self, train_id: str) -> None:
        if self._occupied_by != train_id:
            raise OccupancyError(
                f"Junction {self.id!r} is held by {self._occupied_by!r}, "
                f"not {train_id!r}; cannot release."
            )
        self._occupied_by = None

    # -- connectivity --------------------------------------------------------

    def connect_segment(self, segment_id: str) -> None:
        if segment_id not in self._model.connected_segment_ids:
            self._model.connected_segment_ids.append(segment_id)

    # -- serialisation -------------------------------------------------------

    def to_model(self) -> Junction:
        return self._model.model_copy()

    def __repr__(self) -> str:
        occ = self._occupied_by or "free"
        return (
            f"JunctionRuntime(id={self.id!r}, "
            f"segments={self._model.connected_segment_ids}, occ={occ!r})"
        )


class StationRuntime:
    """Mutable runtime wrapper around the Station Pydantic model."""

    def __init__(self, model: Station) -> None:
        self._model = model

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def name(self) -> str:
        return self._model.name

    @property
    def station_type(self) -> StationType:
        return self._model.station_type

    @property
    def platform_tracks(self) -> list[str]:
        return list(self._model.platform_tracks)

    def add_platform_track(self, track_id: str) -> None:
        if track_id not in self._model.platform_tracks:
            self._model.platform_tracks.append(track_id)

    def to_model(self) -> Station:
        return self._model.model_copy()

    def __repr__(self) -> str:
        return f"StationRuntime(id={self.id!r}, name={self.name!r})"


# A "node" in the graph sense — either a junction or a station.
NodeRuntime = Union[JunctionRuntime, StationRuntime]


class SegmentRuntime:
    """
    Mutable runtime wrapper around the Segment Pydantic model.

    Owns an ordered list of BlockRuntime objects and provides segment-level
    helpers (total length, free-block queries).
    """

    def __init__(self, model: Segment, blocks: list[BlockRuntime]) -> None:
        if len(blocks) == 0:
            raise NetworkError(
                f"Segment {model.id!r} must have at least one block."
            )
        self._model = model
        self._blocks: list[BlockRuntime] = blocks

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def start_node_id(self) -> str:
        return self._model.start_node_id

    @property
    def end_node_id(self) -> str:
        return self._model.end_node_id

    @property
    def blocks(self) -> list[BlockRuntime]:
        return list(self._blocks)

    @property
    def ordered_block_ids(self) -> list[str]:
        return [b.id for b in self._blocks]

    @property
    def total_length_km(self) -> float:
        return sum(b.length_km for b in self._blocks)

    def to_model(self) -> Segment:
        m = self._model.model_copy()
        m.ordered_block_ids = self.ordered_block_ids
        return m

    def __repr__(self) -> str:
        return (
            f"SegmentRuntime(id={self.id!r}, "
            f"{self.start_node_id!r}→{self.end_node_id!r}, "
            f"blocks={len(self._blocks)})"
        )


class TrackRuntime:
    """Mutable runtime wrapper around the Track Pydantic model."""

    def __init__(self, model: Track) -> None:
        self._model = model

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def segment_id(self) -> str:
        return self._model.segment_id

    @segment_id.setter
    def segment_id(self, value: str) -> None:
        self._model.segment_id = value

    @property
    def geometry(self) -> list[Point]:
        return list(self._model.geometry)

    @geometry.setter
    def geometry(self, value: list[Point]) -> None:
        self._model.geometry = value

    @property
    def directionality(self) -> TrackDirectionality:
        return self._model.directionality

    @property
    def restricted_to_priority(self) -> Optional[int]:
        return self._model.restricted_to_priority

    def to_model(self) -> Track:
        return self._model.model_copy()

    def __repr__(self) -> str:
        return (
            f"TrackRuntime(id={self.id!r}, seg={self.segment_id!r}, "
            f"pts={len(self._model.geometry)})"
        )


# ---------------------------------------------------------------------------
# NetworkGraph
# ---------------------------------------------------------------------------


class NetworkGraph:
    """
    The authoritative in-memory representation of the rail network.

    Responsibilities
    ----------------
    * Own all runtime objects (blocks, segments, junctions, stations, tracks).
    * Provide structural mutation operations: add_track, connect_tracks,
      split_track_at_point.
    * Expose pathfinding (BFS over the node graph).
    * Surface the occupancy API used by engine.py.

    Not responsible for
    -------------------
    * Deciding WHEN a train occupies or releases — that's engine.py.
    * Simulation time, ETA propagation, arbitration — that's engine.py /
      arbitration.py.
    * Physics (speed / time calculations) — that's train.py / conditions.py.
    """

    def __init__(self) -> None:
        # Primary stores — all keyed by their string id.
        self._blocks: dict[str, BlockRuntime] = {}
        self._segments: dict[str, SegmentRuntime] = {}
        self._junctions: dict[str, JunctionRuntime] = {}
        self._stations: dict[str, StationRuntime] = {}
        self._tracks: dict[str, TrackRuntime] = {}
        self._signals: dict[str, SignalState] = {}
        self._weather_cells: dict[str, WeatherCell] = {}

        # Adjacency: node_id → set of neighbour node_ids reachable via a segment.
        # segment lookup: (node_a, node_b) → segment_id (canonical, smaller id first).
        self._adjacency: dict[str, dict[str, str]] = {}  # node_id → {neighbour: seg_id}

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex[:8]}"

    def _register_node(self, node_id: str) -> None:
        if node_id not in self._adjacency:
            self._adjacency[node_id] = {}

    def _link_nodes(self, node_a: str, node_b: str, segment_id: str) -> None:
        """Register a bidirectional adjacency between two nodes via segment_id."""
        self._register_node(node_a)
        self._register_node(node_b)
        self._adjacency[node_a][node_b] = segment_id
        self._adjacency[node_b][node_a] = segment_id

    def _unlink_nodes(self, node_a: str, node_b: str) -> None:
        self._adjacency.get(node_a, {}).pop(node_b, None)
        self._adjacency.get(node_b, {}).pop(node_a, None)

    def _require_node(self, node_id: str) -> NodeRuntime:
        if node_id in self._junctions:
            return self._junctions[node_id]
        if node_id in self._stations:
            return self._stations[node_id]
        raise NetworkError(f"Node {node_id!r} not found (not a junction or station).")

    def _interpolate_geometry(
        self, geometry: list[Point], t: float
    ) -> tuple[list[Point], Point, list[Point]]:
        """
        Split a polyline at fractional position *t* ∈ (0, 1) along its total
        arc length.

        Returns (left_points, split_point, right_points) where:
        - left_points  includes the split_point as its last element,
        - right_points includes the split_point as its first element.
        """
        if not (0.0 < t < 1.0):
            raise NetworkError(f"Split ratio t={t} must be strictly between 0 and 1.")
        if len(geometry) < 2:
            raise NetworkError("Cannot split a track with fewer than 2 geometry points.")

        # Compute cumulative arc-length distances.
        seg_lengths: list[float] = []
        for i in range(len(geometry) - 1):
            dx = geometry[i + 1].x - geometry[i].x
            dy = geometry[i + 1].y - geometry[i].y
            seg_lengths.append((dx**2 + dy**2) ** 0.5)

        total = sum(seg_lengths)
        if total == 0.0:
            raise NetworkError("Track geometry has zero total length; cannot split.")

        target = t * total
        accumulated = 0.0

        for i, length in enumerate(seg_lengths):
            if accumulated + length >= target:
                # The split point lies within segment i → i+1.
                local_t = (target - accumulated) / length
                p_start = geometry[i]
                p_end = geometry[i + 1]
                split_pt = Point(
                    x=p_start.x + local_t * (p_end.x - p_start.x),
                    y=p_start.y + local_t * (p_end.y - p_start.y),
                )
                left = list(geometry[: i + 1]) + [split_pt]
                right = [split_pt] + list(geometry[i + 1 :])
                return left, split_pt, right
            accumulated += length

        # Floating-point edge: t was extremely close to 1.0.
        raise NetworkError(f"Could not find split point for t={t} (floating point edge).")

    def _make_blocks_for_segment(
        self,
        segment_id: str,
        length_km: float,
        num_blocks: int,
        block_length_km: Optional[float],
    ) -> list[BlockRuntime]:
        """
        Create evenly-spaced blocks for a new segment.

        If *block_length_km* is given, use it exactly for each block;
        otherwise divide *length_km* evenly across *num_blocks* blocks.
        """
        if block_length_km is not None:
            per_block = block_length_km
            num_blocks = max(1, round(length_km / block_length_km))
        else:
            per_block = length_km / max(1, num_blocks)

        blocks = []
        for i in range(num_blocks):
            blk_id = self._new_id("blk")
            model = Block(
                id=blk_id,
                segment_id=segment_id,
                length_km=per_block,
            )
            rt = BlockRuntime(model)
            self._blocks[blk_id] = rt
            blocks.append(rt)
        return blocks

    def _ensure_junction_signal(
        self, junction: JunctionRuntime, segment_id: str
    ) -> None:
        """
        Create a SignalState for *segment_id* approaching *junction* if one
        does not already exist, then register it into both NetworkGraph._signals
        and Junction.signal_states.

        Called internally whenever a segment is wired to a junction so that
        _signals is populated automatically as the network is built.
        Idempotent — re-calling for an already-registered approach is a no-op.
        """
        if junction.get_signal(segment_id) is not None:
            return  # Already registered.
        try:
            seg = self.get_segment(segment_id)
        except NetworkError:
            return
        if not seg.ordered_block_ids:
            return
        # The block adjacent to this junction:
        # if the junction is the *end* node the train exits towards the junction
        # through the last block; if it is the *start* node, through the first.
        if seg.end_node_id == junction.id:
            adj_block_id = seg.ordered_block_ids[-1]
        else:
            adj_block_id = seg.ordered_block_ids[0]
        sig_id = f"sig-{junction.id}-{segment_id}"
        signal = SignalState(
            id=sig_id,
            block_id=adj_block_id,
            state=SignalStateValue.GREEN,
            controlled_by_junction_id=junction.id,
        )
        self.add_signal(signal)
        junction.set_signal(segment_id, sig_id)

    # -----------------------------------------------------------------------
    # Public read accessors
    # -----------------------------------------------------------------------

    def get_block(self, block_id: str) -> BlockRuntime:
        try:
            return self._blocks[block_id]
        except KeyError:
            raise NetworkError(f"Block {block_id!r} not found.")

    def get_segment(self, segment_id: str) -> SegmentRuntime:
        try:
            return self._segments[segment_id]
        except KeyError:
            raise NetworkError(f"Segment {segment_id!r} not found.")

    def get_junction(self, junction_id: str) -> JunctionRuntime:
        try:
            return self._junctions[junction_id]
        except KeyError:
            raise NetworkError(f"Junction {junction_id!r} not found.")

    def get_station(self, station_id: str) -> StationRuntime:
        try:
            return self._stations[station_id]
        except KeyError:
            raise NetworkError(f"Station {station_id!r} not found.")

    def get_track(self, track_id: str) -> TrackRuntime:
        try:
            return self._tracks[track_id]
        except KeyError:
            raise NetworkError(f"Track {track_id!r} not found.")

    def get_node(self, node_id: str) -> NodeRuntime:
        return self._require_node(node_id)

    @property
    def all_blocks(self) -> list[BlockRuntime]:
        return list(self._blocks.values())

    @property
    def all_segments(self) -> list[SegmentRuntime]:
        return list(self._segments.values())

    @property
    def all_junctions(self) -> list[JunctionRuntime]:
        return list(self._junctions.values())

    @property
    def all_stations(self) -> list[StationRuntime]:
        return list(self._stations.values())

    @property
    def all_tracks(self) -> list[TrackRuntime]:
        return list(self._tracks.values())

    # -----------------------------------------------------------------------
    # Node construction
    # -----------------------------------------------------------------------

    def add_junction(
        self,
        *,
        junction_id: Optional[str] = None,
    ) -> JunctionRuntime:
        """Add a bare junction node (no segments yet)."""
        jid = junction_id or self._new_id("jct")
        if jid in self._junctions or jid in self._stations:
            raise NetworkError(f"Node id {jid!r} already exists.")
        model = Junction(id=jid)
        rt = JunctionRuntime(model)
        self._junctions[jid] = rt
        self._register_node(jid)
        return rt

    def add_station(
        self,
        name: str,
        *,
        station_id: Optional[str] = None,
        station_type: StationType = StationType.THROUGH,
        rotation_deg: float = 0.0,
    ) -> StationRuntime:
        """Add a station node."""
        sid = station_id or self._new_id("sta")
        if sid in self._junctions or sid in self._stations:
            raise NetworkError(f"Node id {sid!r} already exists.")
        model = Station(
            id=sid,
            name=name,
            station_type=station_type,
            rotation_deg=rotation_deg,
        )
        rt = StationRuntime(model)
        self._stations[sid] = rt
        self._register_node(sid)
        return rt

    # -----------------------------------------------------------------------
    # add_track — create a segment + track between two nodes
    # -----------------------------------------------------------------------

    def add_track(
        self,
        start_node_id: str,
        end_node_id: str,
        *,
        geometry: Optional[list[Point]] = None,
        length_km: float = 5.0,
        num_blocks: int = 5,
        block_length_km: Optional[float] = None,
        directionality: TrackDirectionality = TrackDirectionality.BIDIRECTIONAL,
        restricted_to_priority: Optional[int] = None,
        segment_id: Optional[str] = None,
        track_id: Optional[str] = None,
    ) -> tuple[SegmentRuntime, TrackRuntime]:
        """
        Create a Segment (with blocks) and its corresponding Track between two
        nodes, registering both in the graph.

        Parameters
        ----------
        start_node_id, end_node_id : str
            Must already exist as junctions or stations.
        geometry : list[Point] | None
            Visual control points.  If None, a straight horizontal line of
            *length_km* × 100 px/km is synthesised.
        length_km : float
            Total segment length.  Ignored when *block_length_km* is set and
            a geometry is provided (geometry arc length is authoritative then).
        num_blocks : int
            Number of blocks to divide the segment into (only used when
            *block_length_km* is None).
        block_length_km : float | None
            If set, each block is exactly this long and *num_blocks* is derived.
        """
        # Validate nodes exist.
        self._require_node(start_node_id)
        self._require_node(end_node_id)

        if start_node_id == end_node_id:
            raise NetworkError("A segment cannot start and end at the same node.")

        # Synthesise geometry if not provided.
        if geometry is None:
            px_per_km = 100.0
            geometry = [
                Point(x=0.0, y=0.0),
                Point(x=length_km * px_per_km, y=0.0),
            ]

        # Create the Segment model first (we need its id for blocks).
        seg_id = segment_id or self._new_id("seg")
        if seg_id in self._segments:
            raise NetworkError(f"Segment id {seg_id!r} already exists.")

        blocks = self._make_blocks_for_segment(
            seg_id, length_km, num_blocks, block_length_km
        )
        seg_model = Segment(
            id=seg_id,
            start_node_id=start_node_id,
            end_node_id=end_node_id,
            ordered_block_ids=[b.id for b in blocks],
        )
        seg_rt = SegmentRuntime(seg_model, blocks)
        self._segments[seg_id] = seg_rt

        # Create the Track.
        trk_id = track_id or self._new_id("trk")
        if trk_id in self._tracks:
            raise NetworkError(f"Track id {trk_id!r} already exists.")
        trk_model = Track(
            id=trk_id,
            segment_id=seg_id,
            geometry=geometry,
            directionality=directionality,
            restricted_to_priority=restricted_to_priority,
        )
        trk_rt = TrackRuntime(trk_model)
        self._tracks[trk_id] = trk_rt

        # Wire connectivity.
        self._link_nodes(start_node_id, end_node_id, seg_id)

        # Let junctions know about this segment and create approach signals.
        if start_node_id in self._junctions:
            jct = self._junctions[start_node_id]
            jct.connect_segment(seg_id)
            self._ensure_junction_signal(jct, seg_id)
        if end_node_id in self._junctions:
            jct = self._junctions[end_node_id]
            jct.connect_segment(seg_id)
            self._ensure_junction_signal(jct, seg_id)

        # Let stations record the track.
        if start_node_id in self._stations:
            self._stations[start_node_id].add_platform_track(trk_id)
        if end_node_id in self._stations:
            self._stations[end_node_id].add_platform_track(trk_id)

        return seg_rt, trk_rt

    # -----------------------------------------------------------------------
    # connect_tracks — share a node between two existing tracks/segments
    # -----------------------------------------------------------------------

    def connect_tracks(
        self,
        track_id_a: str,
        track_id_b: str,
        *,
        via_node_id: str,
    ) -> None:
        """
        Declare that track_a and track_b are connected at *via_node_id*.

        This is used when two separately-created tracks need to share a node
        (e.g. a junction that was added after the tracks were created, or when
        re-wiring after a split).  The node must already exist; segments must
        already reference it as start/end node.

        Raises NetworkError if the segment belonging to either track does not
        already have *via_node_id* as one of its endpoints.
        """
        trk_a = self.get_track(track_id_a)
        trk_b = self.get_track(track_id_b)
        seg_a = self.get_segment(trk_a.segment_id)
        seg_b = self.get_segment(trk_b.segment_id)

        self._require_node(via_node_id)

        for seg, trk in ((seg_a, trk_a), (seg_b, trk_b)):
            if via_node_id not in (seg.start_node_id, seg.end_node_id):
                raise NetworkError(
                    f"Track {trk.id!r} (segment {seg.id!r}) does not touch node "
                    f"{via_node_id!r}.  Its endpoints are "
                    f"{seg.start_node_id!r} and {seg.end_node_id!r}."
                )

        # Both already reference the node; just ensure adjacency is registered.
        self._link_nodes(seg_a.start_node_id, seg_a.end_node_id, seg_a.id)
        self._link_nodes(seg_b.start_node_id, seg_b.end_node_id, seg_b.id)

        # Register with junctions and ensure approach signals exist.
        if via_node_id in self._junctions:
            jct = self._junctions[via_node_id]
            jct.connect_segment(seg_a.id)
            jct.connect_segment(seg_b.id)
            self._ensure_junction_signal(jct, seg_a.id)
            self._ensure_junction_signal(jct, seg_b.id)

    # -----------------------------------------------------------------------
    # split_track_at_point — insert a node mid-track
    # -----------------------------------------------------------------------

    def split_track_at_point(
        self,
        track_id: str,
        t: float,
        new_node: NodeRuntime,
        *,
        left_track_id: Optional[str] = None,
        right_track_id: Optional[str] = None,
        left_segment_id: Optional[str] = None,
        right_segment_id: Optional[str] = None,
        block_length_km: Optional[float] = None,
    ) -> tuple[SegmentRuntime, TrackRuntime, SegmentRuntime, TrackRuntime]:
        """
        Split *track_id* at fractional position *t* ∈ (0, 1) along its geometry,
        inserting *new_node* (a Station or Junction) at the split point.

        The original track and its segment are **removed** from the graph.
        Two new (segment, track) pairs are created and registered:
          - left:  original start_node → new_node
          - right: new_node → original end_node

        Blocks are partitioned proportionally: blocks whose cumulative position
        falls on the left side go to the left segment, the rest to the right.
        At minimum one block on each side is guaranteed.

        Returns
        -------
        (left_seg, left_trk, right_seg, right_trk)
        """
        trk = self.get_track(track_id)
        seg = self.get_segment(trk.segment_id)
        original_start = seg.start_node_id
        original_end = seg.end_node_id
        original_blocks = seg.blocks  # ordered list

        # Ensure new_node is registered.
        new_node_id = new_node.id
        if new_node_id not in self._junctions and new_node_id not in self._stations:
            if isinstance(new_node, JunctionRuntime):
                self._junctions[new_node_id] = new_node
            else:
                self._stations[new_node_id] = new_node
            self._register_node(new_node_id)

        # Split geometry.
        left_geom, _split_pt, right_geom = self._interpolate_geometry(
            trk.geometry, t
        )

        # Partition blocks: first ⌊t × n⌋ blocks go left, rest go right.
        # Always guarantee at least 1 block per side.
        n = len(original_blocks)
        split_idx = max(1, min(n - 1, round(t * n)))
        left_blocks_raw = original_blocks[:split_idx]
        right_blocks_raw = original_blocks[split_idx:]

        # Remove original segment + track from graph.
        del self._segments[seg.id]
        del self._tracks[trk.id]
        self._unlink_nodes(original_start, original_end)

        # -- Left segment & track -------------------------------------------
        l_seg_id = left_segment_id or self._new_id("seg")
        l_trk_id = left_track_id or self._new_id("trk")

        # Re-point blocks to the new segment id.
        for blk in left_blocks_raw:
            blk._model.segment_id = l_seg_id
        l_seg_model = Segment(
            id=l_seg_id,
            start_node_id=original_start,
            end_node_id=new_node_id,
            ordered_block_ids=[b.id for b in left_blocks_raw],
        )
        l_seg_rt = SegmentRuntime(l_seg_model, left_blocks_raw)
        self._segments[l_seg_id] = l_seg_rt

        l_trk_model = Track(
            id=l_trk_id,
            segment_id=l_seg_id,
            geometry=left_geom,
            directionality=trk.directionality,
            restricted_to_priority=trk.restricted_to_priority,
        )
        l_trk_rt = TrackRuntime(l_trk_model)
        self._tracks[l_trk_id] = l_trk_rt

        # -- Right segment & track ------------------------------------------
        r_seg_id = right_segment_id or self._new_id("seg")
        r_trk_id = right_track_id or self._new_id("trk")

        for blk in right_blocks_raw:
            blk._model.segment_id = r_seg_id
        r_seg_model = Segment(
            id=r_seg_id,
            start_node_id=new_node_id,
            end_node_id=original_end,
            ordered_block_ids=[b.id for b in right_blocks_raw],
        )
        r_seg_rt = SegmentRuntime(r_seg_model, right_blocks_raw)
        self._segments[r_seg_id] = r_seg_rt

        r_trk_model = Track(
            id=r_trk_id,
            segment_id=r_seg_id,
            geometry=right_geom,
            directionality=trk.directionality,
            restricted_to_priority=trk.restricted_to_priority,
        )
        r_trk_rt = TrackRuntime(r_trk_model)
        self._tracks[r_trk_id] = r_trk_rt

        # -- Re-wire adjacency and junctions --------------------------------
        self._link_nodes(original_start, new_node_id, l_seg_id)
        self._link_nodes(new_node_id, original_end, r_seg_id)

        for node_id, seg_id in [
            (original_start, l_seg_id),
            (new_node_id, l_seg_id),
            (new_node_id, r_seg_id),
            (original_end, r_seg_id),
        ]:
            if node_id in self._junctions:
                jct = self._junctions[node_id]
                jct.connect_segment(seg_id)
                self._ensure_junction_signal(jct, seg_id)
            elif node_id in self._stations:
                # station tracks are the visual tracks, not segment ids
                pass

        # Add platform tracks to station if new_node is a station.
        if isinstance(new_node, StationRuntime):
            new_node.add_platform_track(l_trk_id)
            new_node.add_platform_track(r_trk_id)

        return l_seg_rt, l_trk_rt, r_seg_rt, r_trk_rt

    # -----------------------------------------------------------------------
    # Pathfinding — BFS over the node graph
    # -----------------------------------------------------------------------

    def find_path(
        self,
        start_node_id: str,
        end_node_id: str,
    ) -> Optional[list[str]]:
        """
        Find a path from *start_node_id* to *end_node_id* using BFS on the
        node-level adjacency graph.

        Returns
        -------
        list[str] | None
            An ordered list of segment_ids connecting the two nodes, or None
            if no path exists.

        Note: BFS finds a path with the minimum number of *segment hops*, not
        the shortest physical distance.  Dijkstra will replace this in Phase 3
        once edge weights (block counts / km) matter for ETA estimation.
        """
        self._require_node(start_node_id)
        self._require_node(end_node_id)

        if start_node_id == end_node_id:
            return []

        # BFS: queue of (current_node, path_so_far_as_segment_ids)
        queue: deque[tuple[str, list[str]]] = deque()
        queue.append((start_node_id, []))
        visited: set[str] = {start_node_id}

        while queue:
            current, path = queue.popleft()
            neighbours = self._adjacency.get(current, {})
            for neighbour, seg_id in neighbours.items():
                if neighbour in visited:
                    continue
                new_path = path + [seg_id]
                if neighbour == end_node_id:
                    return new_path
                visited.add(neighbour)
                queue.append((neighbour, new_path))

        return None  # No path found.

    def find_path_excluding_segments(
        self,
        start_node_id: str,
        end_node_id: str,
        excluded_segment_ids: set[str],
    ) -> Optional[list[str]]:
        """
        BFS pathfinding that skips any segment whose id is in
        *excluded_segment_ids*.

        Used by conditions.py when a maintenance window makes one or more
        segments impassable: call this with the segment(s) covering the
        blocked blocks, and it returns an alternate route if one exists,
        or None if the network is fully partitioned.

        Parameters
        ----------
        start_node_id : str
        end_node_id : str
        excluded_segment_ids : set[str]
            Segment ids to treat as impassable for this search only.
            The graph itself is NOT mutated.

        Returns
        -------
        list[str] | None
            Ordered segment_ids of the alternate route, or None.
        """
        self._require_node(start_node_id)
        self._require_node(end_node_id)

        if start_node_id == end_node_id:
            return []

        queue: deque[tuple[str, list[str]]] = deque()
        queue.append((start_node_id, []))
        visited: set[str] = {start_node_id}

        while queue:
            current, path = queue.popleft()
            for neighbour, seg_id in self._adjacency.get(current, {}).items():
                if neighbour in visited:
                    continue
                if seg_id in excluded_segment_ids:
                    continue  # treat this segment as impassable
                new_path = path + [seg_id]
                if neighbour == end_node_id:
                    return new_path
                visited.add(neighbour)
                queue.append((neighbour, new_path))

        return None  # No alternate path found.

    def find_path_blocks(
        self,
        start_node_id: str,
        end_node_id: str,
    ) -> Optional[list[str]]:
        """
        Like find_path but returns an ordered list of block_ids (expanding
        each segment's ordered_block_ids in traversal order).

        Returns None if no path exists.
        """
        seg_path = self.find_path(start_node_id, end_node_id)
        if seg_path is None:
            return None

        block_ids: list[str] = []
        # Walk the node sequence to determine direction through each segment.
        # Reconstruct the node sequence from the segment path.
        node_seq = self._node_sequence_from_segments(start_node_id, seg_path)

        for i, seg_id in enumerate(seg_path):
            seg = self.get_segment(seg_id)
            entry_node = node_seq[i]
            if entry_node == seg.start_node_id:
                block_ids.extend(seg.ordered_block_ids)
            else:
                # Traversing this segment in reverse.
                block_ids.extend(reversed(seg.ordered_block_ids))

        return block_ids

    def _node_sequence_from_segments(
        self, start_node_id: str, seg_ids: list[str]
    ) -> list[str]:
        """
        Given a start node and an ordered segment path, reconstruct the
        ordered node sequence (length = len(seg_ids) + 1).
        """
        nodes = [start_node_id]
        current = start_node_id
        for seg_id in seg_ids:
            seg = self.get_segment(seg_id)
            if seg.start_node_id == current:
                current = seg.end_node_id
            elif seg.end_node_id == current:
                current = seg.start_node_id
            else:
                raise NetworkError(
                    f"Segment {seg_id!r} does not connect to node {current!r}."
                )
            nodes.append(current)
        return nodes

    # -----------------------------------------------------------------------
    # Weather / conditions helpers (read-only here; mutations via engine.py)
    # -----------------------------------------------------------------------

    def add_weather_cell(self, cell: WeatherCell) -> None:
        self._weather_cells[cell.id] = cell

    def get_weather_cell(self, cell_id: str) -> Optional[WeatherCell]:
        return self._weather_cells.get(cell_id)

    def add_signal(self, signal: SignalState) -> None:
        self._signals[signal.id] = signal

    def get_signal(self, signal_id: str) -> Optional[SignalState]:
        return self._signals.get(signal_id)

    def set_signal_state(
        self, signal_id: str, state: SignalStateValue
    ) -> None:
        sig = self._signals.get(signal_id)
        if sig is None:
            raise NetworkError(f"Signal {signal_id!r} not found.")
        sig.state = state

    def register_junction_signals(self) -> None:
        """
        Idempotently create SignalState records for every approach segment at
        every junction in the network.

        For networks built via add_track() / connect_tracks() /
        split_track_at_point() the signals are created automatically during
        construction.  Call this method as a catch-all after loading a network
        from serialised data (e.g. a DB snapshot or JSON fixture) to ensure
        _signals is fully populated before the engine starts.

        Safe to call multiple times — already-registered approaches are skipped.
        """
        for jct in self._junctions.values():
            for seg_id in list(jct.connected_segment_ids):
                self._ensure_junction_signal(jct, seg_id)

    # -----------------------------------------------------------------------
    # Serialisation helpers
    # -----------------------------------------------------------------------

    def to_dict(self) -> dict:
        """
        Snapshot the full graph as plain dicts of Pydantic models.
        Suitable for JSON serialisation by main.py.
        """
        return {
            "blocks": [b.to_model().model_dump() for b in self._blocks.values()],
            "segments": [s.to_model().model_dump() for s in self._segments.values()],
            "junctions": [j.to_model().model_dump() for j in self._junctions.values()],
            "stations": [s.to_model().model_dump() for s in self._stations.values()],
            "tracks": [t.to_model().model_dump() for t in self._tracks.values()],
            "signals": [s.model_dump() for s in self._signals.values()],
            "weather_cells": [w.model_dump() for w in self._weather_cells.values()],
        }
