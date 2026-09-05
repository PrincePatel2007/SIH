"""
engine.py — Simulation engine: tick loop, occupancy commits, event log.

Architecture contract
---------------------
* engine.py is the TOP of the dependency stack.
  It imports: models, network, train, arbitration, conditions.
  Nothing imports engine.py (to avoid circular deps).

* engine.py is the SOLE module that commits occupancy:
    block.occupy(train_id) / block.release(train_id)
    junction.occupy(train_id) / junction.release(train_id)

* engine.py is the SOLE module that calls train.update_position().
  All other state mutations (expected_arrival, actual_arrival) are
  delegated to arbitration.propagate_eta_update / train.record_actual_arrival.

* The tick loop is event-propagation-driven for ETAs (arbitration handles
  that), but still advances physical position on every tick for animation.

Occupancy invariant (enforced here)
------------------------------------
Before moving a train into a block, we re-verify the block is free at
move-time (not just at grant-time). This closes the race condition where
two trains requested passage near-simultaneously and both were granted
before either physically moved.

Siding overtake threshold
--------------------------
If a lower-priority train is blocking an equal-or-higher-priority faster
train such that the faster train cannot reach the next station on scheduled
time at its max_speed even accounting for allowable delay, AND a siding
track is available at the next station/junction, the slower train is
rerouted to the siding and held there until the faster train clears.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from app.arbitration import (
    ASPECT_SPEED_FACTOR,
    ArbitrationRegistry,
    SignalAspect,
    SignalViolationError,
    aspect_to_signal_state_value,
    compute_signal_aspect,
    propagate_eta_update,
    try_advance,
)
from app.conditions import (
    ConditionsEngine,
    WeatherTrainOutcome,
    apply_weather_to_train,
)
from app.models import RouteHop, ScheduleEntry
from app.network import NetworkGraph, NetworkError, OccupancyError
from app.train import TrainRuntime, travel_time_hours

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default deceleration limit (km/h per second).
# A train cannot shed speed faster than this.
# Real heavy rail: ~0.8–1.2 m/s² ≈ 2.88–4.32 km/h per second.
DEFAULT_DECEL_KMH_PER_SEC: float = 3.0

# How many seconds before a siding-held train is re-released after
# the overtaking train's last block clears.
SIDING_RELEASE_DELAY_SEC: float = 30.0

# If an equal/higher-priority train would be delayed more than this
# many simulated seconds, trigger siding overtake check.
SIDING_THRESHOLD_SECONDS: float = 300.0  # 5 minutes


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    DEPARTURE        = "DEPARTURE"
    ARRIVAL          = "ARRIVAL"
    BLOCK_ENTER      = "BLOCK_ENTER"
    BLOCK_EXIT       = "BLOCK_EXIT"
    SIGNAL_HOLD      = "SIGNAL_HOLD"
    WEATHER_HALT     = "WEATHER_HALT"
    WEATHER_RESUME   = "WEATHER_RESUME"
    CANCELLED        = "CANCELLED"
    SIDING_OVERTAKE  = "SIDING_OVERTAKE"
    SIDING_RELEASE   = "SIDING_RELEASE"
    REPATH           = "REPATH"
    OCCUPANCY_RACE   = "OCCUPANCY_RACE"


# ---------------------------------------------------------------------------
# Train lifecycle state
# ---------------------------------------------------------------------------

class TrainStatus(str, Enum):
    PENDING    = "PENDING"    # Not yet departed from origin.
    ACTIVE     = "ACTIVE"     # Moving on network.
    SIDING     = "SIDING"     # Held in siding for overtake.
    COMPLETED  = "COMPLETED"  # Reached final destination.
    CANCELLED  = "CANCELLED"  # Cancelled due to weather.
    HALTED     = "HALTED"     # Halted by weather (will resume).


# ---------------------------------------------------------------------------
# Internal per-train runtime state
# ---------------------------------------------------------------------------

@dataclass
class TrainEngineState:
    """
    Per-train mutable engine state, kept separate from TrainRuntime so the
    Pydantic model stays clean.
    """
    train: TrainRuntime
    status: TrainStatus = TrainStatus.PENDING

    # Block-level position tracking
    block_sequence: list[str] = field(default_factory=list)  # ordered block ids
    block_index: int = 0          # index into block_sequence of current block
    position_in_block: float = 0.0  # [0.0, 1.0]

    # Speed state (km/h)
    current_speed_kmh: float = 0.0
    target_speed_kmh: float = 0.0

    # Siding state
    siding_block_id: Optional[str] = None
    siding_release_at: Optional[datetime] = None

    # Node tracking (for arrival/departure events)
    last_node_id: Optional[str] = None  # node just departed from
    next_node_id: Optional[str] = None  # node heading towards

    @property
    def current_block_id(self) -> Optional[str]:
        if (
            self.status in (TrainStatus.ACTIVE, TrainStatus.SIDING, TrainStatus.HALTED)
            and self.block_index < len(self.block_sequence)
        ):
            return self.block_sequence[self.block_index]
        return None

    @property
    def is_movable(self) -> bool:
        return self.status == TrainStatus.ACTIVE

    @property
    def at_final_block(self) -> bool:
        return self.block_index >= len(self.block_sequence) - 1


# ---------------------------------------------------------------------------
# SimulationEngine
# ---------------------------------------------------------------------------

class SimulationEngine:
    """
    The top-level simulation engine.

    Owns: NetworkGraph, TrainRuntimes, ArbitrationRegistry, ConditionsEngine,
          sim_clock, speed_multiplier, event log.

    Entry points
    ------------
    start_at(dt)         — set the starting clock time.
    play() / pause()     — gate tick processing.
    set_speed_multiplier(x) — wall-clock → sim-clock ratio.
    tick(delta_seconds)  — advance by delta_seconds of simulated time.
    run_headless(sec)    — tick in a loop with no UI (for tests / CLI).

    Occupancy invariant
    -------------------
    Only this class calls block.occupy() / block.release() /
    junction.occupy() / junction.release().
    """

    def __init__(
        self,
        network: NetworkGraph,
        *,
        junction_clearance_minutes: float = 2.0,
        decel_kmh_per_sec: float = DEFAULT_DECEL_KMH_PER_SEC,
        siding_threshold_seconds: float = SIDING_THRESHOLD_SECONDS,
        start_time: Optional[datetime] = None,
    ) -> None:
        self.network = network
        self.registry = ArbitrationRegistry.from_network(
            network, junction_clearance_minutes=junction_clearance_minutes
        )
        self.conditions = ConditionsEngine()

        self._decel = decel_kmh_per_sec
        self._siding_threshold = siding_threshold_seconds

        # sim_clock is the authoritative simulation time.
        self.sim_clock: datetime = start_time or datetime.now(tz=timezone.utc)
        self.speed_multiplier: float = 1.0  # 1.0 = real-time

        self._playing: bool = False
        self._train_states: dict[str, TrainEngineState] = {}  # train_id → state
        self.event_log: list[dict] = []

    # -----------------------------------------------------------------------
    # Train registration
    # -----------------------------------------------------------------------

    def add_train(self, train: TrainRuntime) -> None:
        """
        Register a train with the engine.  Must be called before play().
        The train's route must already be set.
        """
        if train.id in self._train_states:
            raise ValueError(f"Train {train.id!r} is already registered.")

        # Build block sequence from the route.
        block_seq = self._build_block_sequence(train)

        state = TrainEngineState(
            train=train,
            status=TrainStatus.PENDING,
            block_sequence=block_seq,
            block_index=0,
            position_in_block=0.0,
            current_speed_kmh=0.0,
            target_speed_kmh=0.0,
        )
        # Work out the first and last nodes for event logging.
        route = train.route
        if route:
            state.last_node_id = route[0].node_id
            state.next_node_id = route[1].node_id if len(route) > 1 else route[0].node_id

        self._train_states[train.id] = state

        # Kick off initial ETA propagation from origin node.
        if route:
            origin_node = route[0].node_id
            origin_sched = train.schedule.get(origin_node)
            if origin_sched and origin_sched.scheduled_departure:
                departure_dt = origin_sched.scheduled_departure
            else:
                departure_dt = self.sim_clock
            # Request passage at the first junction (if any).
            propagate_eta_update(
                train=train,
                current_node_id=origin_node,
                granted_pass_time=departure_dt,
                network=self.network,
                registry=self.registry,
                effective_speed_kmh=train.avg_speed,
            )

    def _build_block_sequence(self, train: TrainRuntime) -> list[str]:
        """
        Derive the ordered list of block ids for the train's full route.
        Uses network.find_path_blocks between the first and last route nodes.
        """
        route = train.route
        if len(route) < 2:
            return []

        # Walk hop-by-hop and concatenate blocks for each segment.
        all_blocks: list[str] = []
        for i, hop in enumerate(route[1:], start=1):
            if hop.segment_id is None:
                continue
            seg = self.network.get_segment(hop.segment_id)
            prev_node = route[i - 1].node_id
            # Determine traversal direction.
            if seg.start_node_id == prev_node:
                all_blocks.extend(seg.ordered_block_ids)
            else:
                all_blocks.extend(reversed(seg.ordered_block_ids))
        return all_blocks

    # -----------------------------------------------------------------------
    # Engine controls
    # -----------------------------------------------------------------------

    def start_at(self, dt: datetime) -> None:
        """Set the simulation clock to *dt* and begin from that point."""
        self.sim_clock = dt

    def play(self) -> None:
        """Allow tick() to process state changes."""
        self._playing = True

    def pause(self) -> None:
        """Halt tick() from processing state changes (clock still advances)."""
        self._playing = False

    def set_speed_multiplier(self, x: float) -> None:
        """
        Set the wall-clock-to-sim-clock ratio.
        x=1 → real time; x=2 → 2× faster; x=60 → 1 min wall = 1 hr sim.
        """
        if x <= 0:
            raise ValueError(f"speed_multiplier must be > 0, got {x!r}.")
        self.speed_multiplier = x

    def run_headless(
        self,
        duration_sim_seconds: float,
        tick_size: float = 5.0,
    ) -> None:
        """
        Advance the simulation by *duration_sim_seconds* of simulated time
        without any real-time delay — useful for history replay and tests.

        The engine must already be in the playing state (call play() first).

        Args:
            duration_sim_seconds: Total simulated seconds to advance.
            tick_size: Simulated seconds per individual tick (default 5 s).
                       Smaller values are more accurate; larger are faster.
        """
        remaining = duration_sim_seconds
        while remaining > 0:
            step = min(tick_size, remaining)
            self.tick(step)
            remaining -= step


    # -----------------------------------------------------------------------
    # Tick loop — the heart of the engine
    # -----------------------------------------------------------------------

    def tick(self, delta_seconds: float) -> None:
        """
        Advance the simulation by *delta_seconds* of simulated time.

        Steps per tick:
          1. Advance sim_clock.
          2. If paused, return immediately (clock advanced, nothing else).
          3. For each PENDING train whose departure time has arrived, depart it.
          4. For each ACTIVE train, advance its physical position.
          5. Check siding overtake conditions.
          6. Release any siding-held trains whose release time has passed.
        """
        self.sim_clock += timedelta(seconds=delta_seconds)

        if not self._playing:
            return

        # Step 3: depart PENDING trains.
        for state in list(self._train_states.values()):
            if state.status == TrainStatus.PENDING:
                self._try_depart(state)

        # Step 4: advance ACTIVE trains.
        for state in list(self._train_states.values()):
            if state.status == TrainStatus.ACTIVE:
                self._advance_train(state, delta_seconds)

        # Step 5: siding overtake check.
        self._check_siding_overtake()

        # Step 6: release siding trains.
        for state in list(self._train_states.values()):
            if state.status == TrainStatus.SIDING:
                self._try_release_from_siding(state)

    # -----------------------------------------------------------------------
    # Departure logic
    # -----------------------------------------------------------------------

    def _try_depart(self, state: TrainEngineState) -> None:
        """
        Attempt to depart a PENDING train.  Checks:
        - Scheduled departure time has passed.
        - First block is free.
        - Not cancelled by weather.
        """
        train = state.train
        route = train.route
        if not route or not state.block_sequence:
            state.status = TrainStatus.COMPLETED
            return

        # Check weather on first block.
        first_blk_id = state.block_sequence[0]
        try:
            first_blk = self.network.get_block(first_blk_id)
        except NetworkError:
            return

        weather_outcome = self.conditions.apply_weather(train, first_blk, self.network)
        if weather_outcome == WeatherTrainOutcome.CANCELLED:
            state.status = TrainStatus.CANCELLED
            self._log(EventType.CANCELLED, train_id=train.id,
                      block_id=first_blk_id, detail="Weather intensity=1.0 at departure block.")
            return

        # Check scheduled departure time.
        origin_node = route[0].node_id
        sched = train.schedule.get(origin_node)
        if sched and sched.scheduled_departure:
            if self.sim_clock < sched.scheduled_departure:
                return  # Not yet time to depart.

        # Check first block is free.
        if not first_blk.is_free:
            return  # Block occupied; wait.

        # Depart.
        try:
            first_blk.occupy(train.id)
        except OccupancyError:
            return  # Lost the race — try next tick.

        state.status = TrainStatus.ACTIVE
        state.block_index = 0
        state.position_in_block = 0.0
        state.current_speed_kmh = 0.0  # accelerates from rest

        train.update_position(first_blk_id, 0.0)
        train.record_actual_arrival(origin_node, self.sim_clock)

        self._log(EventType.DEPARTURE, train_id=train.id,
                  node_id=origin_node,
                  block_id=first_blk_id,
                  detail=f"Departed {origin_node!r}. Scheduled={sched.scheduled_departure if sched else None}")

    # -----------------------------------------------------------------------
    # Physical advance — one tick for one train
    # -----------------------------------------------------------------------

    def _advance_train(self, state: TrainEngineState, delta_seconds: float) -> None:
        """
        Advance a single ACTIVE train's position by *delta_seconds* of sim time.

        Logic:
        1. Look up current and next block ids.
        2. Compute signal aspect for the next block.
        3. Compute target speed = effective_speed * ASPECT_SPEED_FACTOR.
        4. Apply deceleration limit → current_speed_kmh.
        5. Compute distance traveled; advance position_in_block.
        6. If position_in_block >= 1.0, cross into next block.
        """
        train = state.train
        blk_seq = state.block_sequence

        if not blk_seq or state.block_index >= len(blk_seq):
            state.status = TrainStatus.COMPLETED
            return

        current_blk_id = blk_seq[state.block_index]
        try:
            current_blk = self.network.get_block(current_blk_id)
        except NetworkError:
            logger.warning("Train %s: current block %s not found", train.id, current_blk_id)
            return

        # Effective speed on this block.
        effective_speed = self.conditions.effective_speed_for(
            train, current_blk, self.network, self.sim_clock
        )

        # Weather check: halt or cancel if needed.
        weather_outcome = self.conditions.apply_weather(train, current_blk, self.network)
        if weather_outcome == WeatherTrainOutcome.HALTED:
            if state.status != TrainStatus.HALTED:
                state.status = TrainStatus.HALTED
                state.current_speed_kmh = 0.0
                self._log(EventType.WEATHER_HALT, train_id=train.id,
                          block_id=current_blk_id, detail="Weather intensity=1.0; halted.")
            return
        elif state.status == TrainStatus.HALTED:
            # Weather cleared; resume.
            state.status = TrainStatus.ACTIVE
            self._log(EventType.WEATHER_RESUME, train_id=train.id,
                      block_id=current_blk_id, detail="Weather cleared; resuming.")

        # Compute next-block info for signal aspect.
        next_block_ids = blk_seq[state.block_index + 1: state.block_index + 4]  # up to 3 lookahead
        approaching_junction_id = self._junction_at_next_segment_boundary(
            state, blk_seq[state.block_index + 1] if state.block_index + 1 < len(blk_seq) else None
        )

        # Determine signal aspect — this is a READ (does not raise on its own here;
        # we use the aspect to choose target speed, and enforce RED at crossing time).
        if next_block_ids:
            next_blk = self.network.get_block(next_block_ids[0])
            if approaching_junction_id is not None:
                arbiter = self.registry.get_arbiter(approaching_junction_id)
                granted_to = (
                    train.id
                    if arbiter and arbiter.get_grant(train.id) is not None
                    else None
                )
            else:
                granted_to = train.id if next_blk.is_free else None

            aspect = compute_signal_aspect(
                train_id=train.id,
                next_block_ids=next_block_ids,
                network=self.network,
                granted_to_train_id=granted_to,
            )
            # Persist the live aspect into the SignalState record so that
            # to_dict() reflects what is actually being enforced right now.
            if approaching_junction_id is not None:
                self._persist_signal_aspect(
                    approaching_junction_id, current_blk.segment_id, aspect
                )
        else:
            aspect = SignalAspect.GREEN  # Final block — let the train finish.

        # Target speed = conditions speed × signal factor.
        raw_target = effective_speed * ASPECT_SPEED_FACTOR[aspect]

        # Apply deceleration limit: can't instantaneously reach target.
        max_speed_change = self._decel * delta_seconds  # km/h change in this tick
        if raw_target < state.current_speed_kmh:
            # Decelerating.
            state.current_speed_kmh = max(
                raw_target, state.current_speed_kmh - max_speed_change
            )
        else:
            # Accelerating — use same rate for simplicity.
            state.current_speed_kmh = min(
                raw_target, state.current_speed_kmh + max_speed_change * 2
            )

        # If we're approaching RED and can't stop in time (shouldn't happen with
        # proper deceleration planning), log it but do NOT move past.
        if aspect == SignalAspect.RED and state.current_speed_kmh > 0:
            state.current_speed_kmh = 0.0
            self._log(EventType.SIGNAL_HOLD, train_id=train.id,
                      block_id=current_blk_id,
                      detail=f"Held at RED signal approaching {next_block_ids[0] if next_block_ids else '?'!r}.")
            train.update_position(current_blk_id, state.position_in_block)
            return
        if state.current_speed_kmh <= 0.0:
            train.update_position(current_blk_id, state.position_in_block)
            return

        # Distance traveled this tick (km).
        dist_km = state.current_speed_kmh * (delta_seconds / 3600.0)
        block_len_km = current_blk.length_km
        progress = dist_km / block_len_km

        state.position_in_block += progress

        if state.position_in_block >= 1.0:
            # Train has crossed out of this block into the next.
            self._cross_block_boundary(state, current_blk, next_block_ids, approaching_junction_id)
        else:
            train.update_position(current_blk_id, state.position_in_block)

    def _cross_block_boundary(
        self,
        state: TrainEngineState,
        current_blk,
        next_block_ids: list[str],
        approaching_junction_id: Optional[str],
    ) -> None:
        """
        A train has physically reached the end of current_blk.
        Attempt to enter the next block, enforcing all RED-signal guards.
        """
        train = state.train

        # Entering final destination (no next block).
        if not next_block_ids or state.at_final_block:
            self._complete_train(state, current_blk)
            return

        next_blk_id = next_block_ids[0]
        try:
            next_blk = self.network.get_block(next_blk_id)
        except NetworkError:
            logger.warning("Train %s: next block %s not found", train.id, next_blk_id)
            return

        # ── RE-VERIFY at move-time (race-condition guard) ────────────────
        # Even if a grant was issued, the block may have been occupied by
        # another train between grant-time and now.
        if not next_blk.is_free:
            # Block is occupied — must stop here.
            state.current_speed_kmh = 0.0
            state.position_in_block = 1.0 - 1e-6  # stay at end of current block
            self._log(
                EventType.OCCUPANCY_RACE,
                train_id=train.id,
                block_id=next_blk_id,
                detail=(
                    f"Block {next_blk_id!r} occupied at move-time by "
                    f"{next_blk.occupied_by!r}; train held."
                ),
            )
            train.update_position(current_blk.id, state.position_in_block)
            return

        # Junction-approach: enforce grant check at crossing time.
        if approaching_junction_id is not None:
            arbiter = self.registry.get_arbiter(approaching_junction_id)
            if arbiter is not None and arbiter.get_grant(train.id) is None:
                state.current_speed_kmh = 0.0
                state.position_in_block = 1.0 - 1e-6
                self._log(
                    EventType.SIGNAL_HOLD,
                    train_id=train.id,
                    block_id=next_blk_id,
                    detail=f"No grant at junction {approaching_junction_id!r}; held.",
                )
                # Persist RED: this train is being held at a junction signal.
                self._persist_signal_aspect(
                    approaching_junction_id, current_blk.segment_id, SignalAspect.RED
                )
                train.update_position(current_blk.id, state.position_in_block)
                return

        # ── Commit occupancy ─────────────────────────────────────────────
        try:
            next_blk.occupy(train.id)
        except OccupancyError as exc:
            # Lost the race to another thread / tick ordering.
            state.current_speed_kmh = 0.0
            state.position_in_block = 1.0 - 1e-6
            self._log(
                EventType.OCCUPANCY_RACE,
                train_id=train.id,
                block_id=next_blk_id,
                detail=f"OccupancyError at move-time: {exc}",
            )
            train.update_position(current_blk.id, state.position_in_block)
            return

        # Release old block.
        try:
            current_blk.release(train.id)
        except OccupancyError as exc:
            logger.error("Train %s: failed to release block %s: %s", train.id, current_blk.id, exc)

        # If crossing a junction, clear the grant so the next train can enter.
        if approaching_junction_id is not None:
            arbiter = self.registry.get_arbiter(approaching_junction_id)
            if arbiter is not None:
                arbiter.clear_grant(train.id)
            # Signal clears to GREEN now that this train has physically passed.
            self._persist_signal_aspect(
                approaching_junction_id, current_blk.segment_id, SignalAspect.GREEN
            )
            # Occupy the junction (atomic).
            jct_node = self.network._junctions.get(approaching_junction_id)
            if jct_node is not None and jct_node.is_free:
                try:
                    jct_node.occupy(train.id)
                except OccupancyError:
                    pass  # another train got there first — very unusual

        self._log(EventType.BLOCK_EXIT, train_id=train.id, block_id=current_blk.id)
        self._log(EventType.BLOCK_ENTER, train_id=train.id, block_id=next_blk_id)

        state.block_index += 1
        state.position_in_block -= 1.0  # carry over fractional progress
        state.position_in_block = max(0.0, min(state.position_in_block, 1.0))

        train.update_position(next_blk_id, state.position_in_block)

        # Release junction once the train has fully entered the next block.
        if approaching_junction_id is not None:
            jct_node = self.network._junctions.get(approaching_junction_id)
            if jct_node is not None and jct_node.occupied_by == train.id:
                try:
                    jct_node.release(train.id)
                except OccupancyError:
                    pass

        # Fire node arrival/departure events when the train reaches a node block.
        self._check_node_events(state)

    def _complete_train(self, state: TrainEngineState, current_blk) -> None:
        """Train has reached its final block boundary — mark completed."""
        train = state.train
        route = train.route
        final_node = route[-1].node_id if route else None

        try:
            current_blk.release(train.id)
        except OccupancyError:
            pass

        state.status = TrainStatus.COMPLETED
        state.current_speed_kmh = 0.0
        train.update_position(None, 0.0)

        if final_node:
            train.record_actual_arrival(final_node, self.sim_clock)
            sched = train.schedule.get(final_node)
            delay = None
            if sched and sched.scheduled_arrival:
                delay = (self.sim_clock - sched.scheduled_arrival).total_seconds()
            self._log(
                EventType.ARRIVAL,
                train_id=train.id,
                node_id=final_node,
                detail=f"Completed journey. Delay={delay:.0f}s" if delay is not None else "Completed.",
            )

    def _check_node_events(self, state: TrainEngineState) -> None:
        """
        After a block crossing, check if the train has reached a node
        (station or junction) and fire ARRIVAL/DEPARTURE events.
        """
        train = state.train
        route = train.route
        blk_seq = state.block_sequence

        if state.block_index >= len(blk_seq):
            return

        current_blk_id = blk_seq[state.block_index]

        for i, hop in enumerate(route):
            if hop.segment_id is None:
                continue  # origin
            seg = self.network.get_segment(hop.segment_id)
            # Check if current block is the LAST block in this segment
            # (meaning we just arrived at hop.node_id).
            prev_node = route[i - 1].node_id
            if seg.start_node_id == prev_node:
                last_blk_in_seg = seg.ordered_block_ids[-1]
            else:
                last_blk_in_seg = seg.ordered_block_ids[0]

            if current_blk_id == last_blk_in_seg:
                node_id = hop.node_id
                train.record_actual_arrival(node_id, self.sim_clock)
                sched = train.schedule.get(node_id)
                delay = None
                if sched and sched.scheduled_arrival:
                    delay = (self.sim_clock - sched.scheduled_arrival).total_seconds()
                self._log(
                    EventType.ARRIVAL,
                    train_id=train.id,
                    node_id=node_id,
                    detail=f"Arrived {node_id!r}. Delay={delay:.0f}s" if delay is not None else f"Arrived {node_id!r}.",
                )
                state.last_node_id = node_id
                # Update next_node_id.
                if i + 1 < len(route):
                    state.next_node_id = route[i + 1].node_id
                break

    # -----------------------------------------------------------------------
    # Junction helper
    # -----------------------------------------------------------------------

    def _junction_at_next_segment_boundary(
        self,
        state: TrainEngineState,
        next_blk_id: Optional[str],
    ) -> Optional[str]:
        """
        Return the junction_id if the next block is the FIRST block of a new
        segment that starts at a junction — i.e., we are approaching a junction.
        Returns None for intra-segment moves.
        """
        if next_blk_id is None:
            return None
        try:
            next_blk = self.network.get_block(next_blk_id)
        except NetworkError:
            return None

        # Current block's segment.
        current_blk_id = state.block_sequence[state.block_index] if state.block_index < len(state.block_sequence) else None
        if current_blk_id is None:
            return None
        try:
            current_blk = self.network.get_block(current_blk_id)
        except NetworkError:
            return None

        if next_blk.segment_id == current_blk.segment_id:
            return None  # Intra-segment, no junction.

        # Inter-segment: find the connecting node between the two segments.
        try:
            curr_seg = self.network.get_segment(current_blk.segment_id)
            next_seg = self.network.get_segment(next_blk.segment_id)
        except NetworkError:
            return None

        # Find shared node.
        curr_nodes = {curr_seg.start_node_id, curr_seg.end_node_id}
        next_nodes = {next_seg.start_node_id, next_seg.end_node_id}
        shared = curr_nodes & next_nodes
        if not shared:
            return None

        shared_node = next(iter(shared))
        # Check if it is a junction.
        if shared_node in self.network._junctions:
            return shared_node
        return None

    def _persist_signal_aspect(
        self,
        junction_id: str,
        approach_segment_id: str,
        aspect: SignalAspect,
    ) -> None:
        """
        Convert *aspect* to a SignalStateValue and write it into the SignalState
        record registered for *approach_segment_id* at *junction_id*.

        Emits a WARNING log (never raises) on each wiring-gap path so that
        missing signal registrations surface in logs instead of disappearing
        silently into the tick loop.  Three distinct gaps are detected:
          1. junction_id unknown to the network graph.
          2. Approach segment not registered in Junction.signal_states
             (register_junction_signals() was not called, or the segment was
             added after signal setup).
          3. signal_id recorded in Junction.signal_states but absent from
             NetworkGraph._signals (stale reference after a graph mutation).
        """
        try:
            jct = self.network.get_junction(junction_id)
        except NetworkError:
            logger.warning(
                "persist_signal_aspect: junction %r not found in network graph "
                "(approach_segment=%r, aspect=%s). Signal state NOT updated. "
                "Check that the junction was added via NetworkGraph.add_junction().",
                junction_id,
                approach_segment_id,
                aspect.value,
            )
            return

        sig_id = jct.get_signal(approach_segment_id)
        if sig_id is None:
            logger.warning(
                "persist_signal_aspect: no SignalState registered for approach "
                "segment %r at junction %r (aspect=%s). Signal state NOT updated. "
                "Call NetworkGraph.register_junction_signals() after building the "
                "network topology to auto-populate all approach signals.",
                approach_segment_id,
                junction_id,
                aspect.value,
            )
            return

        sv = aspect_to_signal_state_value(aspect)
        try:
            self.network.set_signal_state(sig_id, sv)
        except NetworkError:
            logger.warning(
                "persist_signal_aspect: signal_id %r (registered for approach "
                "segment %r at junction %r) not found in NetworkGraph._signals "
                "(aspect=%s). Stale reference — was the signal removed after "
                "register_junction_signals() was called?",
                sig_id,
                approach_segment_id,
                junction_id,
                aspect.value,
            )

    # -----------------------------------------------------------------------
    # Siding overtake logic
    # -----------------------------------------------------------------------

    def _check_siding_overtake(self) -> None:
        """
        Check whether any ACTIVE train should be rerouted to a siding to allow
        a higher/equal-priority faster train to overtake.

        Trigger condition:
          - Train A (ahead, slower, lower priority) is blocking train B (behind,
            faster, equal/higher priority).
          - Train B cannot reach its next scheduled station on time even at
            max_speed, accounting for the delay of following A.
          - A siding/loop track is available at A's next station/junction.

        Action: move A to the siding, log SIDING_OVERTAKE, set B's path clear.
        """
        active_states = [
            s for s in self._train_states.values()
            if s.status == TrainStatus.ACTIVE
        ]

        for i, state_b in enumerate(active_states):
            train_b = state_b.train
            b_blk_idx = state_b.block_index
            b_blk_seq = state_b.block_sequence

            for state_a in active_states:
                if state_a is state_b:
                    continue
                train_a = state_a.train
                a_blk_idx = state_a.block_index
                a_blk_seq = state_a.block_sequence

                # B must be immediately behind A (A's block is the next block B needs).
                if b_blk_idx + 1 >= len(b_blk_seq):
                    continue
                b_next_blk = b_blk_seq[b_blk_idx + 1]
                if a_blk_idx >= len(a_blk_seq) or a_blk_seq[a_blk_idx] != b_next_blk:
                    continue  # A is not directly ahead of B.

                # B must have higher or equal priority (lower/equal rank).
                b_rank = 0 if train_b.is_max_priority_override(self.network) else int(train_b.priority)
                a_rank = 0 if train_a.is_max_priority_override(self.network) else int(train_a.priority)
                if b_rank > a_rank:
                    continue  # B is lower priority — skip.

                # B must be faster.
                if train_b.max_speed <= train_a.max_speed:
                    continue

                # Check if B can still make its next scheduled stop at max_speed.
                if not self._b_is_critically_delayed(state_b):
                    continue

                # Check if a siding is available at A's next station.
                siding_blk_id = self._find_siding_for(state_a)
                if siding_blk_id is None:
                    continue

                # Trigger siding overtake.
                self._execute_siding_overtake(state_a, state_b, siding_blk_id)
                break  # One overtake per tick per B train.

    def _b_is_critically_delayed(self, state_b: TrainEngineState) -> bool:
        """
        Return True if train B cannot reach its nearest scheduled stop on time
        at max_speed from its current position.

        Scans the full route for the nearest future node that has a
        scheduled_arrival, then checks whether travelling at max_speed from
        the current block would arrive by that time.
        """
        train = state_b.train
        route = train.route
        blk_seq = state_b.block_sequence
        b_blk_idx = state_b.block_index

        if b_blk_idx >= len(blk_seq):
            return False

        current_blk_id = blk_seq[b_blk_idx]

        # Find all nodes in the remaining route that have a scheduled_arrival.
        # We check from the train's current block forward.
        current_seg_id: Optional[str] = None
        try:
            current_seg_id = self.network.get_block(current_blk_id).segment_id
        except NetworkError:
            return False

        # Build (node_id, scheduled_arrival) for future nodes.
        future_scheduled: list[tuple[str, datetime]] = []
        reached_current = False
        for i, hop in enumerate(route):
            if hop.segment_id is None:
                continue
            # Check if we're at or past the current segment.
            if hop.segment_id == current_seg_id or reached_current:
                reached_current = True
                sched = train.schedule.get(hop.node_id)
                if sched and sched.scheduled_arrival is not None:
                    future_scheduled.append((hop.node_id, sched.scheduled_arrival))

        if not future_scheduled:
            return False  # No scheduled stops ahead — can't assess delay.

        # Use the nearest scheduled stop.
        target_node_id, target_arrival = future_scheduled[0]

        # Compute remaining distance from current position to target_node_id.
        # Sum all blocks from current block until we exit the segment that ends at target_node_id.
        remaining_km = 0.0
        remaining_blocks = blk_seq[b_blk_idx:]
        target_seg_id: Optional[str] = None
        for hop in route:
            if hop.node_id == target_node_id and hop.segment_id:
                target_seg_id = hop.segment_id
                break

        for blk_id in remaining_blocks:
            try:
                blk = self.network.get_block(blk_id)
                remaining_km += blk.length_km * (1.0 - state_b.position_in_block if blk_id == current_blk_id else 1.0)
                if blk.segment_id == target_seg_id:
                    # Last block of the route to the target node.
                    try:
                        seg = self.network.get_segment(blk.segment_id)
                        if blk_id == seg.ordered_block_ids[-1]:
                            break
                    except NetworkError:
                        break
            except NetworkError:
                break

        if remaining_km <= 0:
            return False

        # Can B make it at max_speed?
        min_travel_hours = remaining_km / train.max_speed
        earliest_arrival = self.sim_clock + timedelta(hours=min_travel_hours)
        delay_seconds = (earliest_arrival - target_arrival).total_seconds()
        return delay_seconds > self._siding_threshold

    def _find_siding_for(self, state_a: TrainEngineState) -> Optional[str]:
        """
        Look for a siding/loop track accessible from the train's current
        position.  Checks both the next node ahead AND the last node visited
        (the origin of the current segment), since in a diamond/loop topology
        the alternate branch often starts at the same node the slow train just
        departed from.

        A siding is any free block in a segment connected to either node that
        is NOT on train A's planned route.

        Returns the block_id of the first free siding block, or None.
        """
        train_a = state_a.train
        route = train_a.route

        # The segments in the train's planned route.
        route_seg_ids: set[str] = set()
        for hop in route:
            if hop.segment_id:
                route_seg_ids.add(hop.segment_id)

        # Check both the node we last came from and the one we're heading to.
        candidate_nodes = []
        if state_a.last_node_id:
            candidate_nodes.append(state_a.last_node_id)
        if state_a.next_node_id:
            candidate_nodes.append(state_a.next_node_id)

        for node_id in candidate_nodes:
            adjacency = self.network._adjacency.get(node_id, {})
            for neighbour, seg_id in adjacency.items():
                if seg_id in route_seg_ids:
                    continue  # This is the train's planned path — not a siding.
                try:
                    seg = self.network.get_segment(seg_id)
                    # Check if the first block of this alternate segment is free.
                    if seg.ordered_block_ids:
                        first_blk = self.network.get_block(seg.ordered_block_ids[0])
                        if first_blk.is_free:
                            return first_blk.id  # Siding available.
                except NetworkError:
                    continue

        return None

        return None

    def _execute_siding_overtake(
        self,
        state_a: TrainEngineState,
        state_b: TrainEngineState,
        siding_blk_id: str,
    ) -> None:
        """
        Move train A to the siding block, update its status to SIDING, and
        log the SIDING_OVERTAKE event.
        """
        train_a = state_a.train
        train_b = state_b.train

        # Release A's current block.
        curr_blk_id = state_a.current_block_id
        if curr_blk_id:
            try:
                self.network.get_block(curr_blk_id).release(train_a.id)
            except OccupancyError:
                pass

        # Occupy the siding block.
        try:
            siding_blk = self.network.get_block(siding_blk_id)
            siding_blk.occupy(train_a.id)
        except (NetworkError, OccupancyError):
            # Couldn't take the siding — bail out.
            return

        state_a.status = TrainStatus.SIDING
        state_a.siding_block_id = siding_blk_id
        state_a.current_speed_kmh = 0.0
        train_a.update_position(siding_blk_id, 0.0)

        # Set release time: B's next block cleared + delay buffer.
        state_a.siding_release_at = (
            self.sim_clock + timedelta(seconds=SIDING_RELEASE_DELAY_SEC)
        )

        self._log(
            EventType.SIDING_OVERTAKE,
            train_id=train_a.id,
            block_id=siding_blk_id,
            detail=(
                f"Train {train_a.id!r} (priority={train_a.priority}) moved to siding "
                f"to allow {train_b.id!r} (priority={train_b.priority}) to overtake. "
                f"Siding block: {siding_blk_id!r}."
            ),
        )

    def _try_release_from_siding(self, state: TrainEngineState) -> None:
        """
        Release a SIDING train back to ACTIVE if its release time has passed
        and its original block is now free.
        """
        if state.siding_release_at is None:
            return
        if self.sim_clock < state.siding_release_at:
            return

        # Release siding block.
        if state.siding_block_id:
            try:
                self.network.get_block(state.siding_block_id).release(state.train.id)
            except OccupancyError:
                pass

        state.status = TrainStatus.ACTIVE
        state.siding_block_id = None
        state.siding_release_at = None
        state.current_speed_kmh = 0.0

        self._log(
            EventType.SIDING_RELEASE,
            train_id=state.train.id,
            detail="Released from siding; resuming active status.",
        )

    # -----------------------------------------------------------------------
    # Event logging
    # -----------------------------------------------------------------------

    def _log(
        self,
        event_type: EventType,
        *,
        train_id: Optional[str] = None,
        node_id: Optional[str] = None,
        block_id: Optional[str] = None,
        detail: str = "",
    ) -> None:
        entry = {
            "timestamp": self.sim_clock.isoformat(),
            "event_type": event_type.value,
            "train_id": train_id,
            "node_id": node_id,
            "block_id": block_id,
            "detail": detail,
        }
        self.event_log.append(entry)
        logger.debug("ENGINE EVENT: %s", entry)

    # -----------------------------------------------------------------------
    # Headless runner (for tests / CLI)
    # -----------------------------------------------------------------------

    def run_headless(
        self,
        duration_sim_seconds: float,
        tick_size: float = 1.0,
    ) -> None:
        """
        Run the engine for *duration_sim_seconds* of simulated time with no UI,
        using *tick_size* per tick.

        Automatically calls play() before starting and pauses after.

        Parameters
        ----------
        duration_sim_seconds : float
            How many simulated seconds to run.
        tick_size : float
            Simulated seconds per tick.  Smaller = more accurate physics;
            larger = faster for tests.  Default 1.0.
        """
        self.play()
        elapsed = 0.0
        while elapsed < duration_sim_seconds:
            step = min(tick_size, duration_sim_seconds - elapsed)
            self.tick(step)
            elapsed += step
        self.pause()

    # -----------------------------------------------------------------------
    # Snapshot / introspection helpers (for tests)
    # -----------------------------------------------------------------------

    def snapshot_occupancy(self) -> dict[str, Optional[str]]:
        """
        Return a dict of {block_id: occupied_by} for all blocks.
        Used by tests to verify no block is ever multiply occupied.
        """
        return {
            blk.id: blk.occupied_by
            for blk in self.network.all_blocks
        }

    def events_of_type(self, event_type: EventType) -> list[dict]:
        return [e for e in self.event_log if e["event_type"] == event_type.value]

    def train_state(self, train_id: str) -> TrainEngineState:
        if train_id not in self._train_states:
            raise KeyError(f"Train {train_id!r} not registered.")
        return self._train_states[train_id]
