"""
train.py — Train runtime wrapper, schedule builder, and schedule validator.

Architecture contract
---------------------
* This module knows about Trains, physics (distance / speed / time), and
  Schedule feasibility.  It knows about network.py (to query block lengths
  and pathfinding).
* It does NOT know about other trains, arbitration, occupancy, or engine state.
* If you find yourself importing engine.py or arbitration.py from here, stop —
  that's a dependency leak in the wrong direction.

Priority override rules (summarised here; enforced in arbitration.py)
----------------------------------------------------------------------
A train's *effective* priority at a junction is determined as follows:

    1. If is_max_priority_override is True, the train is treated as having the
       highest possible priority (equivalent to PriorityTier.EXPRESS = 1),
       regardless of its stored `priority` field.
    2. Otherwise, the stored `priority` field (1 < 2 < 3, lower = higher
       priority) governs.

is_max_priority_override is True when EITHER of the following holds:
    a. driver_duty_status == DriverDutyStatus.OVER_DUTY, OR
    b. estimated_total_journey_time_hours(network) >= 12.0

This property is INTENTIONALLY read-only and stateless here — it is a flag
that arbitration.py reads at the moment it needs to rank trains.  It does not
mutate the train or get cached.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models import (
    DriverDutyStatus,
    PriorityTier,
    RouteHop,
    ScheduleEntry,
    Train,
)
from app.network import NetworkGraph, NetworkError


# ---------------------------------------------------------------------------
# Exceptions & structured results
# ---------------------------------------------------------------------------


class ScheduleError(ValueError):
    """
    Raised when build_schedule cannot produce a physically feasible schedule.
    Always names the infeasible hop in the message.
    """


@dataclass
class ScheduleViolation:
    """
    A single feasibility violation found by validate_schedule.

    Attributes
    ----------
    hop_index : int
        Zero-based index into the train's route for the arrival hop.
    from_node_id : str
        The node the train is departing from.
    to_node_id : str
        The node the train is arriving at.
    segment_id : str
        The segment used for this hop.
    distance_km : float
        Physical distance of the hop in km.
    max_speed_kmh : float
        The train's max_speed at the time of validation.
    min_travel_hours : float
        Minimum physically possible travel time (distance_km / max_speed_kmh).
    scheduled_window_hours : float
        The scheduled departure-to-arrival window actually in the schedule.
    detail : str
        Human-readable description of the violation.
    """

    hop_index: int
    from_node_id: str
    to_node_id: str
    segment_id: str
    distance_km: float
    max_speed_kmh: float
    min_travel_hours: float
    scheduled_window_hours: float
    detail: str


# ---------------------------------------------------------------------------
# Physics helpers (module-level, no train state needed)
# ---------------------------------------------------------------------------


def segment_distance_km(segment_id: str, network: NetworkGraph) -> float:
    """Return the total km of a segment by summing its block lengths."""
    seg = network.get_segment(segment_id)
    return seg.total_length_km


def route_total_distance_km(
    route: list[RouteHop],
    network: NetworkGraph,
) -> float:
    """
    Sum the physical distances of all hops in *route* that have a segment_id.
    The first hop (origin node) has segment_id=None and contributes 0 km.
    """
    total = 0.0
    for hop in route:
        if hop.segment_id is not None:
            total += segment_distance_km(hop.segment_id, network)
    return total


def travel_time_hours(distance_km: float, speed_kmh: float) -> float:
    """
    Compute travel time in hours.

    Raises ValueError if speed_kmh <= 0 (caller should never pass a zero
    speed — the Pydantic model enforces gt=0 on max_speed / avg_speed, but
    callers outside Pydantic validation may pass arbitrary floats).
    """
    if speed_kmh <= 0.0:
        raise ValueError(f"speed_kmh must be > 0, got {speed_kmh!r}.")
    return distance_km / speed_kmh


# ---------------------------------------------------------------------------
# TrainRuntime
# ---------------------------------------------------------------------------


class TrainRuntime:
    """
    Mutable runtime wrapper around the Train Pydantic model.

    Owns physics queries (journey time, feasibility check) and exposes the
    is_max_priority_override flag used by arbitration.py.

    What this class does NOT do
    ---------------------------
    * It does not arbitrate between trains.
    * It does not mutate block occupancy.
    * It does not track other trains or their ETAs.
    * It does not cache computed travel times (they are always freshly computed
      from the current network state to avoid staleness).
    """

    def __init__(self, model: Train) -> None:
        self._model = model

    # -- identity / attributes -----------------------------------------------

    @property
    def id(self) -> str:
        return self._model.id

    @property
    def name(self) -> str:
        return self._model.name

    @property
    def priority(self) -> PriorityTier:
        return self._model.priority

    @property
    def max_speed(self) -> float:
        return self._model.max_speed

    @property
    def avg_speed(self) -> float:
        return self._model.avg_speed

    @property
    def driver_duty_status(self) -> DriverDutyStatus:
        return self._model.driver_duty_status

    @property
    def route(self) -> list[RouteHop]:
        return list(self._model.route)

    @property
    def schedule(self) -> dict[str, ScheduleEntry]:
        return dict(self._model.schedule)

    @property
    def current_block_id(self) -> Optional[str]:
        return self._model.current_block_id

    @property
    def current_position_in_block(self) -> float:
        return self._model.current_position_in_block

    # -- journey time --------------------------------------------------------

    def estimated_total_journey_time_hours(self, network: NetworkGraph) -> float:
        """
        Compute the total journey time in hours for the train's full route,
        using *avg_speed* as the baseline speed (no weather / conditions
        modifiers — those live in conditions.py).

        Returns 0.0 if the train has no route or all hops lack a segment_id
        (i.e. it's a single-node trip).

        This is deliberately recomputed fresh each call rather than cached,
        because the network can change (e.g. track splits, block length edits)
        and stale journey times would silently produce wrong priority overrides.
        """
        total_km = route_total_distance_km(self._model.route, network)
        if total_km == 0.0 or self._model.avg_speed <= 0.0:
            return 0.0
        return travel_time_hours(total_km, self._model.avg_speed)

    # -- priority override flag ----------------------------------------------

    def is_max_priority_override(self, network: NetworkGraph) -> bool:
        """
        Return True if this train must be treated as maximum-priority at any
        junction arbitration, regardless of its stored `priority` tier.

        Triggers when EITHER condition holds:
          a) driver_duty_status == OVER_DUTY — driver is beyond safe duty hours,
             so the train must be cleared as soon as possible.
          b) estimated total journey time >= 12 hours — the trip is long enough
             that delays compound significantly; the system gives it max priority
             to help keep it on schedule.

        Usage
        -----
        This flag is read by arbitration.py at arbitration time.  It is NOT
        stored on the model — it must be checked live each time it's needed,
        because avg_speed and route can change during an edit session.

        arbitration.py ranking order (documented here, enforced there):
            1. is_max_priority_override == True  →  treated as priority 0
               (beats all numeric tiers, including EXPRESS=1)
            2. priority tier (1=express, 2=ordinary, 3=local)
            3. earliest ETA at the contested junction
        """
        if self._model.driver_duty_status == DriverDutyStatus.OVER_DUTY:
            return True
        return self.estimated_total_journey_time_hours(network) >= 12.0

    # -- position update (called by engine.py only) --------------------------

    def update_position(
        self,
        block_id: Optional[str],
        position_in_block: float,
    ) -> None:
        """
        Update the train's current physical position.
        Only engine.py should call this — it's the sole place that commits
        physical state changes.
        """
        self._model.current_block_id = block_id
        self._model.current_position_in_block = position_in_block

    def update_expected_arrival(
        self, node_id: str, expected_arrival: Optional[datetime]
    ) -> None:
        """
        Update the live expected_arrival for a node in the train's schedule.
        Called by arbitration.py after a grant is issued.
        Only the expected_arrival field is mutable post-authoring;
        scheduled_arrival and actual_arrival are frozen.
        """
        if node_id not in self._model.schedule:
            self._model.schedule[node_id] = ScheduleEntry()
        self._model.schedule[node_id].expected_arrival = expected_arrival

    def record_actual_arrival(self, node_id: str, actual: datetime) -> None:
        """
        Freeze the actual arrival time for a node.  Once set, this must not
        change — only engine.py should call this at the moment a train enters
        a node.
        """
        if node_id not in self._model.schedule:
            self._model.schedule[node_id] = ScheduleEntry()
        self._model.schedule[node_id].actual_arrival = actual

    # -- serialisation -------------------------------------------------------

    def to_model(self) -> Train:
        return self._model.model_copy(deep=True)

    def __repr__(self) -> str:
        return (
            f"TrainRuntime(id={self.id!r}, name={self.name!r}, "
            f"priority={self._model.priority!r})"
        )


# ---------------------------------------------------------------------------
# build_schedule
# ---------------------------------------------------------------------------


def build_schedule(
    train: TrainRuntime,
    route: list[RouteHop],
    network: NetworkGraph,
    avg_speed: float,
    target_padding_minutes: float,
    departure_time: Optional[datetime] = None,
) -> dict[str, ScheduleEntry]:
    """
    Compute a physically realisable schedule for *train* along *route*.

    Algorithm
    ---------
    For each hop i (from hop 1 onward, since hop 0 is the origin):
      1. Look up the segment's block-length distance in km.
      2. Compute baseline travel time = distance / avg_speed.
      3. Add target_padding_minutes as slack.
      4. Set scheduled_arrival at hop i's node = previous scheduled_departure
         + baseline_travel_time + padding.
      5. Set scheduled_departure = scheduled_arrival (no dwell time modelled
         at this phase; dwell can be added later by the editor).

    Feasibility check (hard constraint, raises ScheduleError)
    ----------------------------------------------------------
    After computing the scheduled window for each hop, verify:

        distance_km / train.max_speed  <=  window_hours

    where window_hours = scheduled_arrival - previous scheduled_departure.

    If the target_padding_minutes causes a window that is STILL tighter than
    max_speed allows — which can't happen with positive padding unless avg_speed
    > max_speed (a misconfiguration), or the caller explicitly passes a
    departure_time that's already too tight — we raise ScheduleError naming the
    infeasible hop.

    In practice the check will fail if:
      - avg_speed > max_speed (misconfiguration — avg should never exceed max).
      - The caller passes a departure_time already past the minimum arrival time.
        (This can happen if the function is called again with a re-anchored start.)

    Parameters
    ----------
    train : TrainRuntime
        Source of max_speed for feasibility checking.
    route : list[RouteHop]
        The ordered list of hops. hop[0] must have segment_id=None (origin).
    network : NetworkGraph
        Used to look up segment distances.
    avg_speed : float
        km/h.  Used for baseline travel time.  Should be <= train.max_speed.
    target_padding_minutes : float
        Minutes of slack added to every hop for delay recovery.  Must be >= 0.
    departure_time : datetime | None
        The scheduled departure from the origin node.  Defaults to now (UTC)
        if not provided.  Should be timezone-aware.

    Returns
    -------
    dict[str, ScheduleEntry]
        node_id → ScheduleEntry with scheduled_arrival and scheduled_departure
        filled in.  expected_arrival and actual_arrival are left as None
        (they are populated by the simulation engine, not at authoring time).

    Raises
    ------
    ScheduleError (subclass of ValueError)
        If any hop's computed scheduled window is tighter than what max_speed
        physically allows.
    ValueError
        If avg_speed <= 0, target_padding_minutes < 0, or route is empty.
    """
    if avg_speed <= 0.0:
        raise ValueError(f"avg_speed must be > 0, got {avg_speed!r}.")
    if target_padding_minutes < 0.0:
        raise ValueError(
            f"target_padding_minutes must be >= 0, got {target_padding_minutes!r}."
        )
    if not route:
        raise ValueError("route must have at least one hop.")

    padding = timedelta(minutes=target_padding_minutes)

    # Anchor: scheduled departure from the origin node.
    origin_hop = route[0]
    if departure_time is None:
        departure_time = datetime.now(tz=timezone.utc)

    schedule: dict[str, ScheduleEntry] = {}

    # Origin entry: we know when it departs, no scheduled arrival (it starts here).
    schedule[origin_hop.node_id] = ScheduleEntry(
        scheduled_arrival=None,
        scheduled_departure=departure_time,
        expected_arrival=None,
        actual_arrival=None,
    )

    # Rolling time cursor: the last scheduled departure (or arrival, for
    # intermediate stops where dwell = 0).
    cursor: datetime = departure_time

    for hop_index, hop in enumerate(route[1:], start=1):
        if hop.segment_id is None:
            raise ValueError(
                f"Route hop {hop_index} (node {hop.node_id!r}) has no segment_id. "
                "Only the first (origin) hop may have segment_id=None."
            )

        # Physical distance for this hop.
        try:
            dist_km = segment_distance_km(hop.segment_id, network)
        except NetworkError as exc:
            raise ValueError(
                f"Hop {hop_index} (node {hop.node_id!r}, segment {hop.segment_id!r}): "
                f"segment not found in network. Original error: {exc}"
            ) from exc

        # Baseline travel time at avg_speed.
        baseline_hours = travel_time_hours(dist_km, avg_speed)
        baseline_td = timedelta(hours=baseline_hours)

        # Proposed scheduled arrival with padding.
        scheduled_arr = cursor + baseline_td + padding

        # ── Feasibility check ──────────────────────────────────────────────
        # A train running at max_speed must not arrive before scheduled_arr.
        # Equivalently: the window from cursor to scheduled_arr must be >=
        # the time needed at max_speed.
        window_hours = (scheduled_arr - cursor).total_seconds() / 3600.0
        min_travel_hours = travel_time_hours(dist_km, train.max_speed)

        if window_hours < min_travel_hours:
            # This should only happen if avg_speed > max_speed.
            raise ScheduleError(
                f"Hop {hop_index}: {hop.node_id!r} via segment {hop.segment_id!r} is "
                f"infeasible. "
                f"Distance: {dist_km:.3f} km, max_speed: {train.max_speed:.1f} km/h → "
                f"minimum travel time: {min_travel_hours * 60:.1f} min, "
                f"but scheduled window is only {window_hours * 60:.1f} min. "
                f"avg_speed ({avg_speed:.1f} km/h) must not exceed "
                f"max_speed ({train.max_speed:.1f} km/h)."
            )

        schedule[hop.node_id] = ScheduleEntry(
            scheduled_arrival=scheduled_arr,
            scheduled_departure=scheduled_arr,  # zero dwell; editor can adjust later
            expected_arrival=None,
            actual_arrival=None,
        )

        cursor = scheduled_arr

    return schedule


# ---------------------------------------------------------------------------
# validate_schedule
# ---------------------------------------------------------------------------


def validate_schedule(
    train: TrainRuntime,
    network: NetworkGraph,
) -> list[ScheduleViolation]:
    """
    Re-check an existing schedule on *train* against the max_speed feasibility
    rule.

    For every consecutive pair of scheduled hops (departure → arrival), verify:

        distance_km / max_speed  <=  window_hours

    Returns an empty list if the schedule is fully feasible.
    Returns one ScheduleViolation per infeasible hop — does not raise.

    This function is called:
    - At save-time by the editor REST endpoint to surface authoring errors.
    - After loading a saved layout, to catch stale/corrupted schedules.
    - By tests to verify that schedule corruption is detected.

    Parameters
    ----------
    train : TrainRuntime
        The train whose schedule and route are to be validated.
    network : NetworkGraph
        Used to look up segment distances.

    Returns
    -------
    list[ScheduleViolation]
        Empty = valid.  Each entry describes one infeasible hop.
    """
    route = train.route
    sched = train.schedule
    violations: list[ScheduleViolation] = []

    # Walk consecutive (departure_node, arrival_node) pairs.
    for hop_index, hop in enumerate(route[1:], start=1):
        if hop.segment_id is None:
            # Non-first hop without a segment — structural error, not a timing
            # violation; network/editor validation catches this separately.
            continue

        # Find the previous node in the route.
        prev_hop = route[hop_index - 1]
        prev_node_id = prev_hop.node_id
        curr_node_id = hop.node_id

        prev_entry = sched.get(prev_node_id)
        curr_entry = sched.get(curr_node_id)

        if prev_entry is None or curr_entry is None:
            # Missing schedule entry — skip (editor validation handles this).
            continue

        departure = prev_entry.scheduled_departure
        arrival = curr_entry.scheduled_arrival

        if departure is None or arrival is None:
            continue  # Partial schedule — nothing to check yet.

        # Make both datetimes offset-aware for subtraction if needed.
        if departure.tzinfo is None:
            departure = departure.replace(tzinfo=timezone.utc)
        if arrival.tzinfo is None:
            arrival = arrival.replace(tzinfo=timezone.utc)

        window_hours = (arrival - departure).total_seconds() / 3600.0

        try:
            dist_km = segment_distance_km(hop.segment_id, network)
        except NetworkError:
            # Segment doesn't exist in the current network — structural error.
            continue

        min_travel_hours = travel_time_hours(dist_km, train.max_speed)

        if window_hours < min_travel_hours:
            violations.append(
                ScheduleViolation(
                    hop_index=hop_index,
                    from_node_id=prev_node_id,
                    to_node_id=curr_node_id,
                    segment_id=hop.segment_id,
                    distance_km=dist_km,
                    max_speed_kmh=train.max_speed,
                    min_travel_hours=min_travel_hours,
                    scheduled_window_hours=window_hours,
                    detail=(
                        f"Hop {hop_index} ({prev_node_id!r} → {curr_node_id!r}): "
                        f"{dist_km:.3f} km at max {train.max_speed:.1f} km/h needs "
                        f"{min_travel_hours * 60:.1f} min, but scheduled window is "
                        f"{window_hours * 60:.1f} min."
                    ),
                )
            )

    return violations
