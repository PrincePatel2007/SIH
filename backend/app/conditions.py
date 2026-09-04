"""
conditions.py — Weather, speed restriction, and maintenance window modifiers.

Architecture contract
---------------------
* This module knows about TrainRuntime, BlockRuntime, NetworkGraph, and
  arbitration.py (for ETA propagation after an effective-speed change).
* It does NOT commit occupancy.  engine.py is the sole committer.
* Dependency order: models → network → train → arbitration → conditions → engine

Responsibilities
----------------
1. Weather: translate WeatherCell intensity → speed multiplier; apply to
   trains; mark CANCELLED for not-yet-departed trains on intensity==1.0.
2. SpeedRestriction: time-bounded per-block speed cap, stacking with weather.
3. MaintenanceWindow: time-bounded per-block/segment impassability; triggers
   re-pathfinding via network.find_path_excluding_segments (not reimplemented
   here — pathfinding lives in network.py by design).

Key invariants preserved
------------------------
* conditions.py never calls block.occupy() / junction.occupy().
* conditions.py calls train.update_expected_arrival() (via propagate_eta_update)
  whenever effective speed changes — but only through arbitration.propagate_eta_update,
  not by mutating schedule fields directly.
* Train CANCELLED status is stored in ConditionsEngine._cancelled_trains, NOT
  on the Train Pydantic model (which has no status field).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from app.models import WeatherCell
from app.network import BlockRuntime, NetworkError, NetworkGraph
from app.train import TrainRuntime, travel_time_hours
from app.arbitration import ArbitrationRegistry, propagate_eta_update


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConditionsError(Exception):
    """Raised for structural errors in conditions setup or application."""


class NoAlternatePathError(ConditionsError):
    """
    Raised when a maintenance window blocks a train's current route and no
    alternate path exists in the network.
    """


# ---------------------------------------------------------------------------
# Weather speed-multiplier table
# ---------------------------------------------------------------------------

# Ordered from most severe to least.  get_speed_multiplier() walks this list
# top-to-bottom and returns the first matching multiplier.
# Boundaries are INCLUSIVE on the listed value (i.e. intensity >= threshold).
_WEATHER_TABLE: list[tuple[float, float]] = [
    (1.0,  0.0),   # complete halt / cancellation
    (0.85, 0.2),   # severe
    (0.60, 0.5),   # heavy
    (0.40, 0.7),   # moderate
    (0.20, 0.85),  # light
    (0.0,  1.0),   # clear
]


def get_speed_multiplier(intensity: float) -> float:
    """
    Map a WeatherCell intensity [0.0, 1.0] to a speed multiplier.

    Intensity tiers (each tier inclusive at its lower boundary):
        1.0         → 0.0   (halted / cancelled)
        [0.85, 1.0) → 0.2
        [0.60, 0.85)→ 0.5
        [0.40, 0.60)→ 0.7
        [0.20, 0.40)→ 0.85
        [0.0, 0.20) → 1.0   (no effect)

    Parameters
    ----------
    intensity : float
        Must be in [0.0, 1.0].  Values outside this range raise ValueError.

    Returns
    -------
    float
        Speed multiplier in [0.0, 1.0].
    """
    if not (0.0 <= intensity <= 1.0):
        raise ValueError(
            f"Weather intensity must be in [0.0, 1.0], got {intensity!r}."
        )
    for threshold, multiplier in _WEATHER_TABLE:
        if intensity >= threshold:
            return multiplier
    # Should never reach here; the table covers all of [0.0, 1.0].
    return 1.0  # pragma: no cover


# ---------------------------------------------------------------------------
# Weather application helpers
# ---------------------------------------------------------------------------


class WeatherTrainOutcome(str, Enum):
    """
    Result of applying weather to a single train.

    NORMAL     — train continues, effective speed may be reduced.
    HALTED     — intensity==1.0, train was already en-route: speed → 0,
                 train is NOT cancelled (it resumes when intensity drops).
    CANCELLED  — intensity==1.0, train had not yet departed: it is marked
                 cancelled and will never enter the network on this run.
    """

    NORMAL = "normal"
    HALTED = "halted"
    CANCELLED = "cancelled"


def apply_weather_to_train(
    train: TrainRuntime,
    block: BlockRuntime,
    network: NetworkGraph,
) -> tuple[float, WeatherTrainOutcome]:
    """
    Compute the effective speed for *train* given *block*'s current weather,
    and determine whether the train should be HALTED or CANCELLED.

    This function is pure — it returns values without side effects.  The
    ConditionsEngine applies the outcome and calls propagate_eta_update.

    Parameters
    ----------
    train : TrainRuntime
    block : BlockRuntime
        The block the train is currently on (or approaching as its first block).
    network : NetworkGraph
        Used to look up the WeatherCell attached to *block*.

    Returns
    -------
    (effective_speed_kmh, outcome)
        effective_speed_kmh : float
            min(train.max_speed, train.max_speed * weather_multiplier).
            Is 0.0 when outcome is HALTED or CANCELLED.
        outcome : WeatherTrainOutcome
    """
    cell_id = block.weather_cell_id
    if cell_id is None:
        return train.max_speed, WeatherTrainOutcome.NORMAL

    cell = network.get_weather_cell(cell_id)
    if cell is None:
        return train.max_speed, WeatherTrainOutcome.NORMAL

    multiplier = get_speed_multiplier(cell.intensity)

    if multiplier == 0.0:
        # Intensity == 1.0: branch on whether the train has departed.
        departed = train.current_block_id is not None
        if departed:
            return 0.0, WeatherTrainOutcome.HALTED
        else:
            return 0.0, WeatherTrainOutcome.CANCELLED

    effective = train.max_speed * multiplier
    return effective, WeatherTrainOutcome.NORMAL


# ---------------------------------------------------------------------------
# SpeedRestriction
# ---------------------------------------------------------------------------


@dataclass
class SpeedRestriction:
    """
    A time-bounded per-block speed cap.

    While active (start_time <= now < end_time), a train's effective speed on
    the restricted block is:

        effective = min(train_effective_speed, max_speed_kmh)

    This stacks multiplicatively with weather:

        base_speed = train.max_speed * weather_multiplier
        restricted_speed = min(base_speed, restriction.max_speed_kmh)

    Parameters
    ----------
    id : str
        Unique identifier for this restriction record.
    block_id : str
        The block this restriction applies to.
    max_speed_kmh : float
        The speed cap in km/h while active.
    start_time : datetime
        When the restriction becomes active (timezone-aware recommended).
    end_time : datetime
        When the restriction expires (exclusive: active for [start, end)).
    """

    id: str
    block_id: str
    max_speed_kmh: float
    start_time: datetime
    end_time: datetime

    def is_active(self, at: datetime) -> bool:
        """Return True if this restriction is active at *at*."""
        # Normalise to UTC-aware for safe comparison.
        at_tz = _ensure_tz(at)
        start_tz = _ensure_tz(self.start_time)
        end_tz = _ensure_tz(self.end_time)
        return start_tz <= at_tz < end_tz


def get_effective_speed(
    train_max_speed: float,
    weather_multiplier: float,
    restriction_max_speed: Optional[float],
) -> float:
    """
    Compute a train's effective speed on a block, stacking weather and
    any active speed restriction.

    Parameters
    ----------
    train_max_speed : float
        The train's own max_speed in km/h.
    weather_multiplier : float
        From get_speed_multiplier(); in [0.0, 1.0].
    restriction_max_speed : float | None
        The block's active speed restriction cap, or None if none active.

    Returns
    -------
    float
        Effective speed in km/h. Always >= 0.
    """
    base = train_max_speed * weather_multiplier
    if restriction_max_speed is not None:
        return min(base, restriction_max_speed)
    return base


# ---------------------------------------------------------------------------
# MaintenanceWindow
# ---------------------------------------------------------------------------


@dataclass
class MaintenanceWindow:
    """
    A time-bounded block/segment impassability record.

    While active, any segment that contains the blocked block(s) must be
    excluded from pathfinding.  Trains mid-route through the affected segment
    must be re-pathed around it.

    Parameters
    ----------
    id : str
    block_ids : list[str]
        One or more block ids that are impassable during the window.
    start_time : datetime
    end_time : datetime
    """

    id: str
    block_ids: list[str]
    start_time: datetime
    end_time: datetime

    def is_active(self, at: datetime) -> bool:
        at_tz = _ensure_tz(at)
        return _ensure_tz(self.start_time) <= at_tz < _ensure_tz(self.end_time)


def find_alternate_path(
    train: TrainRuntime,
    current_node_id: str,
    network: NetworkGraph,
    blocked_block_ids: set[str],
) -> list[str]:
    """
    Find an alternate route for *train* from *current_node_id* to its final
    destination, avoiding any segments that contain *blocked_block_ids*.

    This delegates entirely to network.find_path_excluding_segments — the BFS
    logic lives in network.py, not here.

    Parameters
    ----------
    train : TrainRuntime
    current_node_id : str
        The node the train is currently at or approaching.
    network : NetworkGraph
    blocked_block_ids : set[str]
        Block ids that are impassable (from active MaintenanceWindows).

    Returns
    -------
    list[str]
        Ordered segment_ids of the alternate route from current_node to final
        destination.

    Raises
    ------
    NoAlternatePathError
        If no alternate path exists.
    ConditionsError
        If the train has no route or destination.
    """
    route = train.route
    if not route:
        raise ConditionsError(
            f"Train {train.id!r} has an empty route; cannot re-path."
        )

    final_node_id = route[-1].node_id

    if current_node_id == final_node_id:
        return []  # Already at destination.

    # Collect segment_ids that contain any of the blocked blocks.
    excluded_seg_ids: set[str] = set()
    for blk_id in blocked_block_ids:
        try:
            blk = network.get_block(blk_id)
            excluded_seg_ids.add(blk.segment_id)
        except NetworkError:
            pass  # Block not in graph — ignore.

    alt_path = network.find_path_excluding_segments(
        current_node_id, final_node_id, excluded_seg_ids
    )
    if alt_path is None:
        raise NoAlternatePathError(
            f"Train {train.id!r}: no alternate path from {current_node_id!r} "
            f"to {final_node_id!r} exists while blocks "
            f"{sorted(blocked_block_ids)} are under maintenance."
        )
    return alt_path


# ---------------------------------------------------------------------------
# ConditionsEngine — owns all active conditions records
# ---------------------------------------------------------------------------


class ConditionsEngine:
    """
    Runtime manager for all active weather, speed restriction, and maintenance
    conditions.

    The engine holds:
    - Speed restrictions and maintenance windows as time-stamped records.
    - A set of cancelled train ids (trains that cannot depart due to
      intensity==1.0 weather at their first block).

    What it does NOT own
    --------------------
    * Block occupancy — that's engine.py.
    * Weather cells themselves — those live in NetworkGraph (added via
      network.add_weather_cell()).  ConditionsEngine reads them from there.
    * ETA scheduling state — that lives on TrainRuntime.schedule.

    Usage (by engine.py)
    --------------------
        conditions = ConditionsEngine()
        conditions.add_speed_restriction(sr)
        conditions.add_maintenance_window(mw)

        # On each tick or on state change:
        speed = conditions.effective_speed_for(train, block, network, sim_time)
        outcome = conditions.apply_weather(train, block, network)
    """

    def __init__(self) -> None:
        self._speed_restrictions: dict[str, SpeedRestriction] = {}
        self._maintenance_windows: dict[str, MaintenanceWindow] = {}
        self._cancelled_trains: set[str] = set()

    # -----------------------------------------------------------------------
    # Speed restriction management
    # -----------------------------------------------------------------------

    def add_speed_restriction(self, sr: SpeedRestriction) -> None:
        """Register a speed restriction record."""
        self._speed_restrictions[sr.id] = sr

    def remove_speed_restriction(self, sr_id: str) -> None:
        self._speed_restrictions.pop(sr_id, None)

    def active_restriction_for_block(
        self, block_id: str, at: datetime
    ) -> Optional[SpeedRestriction]:
        """
        Return the most restrictive (lowest cap) active SpeedRestriction for
        *block_id* at time *at*, or None if none is active.
        """
        active = [
            sr for sr in self._speed_restrictions.values()
            if sr.block_id == block_id and sr.is_active(at)
        ]
        if not active:
            return None
        return min(active, key=lambda sr: sr.max_speed_kmh)

    # -----------------------------------------------------------------------
    # Maintenance window management
    # -----------------------------------------------------------------------

    def add_maintenance_window(self, mw: MaintenanceWindow) -> None:
        self._maintenance_windows[mw.id] = mw

    def remove_maintenance_window(self, mw_id: str) -> None:
        self._maintenance_windows.pop(mw_id, None)

    def active_blocked_block_ids(self, at: datetime) -> set[str]:
        """
        Return the set of all block ids that are impassable due to active
        maintenance windows at time *at*.
        """
        blocked: set[str] = set()
        for mw in self._maintenance_windows.values():
            if mw.is_active(at):
                blocked.update(mw.block_ids)
        return blocked

    def is_block_under_maintenance(self, block_id: str, at: datetime) -> bool:
        """Return True if *block_id* is under an active maintenance window."""
        return block_id in self.active_blocked_block_ids(at)

    # -----------------------------------------------------------------------
    # Cancellation tracking
    # -----------------------------------------------------------------------

    def cancel_train(self, train_id: str) -> None:
        """Mark train as cancelled (intensity==1.0, never departed)."""
        self._cancelled_trains.add(train_id)

    def is_cancelled(self, train_id: str) -> bool:
        return train_id in self._cancelled_trains

    def reinstate_train(self, train_id: str) -> None:
        """
        Remove cancellation when weather intensity drops below 1.0 before
        the train has departed.  engine.py decides if / when to reinstate.
        """
        self._cancelled_trains.discard(train_id)

    # -----------------------------------------------------------------------
    # Combined effective speed computation
    # -----------------------------------------------------------------------

    def effective_speed_for(
        self,
        train: TrainRuntime,
        block: BlockRuntime,
        network: NetworkGraph,
        at: datetime,
    ) -> float:
        """
        Compute the effective speed for *train* on *block* at simulation time *at*.

        Stacking order:
          1. Weather multiplier (from block's WeatherCell intensity).
          2. Speed restriction cap (block-level, time-bounded).

        Returns 0.0 if weather intensity == 1.0 (HALTED or CANCELLED).

        Does NOT mutate any state — use apply_weather() to record CANCELLED status.
        """
        # 1. Weather.
        cell_id = block.weather_cell_id
        if cell_id is not None:
            cell = network.get_weather_cell(cell_id)
            multiplier = get_speed_multiplier(cell.intensity) if cell else 1.0
        else:
            multiplier = 1.0

        # 2. Speed restriction.
        sr = self.active_restriction_for_block(block.id, at)
        restriction_cap = sr.max_speed_kmh if sr is not None else None

        return get_effective_speed(train.max_speed, multiplier, restriction_cap)

    # -----------------------------------------------------------------------
    # Weather application (with side effects — records cancellation)
    # -----------------------------------------------------------------------

    def apply_weather(
        self,
        train: TrainRuntime,
        block: BlockRuntime,
        network: NetworkGraph,
    ) -> WeatherTrainOutcome:
        """
        Apply current weather on *block* to *train*, recording CANCELLED status
        in this engine if appropriate.

        Returns the WeatherTrainOutcome so the caller (engine.py) can take the
        correct action (halt physical movement, suppress departure, etc.).

        Side effects
        ------------
        * If outcome is CANCELLED, calls self.cancel_train(train.id).
        * Does NOT call propagate_eta_update — that's done by engine.py after
          this call, using the returned outcome.
        """
        _, outcome = apply_weather_to_train(train, block, network)
        if outcome == WeatherTrainOutcome.CANCELLED:
            self.cancel_train(train.id)
        return outcome

    # -----------------------------------------------------------------------
    # Maintenance re-path helper
    # -----------------------------------------------------------------------

    def find_alternate_path_for(
        self,
        train: TrainRuntime,
        current_node_id: str,
        network: NetworkGraph,
        at: datetime,
    ) -> list[str]:
        """
        Find an alternate route for *train* from *current_node_id* that avoids
        all blocks currently under active maintenance windows.

        Delegates the BFS to network.find_path_excluding_segments — not
        reimplemented here.

        Raises NoAlternatePathError if no alternate path exists.
        """
        blocked = self.active_blocked_block_ids(at)
        return find_alternate_path(train, current_node_id, network, blocked)

    def recompute_expected_arrival_after_speed_change(
        self,
        train: TrainRuntime,
        current_node_id: str,
        granted_pass_time: datetime,
        network: NetworkGraph,
        registry: ArbitrationRegistry,
        effective_speed_kmh: float,
    ) -> None:
        """
        After an effective-speed change (weather or restriction), recompute
        the train's expected_arrival values for all future nodes and exchange
        information with upcoming junction arbiters.

        This is the "recompute ETA and exchange information with upcoming nodes"
        step required by the spec for both weather and speed-restriction changes.

        Delegates to arbitration.propagate_eta_update so the cascade logic
        lives in one place.
        """
        if effective_speed_kmh <= 0.0:
            # Train is halted — set all future expected_arrivals to None
            # to signal that arrival time is indeterminate.
            route = train.route
            start_recording = False
            for hop in route:
                if hop.node_id == current_node_id:
                    start_recording = True
                    continue
                if start_recording:
                    train.update_expected_arrival(hop.node_id, None)
            return

        propagate_eta_update(
            train=train,
            current_node_id=current_node_id,
            granted_pass_time=granted_pass_time,
            network=network,
            registry=registry,
            effective_speed_kmh=effective_speed_kmh,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _ensure_tz(dt: datetime) -> datetime:
    """Return a timezone-aware copy of *dt* (UTC assumed if naive)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
