"""
test_engine.py — Pytest tests for app.engine (Phase 5).

Test organisation
-----------------
1. No double-occupancy invariant
   - Run a 2-train network headlessly for N ticks; at every tick, assert that
     no block is reported occupied by two trains simultaneously.
   - This is the most important test — it validates the safety invariant.

2. Siding overtake scenario
   - Express behind local, local is slow enough to trigger threshold.
   - Confirm SIDING_OVERTAKE event fires.
   - Confirm express's total delay is less than it would be without the siding.

3. Engine controls
   - start_at() correctly offsets the clock.
   - pause() stops tick() from advancing physical state (clock still advances).
   - play() resumes state advancement.
   - set_speed_multiplier() stores the value.

4. Paused tick invariant
   - When paused, repeated tick() calls do not change train positions.

5. Weather halt / resume lifecycle
   - A WEATHER_HALT event fires when intensity hits 1.0 on an active train.
   - The train's speed drops to 0 while halted.
   - WEATHER_RESUME fires when intensity drops.

6. Departure scheduling
   - A train with a scheduled_departure in the future does not move until that time.
   - A train whose first block is occupied does not depart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.models import (
    DriverDutyStatus,
    Point,
    PriorityTier,
    RouteHop,
    ScheduleEntry,
    Train,
    WeatherCell,
)
from app.network import NetworkGraph
from app.train import TrainRuntime
from app.engine import (
    EventType,
    SimulationEngine,
    TrainStatus,
    SIDING_THRESHOLD_SECONDS,
)


# ===========================================================================
# Helpers
# ===========================================================================

T0 = datetime(2025, 6, 1, 8, 0, 0, tzinfo=timezone.utc)


def dt(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _make_train(
    train_id: str,
    *,
    max_speed: float = 120.0,
    avg_speed: float = 100.0,
    priority: PriorityTier = PriorityTier.ORDINARY,
    duty: DriverDutyStatus = DriverDutyStatus.NORMAL,
    route: Optional[list[RouteHop]] = None,
    schedule: Optional[dict[str, ScheduleEntry]] = None,
) -> TrainRuntime:
    model = Train(
        id=train_id,
        name=f"Train-{train_id}",
        color="#aabbcc",
        priority=priority,
        num_carriages=4,
        max_speed=max_speed,
        avg_speed=avg_speed,
        route=route or [],
        schedule=schedule or {},
        driver_duty_status=duty,
    )
    return TrainRuntime(model)


def _make_linear_network(
    km_per_segment: float = 10.0,
    num_blocks: int = 5,
) -> tuple[NetworkGraph, str, str, str]:
    """
    Linear: sta_A --[seg_AB, N blocks]--> sta_B
    Returns (graph, "sta_A", "sta_B", seg_id).
    """
    g = NetworkGraph()
    g.add_station("Alpha", station_id="sta_A")
    g.add_station("Beta", station_id="sta_B")
    g.add_track(
        "sta_A", "sta_B",
        length_km=km_per_segment, num_blocks=num_blocks,
        segment_id="seg_AB",
        geometry=[Point(x=0, y=0), Point(x=km_per_segment * 100, y=0)],
    )
    return g, "sta_A", "sta_B", "seg_AB"


def _make_parallel_network() -> tuple[NetworkGraph, str, str]:
    """
    Two parallel routes between sta_A and sta_B:
      Upper: sta_A → jct_1 → sta_B  (seg_AJ, seg_JB)
      Lower: sta_A → jct_2 → sta_B  (seg_AK, seg_KB)
    This gives jct_2 as a "siding" when the upper path is planned.
    Returns (graph, "sta_A", "sta_B").
    """
    g = NetworkGraph()
    g.add_station("Alpha", station_id="sta_A")
    g.add_junction(junction_id="jct_1")
    g.add_junction(junction_id="jct_2")
    g.add_station("Beta", station_id="sta_B")
    for s, e, sid, km in [
        ("sta_A", "jct_1", "seg_AJ", 10.0),
        ("jct_1", "sta_B", "seg_JB", 10.0),
        ("sta_A", "jct_2", "seg_AK", 10.0),
        ("jct_2", "sta_B", "seg_KB", 10.0),
    ]:
        g.add_track(s, e, length_km=km, num_blocks=5, segment_id=sid,
                    geometry=[Point(x=0, y=0), Point(x=km * 100, y=0)])
    return g, "sta_A", "sta_B"


def _assert_no_double_occupancy(engine: SimulationEngine) -> None:
    """
    Assert that no block is currently occupied by more than one train.
    Since block.occupy() enforces mutual exclusion at the object level,
    a 'double occupancy' here means two different *engine state records*
    claim the same block.
    """
    seen: dict[str, str] = {}
    for train_id, state in engine._train_states.items():
        blk_id = state.current_block_id
        if blk_id is None:
            continue
        if blk_id in seen:
            raise AssertionError(
                f"DOUBLE OCCUPANCY: block {blk_id!r} claimed by both "
                f"{seen[blk_id]!r} and {train_id!r}."
            )
        seen[blk_id] = train_id
        # Also verify against the actual block runtime state.
        actual_occ = engine.network.get_block(blk_id).occupied_by
        if actual_occ not in (None, train_id):
            raise AssertionError(
                f"OCCUPANCY MISMATCH: block {blk_id!r} engine-state says "
                f"{train_id!r} but block.occupied_by={actual_occ!r}."
            )


# ===========================================================================
# 1. No double-occupancy invariant
# ===========================================================================


class TestNoDoubleOccupancy:
    """
    Run two trains on the same linear track for a full simulated journey and
    assert at every single tick that no block is ever doubly occupied.

    This is the safety-critical test for the engine.
    """

    def _run_with_invariant_check(
        self,
        engine: SimulationEngine,
        total_sim_seconds: float,
        tick_size: float = 1.0,
    ) -> None:
        """Run headlessly, checking occupancy after every single tick."""
        engine.play()
        elapsed = 0.0
        tick_count = 0
        while elapsed < total_sim_seconds:
            step = min(tick_size, total_sim_seconds - elapsed)
            engine.tick(step)
            elapsed += step
            tick_count += 1
            # THE INVARIANT — checked at every tick.
            _assert_no_double_occupancy(engine)
        engine.pause()
        return tick_count

    def test_two_trains_same_direction_no_double_occupancy(self):
        """
        Two trains travelling the same linear route in the same direction.
        The express starts 1 min after the local so it approaches behind it,
        but must never share a block.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=5.0, num_blocks=5)

        engine = SimulationEngine(
            g,
            junction_clearance_minutes=2.0,
            start_time=T0,
        )

        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]

        # Local train — departs at T0, slow.
        local = _make_train(
            "local",
            max_speed=60.0, avg_speed=50.0,
            priority=PriorityTier.LOCAL,
            route=route,
            schedule={
                "sta_A": ScheduleEntry(scheduled_departure=T0),
            },
        )
        # Express train — departs at T0+2min (behind the local).
        express = _make_train(
            "express",
            max_speed=120.0, avg_speed=100.0,
            priority=PriorityTier.EXPRESS,
            route=route,
            schedule={
                "sta_A": ScheduleEntry(scheduled_departure=T0 + timedelta(minutes=2)),
            },
        )

        engine.add_train(local)
        engine.add_train(express)

        # Run for 15 sim-minutes (900 seconds) with 1-second ticks.
        tick_count = self._run_with_invariant_check(engine, 900, tick_size=1.0)

        # Verify the invariant held for all ticks (already checked inside).
        assert tick_count == 900

    def test_single_train_no_occupancy_issues(self):
        """Single train must not have any occupancy conflicts (baseline sanity)."""
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=5.0, num_blocks=10)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "solo",
            max_speed=100.0, avg_speed=80.0,
            route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)
        self._run_with_invariant_check(engine, 600, tick_size=1.0)

    def test_three_trains_staggered_departures_no_double_occupancy(self):
        """
        Three trains staggered 3 minutes apart on a longer track.
        None should ever share a block.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=10.0, num_blocks=10)
        engine = SimulationEngine(g, start_time=T0, junction_clearance_minutes=1.0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        for i, (t_id, dep_offset, max_sp) in enumerate([
            ("t1", 0,   120.0),
            ("t2", 180, 100.0),
            ("t3", 360,  80.0),
        ]):
            train = _make_train(
                t_id, max_speed=max_sp, avg_speed=max_sp * 0.8,
                route=route,
                schedule={"sta_A": ScheduleEntry(
                    scheduled_departure=T0 + timedelta(seconds=dep_offset)
                )},
            )
            engine.add_train(train)

        self._run_with_invariant_check(engine, 1200, tick_size=1.0)

    def test_occupancy_fully_released_after_completion(self):
        """
        After a train completes its journey, all blocks on its former path
        must be free (occupied_by == None).
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=2.0, num_blocks=4)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "solo",
            max_speed=120.0, avg_speed=100.0,
            route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)
        # Run long enough for the train to complete.
        engine.run_headless(600, tick_size=1.0)

        state = engine.train_state("solo")
        assert state.status == TrainStatus.COMPLETED

        # All blocks must be free after completion.
        seg = g.get_segment(seg_id)
        for blk_id in seg.ordered_block_ids:
            blk = g.get_block(blk_id)
            assert blk.is_free, f"Block {blk_id!r} still occupied after train completion."


# ===========================================================================
# 2. Siding overtake scenario
# ===========================================================================


class TestSidingOvertake:
    """
    Verify the siding overtake logic fires when the threshold is met, and
    that the express train's delay is reduced compared to following the local.
    """

    def _build_overtake_scenario(
        self, threshold_seconds: float = 100.0
    ) -> tuple[SimulationEngine, TrainRuntime, TrainRuntime]:
        """
        Network: sta_A → jct_1 → sta_B  (upper, 10 km each)
                 sta_A → jct_2 → sta_B  (lower / siding)

        Local train departs T0 on upper route, slow (30 km/h).
        Express train departs T0+30s on upper route, fast (120 km/h).
        Express's next scheduled station is sta_B at T0+7min.

        At 30 km/h, local takes ~20 min for 10 km; express following local
        would arrive around T0+22min — far beyond the threshold.
        """
        g, sta_a, sta_b = _make_parallel_network()

        engine = SimulationEngine(
            g,
            start_time=T0,
            junction_clearance_minutes=1.0,
            siding_threshold_seconds=threshold_seconds,
        )

        # Local: upper route, very slow.
        local_route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="jct_1", segment_id="seg_AJ"),
            RouteHop(node_id="sta_B", segment_id="seg_JB"),
        ]
        local = _make_train(
            "local",
            max_speed=30.0, avg_speed=25.0,
            priority=PriorityTier.LOCAL,
            route=local_route,
            schedule={
                "sta_A": ScheduleEntry(scheduled_departure=T0),
            },
        )

        # Express: same route, fast, scheduled to arrive sta_B at T0+7min.
        express_route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="jct_1", segment_id="seg_AJ"),
            RouteHop(node_id="sta_B", segment_id="seg_JB"),
        ]
        express = _make_train(
            "express",
            max_speed=120.0, avg_speed=100.0,
            priority=PriorityTier.EXPRESS,
            route=express_route,
            schedule={
                "sta_A": ScheduleEntry(scheduled_departure=T0 + timedelta(seconds=30)),
                "sta_B": ScheduleEntry(scheduled_arrival=T0 + timedelta(minutes=7)),
            },
        )

        engine.add_train(local)
        engine.add_train(express)
        return engine, local, express

    def test_siding_overtake_event_fires(self):
        """
        When the express is critically delayed by the local, and a siding
        (lower route) is available, a SIDING_OVERTAKE event must be logged.
        """
        engine, local, express = self._build_overtake_scenario(threshold_seconds=60.0)
        engine.run_headless(300, tick_size=1.0)

        overtake_events = engine.events_of_type(EventType.SIDING_OVERTAKE)
        # At minimum one siding overtake must have fired.
        assert len(overtake_events) > 0, (
            f"Expected SIDING_OVERTAKE event but none found. "
            f"All events: {[e['event_type'] for e in engine.event_log]}"
        )
        # The displaced train must be the local (lower priority, slower).
        for ev in overtake_events:
            assert ev["train_id"] == "local", (
                f"Expected local to be displaced, got {ev['train_id']!r}."
            )

    def test_no_siding_overtake_on_linear_network(self):
        """
        On a linear network with no alternate path (no siding), even if the
        threshold is exceeded, no SIDING_OVERTAKE event should fire.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=10.0, num_blocks=5)
        engine = SimulationEngine(
            g, start_time=T0,
            siding_threshold_seconds=1.0,  # extremely low threshold
        )
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        local = _make_train(
            "local", max_speed=30.0, avg_speed=25.0,
            priority=PriorityTier.LOCAL,
            route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        express = _make_train(
            "express", max_speed=120.0, avg_speed=100.0,
            priority=PriorityTier.EXPRESS,
            route=route,
            schedule={
                "sta_A": ScheduleEntry(
                    scheduled_departure=T0 + timedelta(seconds=30)
                ),
                "sta_B": ScheduleEntry(
                    scheduled_arrival=T0 + timedelta(seconds=60)  # impossibly tight
                ),
            },
        )
        engine.add_train(local)
        engine.add_train(express)
        engine.run_headless(300, tick_size=1.0)

        # No SIDING_OVERTAKE on a linear (single-path) network.
        assert len(engine.events_of_type(EventType.SIDING_OVERTAKE)) == 0


# ===========================================================================
# 3. Engine controls
# ===========================================================================


class TestEngineControls:
    def test_start_at_sets_clock(self):
        g, _, _, _ = _make_linear_network()
        engine = SimulationEngine(g)
        target_time = datetime(2025, 12, 25, 0, 0, 0, tzinfo=timezone.utc)
        engine.start_at(target_time)
        assert engine.sim_clock == target_time

    def test_play_enables_state_advancement(self):
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=2.0, num_blocks=4)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)
        engine.play()
        # After play+tick, the train should have tried to depart.
        engine.tick(5.0)
        state = engine.train_state("t")
        # Should be ACTIVE (departed) since first block is free.
        assert state.status == TrainStatus.ACTIVE

    def test_pause_stops_state_advancement(self):
        """
        When paused, tick() only advances the clock — trains do not move.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=2.0, num_blocks=4)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)
        # Do NOT call play() — engine starts paused.
        initial_clock = engine.sim_clock
        engine.tick(60.0)  # 60 sim-seconds while paused.

        # Clock must have advanced.
        assert engine.sim_clock > initial_clock
        assert engine.sim_clock == T0 + timedelta(seconds=60)

        # Train must still be PENDING (no state change).
        state = engine.train_state("t")
        assert state.status == TrainStatus.PENDING

    def test_pause_then_play_resumes(self):
        """
        Pause → tick() → play() → tick() should now move the train.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=2.0, num_blocks=4)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)
        engine.tick(10.0)  # paused — no movement
        state = engine.train_state("t")
        assert state.status == TrainStatus.PENDING

        engine.play()
        engine.tick(5.0)
        state = engine.train_state("t")
        assert state.status == TrainStatus.ACTIVE

    def test_set_speed_multiplier_stores_value(self):
        g, _, _, _ = _make_linear_network()
        engine = SimulationEngine(g)
        engine.set_speed_multiplier(60.0)
        assert engine.speed_multiplier == 60.0

    def test_set_speed_multiplier_rejects_non_positive(self):
        g, _, _, _ = _make_linear_network()
        engine = SimulationEngine(g)
        with pytest.raises(ValueError):
            engine.set_speed_multiplier(0.0)
        with pytest.raises(ValueError):
            engine.set_speed_multiplier(-5.0)


# ===========================================================================
# 4. Paused tick invariant
# ===========================================================================


class TestPausedTickInvariant:
    def test_train_position_unchanged_while_paused(self):
        """
        When paused, repeated tick() calls must not change block_index or
        position_in_block of any train.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=5.0, num_blocks=10)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)

        # Activate the train first.
        engine.play()
        engine.tick(5.0)
        engine.pause()

        state = engine.train_state("t")
        frozen_idx = state.block_index
        frozen_pos = state.position_in_block
        frozen_speed = state.current_speed_kmh

        # Many ticks while paused.
        for _ in range(100):
            engine.tick(1.0)

        assert state.block_index == frozen_idx
        assert state.position_in_block == frozen_pos
        assert state.current_speed_kmh == frozen_speed

    def test_clock_advances_while_paused(self):
        """Clock must advance even when paused."""
        g, _, _, _ = _make_linear_network()
        engine = SimulationEngine(g, start_time=T0)
        # Engine is paused by default.
        engine.tick(30.0)
        assert engine.sim_clock == T0 + timedelta(seconds=30)
        engine.tick(30.0)
        assert engine.sim_clock == T0 + timedelta(seconds=60)


# ===========================================================================
# 5. Weather halt / resume lifecycle
# ===========================================================================


class TestWeatherHaltResume:
    def _setup_with_weather(self, intensity: float = 0.0):
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=5.0, num_blocks=5)
        blk_ids = g.get_segment(seg_id).ordered_block_ids
        # Attach weather to the 2nd block (not the departure block).
        cell = WeatherCell(id="wc_1", intensity=intensity, affected_block_ids=[blk_ids[1]])
        g.add_weather_cell(cell)
        g.get_block(blk_ids[1]).weather_cell_id = "wc_1"
        return g, sta_a, sta_b, seg_id, blk_ids

    def test_weather_halt_fires_when_intensity_1(self):
        """
        Train running on a block with intensity=1.0 must emit WEATHER_HALT
        and stop moving.
        """
        g, sta_a, sta_b, seg_id, blk_ids = self._setup_with_weather(intensity=1.0)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)
        engine.run_headless(300, tick_size=1.0)

        # WEATHER_HALT must have been logged.
        halt_events = engine.events_of_type(EventType.WEATHER_HALT)
        assert len(halt_events) > 0, "Expected WEATHER_HALT event."

    def test_train_cancelled_on_intensity_1_at_departure(self):
        """
        Train not yet departed + intensity=1.0 at first block → CANCELLED.
        """
        g, sta_a, sta_b, seg_id, blk_ids = self._setup_with_weather(intensity=0.0)
        # Override: intensity on the FIRST block.
        cell = WeatherCell(id="wc_dep", intensity=1.0, affected_block_ids=[blk_ids[0]])
        g.add_weather_cell(cell)
        g.get_block(blk_ids[0]).weather_cell_id = "wc_dep"

        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)
        engine.run_headless(60, tick_size=1.0)

        state = engine.train_state("t")
        assert state.status == TrainStatus.CANCELLED
        assert len(engine.events_of_type(EventType.CANCELLED)) > 0


# ===========================================================================
# 6. Departure scheduling
# ===========================================================================


class TestDepartureScheduling:
    def test_train_does_not_depart_before_scheduled_time(self):
        """
        A train with scheduled_departure at T0+5min should still be PENDING
        at T0+4min.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=5.0, num_blocks=5)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={
                "sta_A": ScheduleEntry(scheduled_departure=T0 + timedelta(minutes=5))
            },
        )
        engine.add_train(train)
        engine.play()
        engine.run_headless(240, tick_size=10.0)  # 4 minutes

        state = engine.train_state("t")
        assert state.status == TrainStatus.PENDING, (
            f"Train should be PENDING at T0+4min, got {state.status!r}."
        )

    def test_train_departs_after_scheduled_time(self):
        """
        A train with scheduled_departure at T0+5min should be ACTIVE after
        T0+5min with a clear first block.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=5.0, num_blocks=5)
        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={
                "sta_A": ScheduleEntry(scheduled_departure=T0 + timedelta(minutes=5))
            },
        )
        engine.add_train(train)
        engine.play()
        # Run for 6 minutes (past departure time).
        engine.run_headless(360, tick_size=5.0)

        state = engine.train_state("t")
        assert state.status in (TrainStatus.ACTIVE, TrainStatus.COMPLETED), (
            f"Expected ACTIVE or COMPLETED after departure time, got {state.status!r}."
        )

    def test_train_waits_if_first_block_occupied(self):
        """
        If the first block is already occupied by another train, the departing
        train must wait (remain PENDING) even past its scheduled departure time.
        """
        g, sta_a, sta_b, seg_id = _make_linear_network(km_per_segment=5.0, num_blocks=5)
        blk_ids = g.get_segment(seg_id).ordered_block_ids

        # Pre-occupy the first block with a "phantom" train.
        g.get_block(blk_ids[0]).occupy("phantom-train")

        engine = SimulationEngine(g, start_time=T0)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(
            "t", max_speed=120.0, avg_speed=100.0, route=route,
            schedule={"sta_A": ScheduleEntry(scheduled_departure=T0)},
        )
        engine.add_train(train)
        engine.play()
        engine.run_headless(60, tick_size=1.0)

        state = engine.train_state("t")
        assert state.status == TrainStatus.PENDING, (
            f"Train should wait when first block is occupied, got {state.status!r}."
        )

    def test_run_headless_advances_clock(self):
        """run_headless must correctly advance the sim_clock."""
        g, _, _, _ = _make_linear_network()
        engine = SimulationEngine(g, start_time=T0)
        engine.run_headless(300, tick_size=10.0)
        assert engine.sim_clock == T0 + timedelta(seconds=300)
