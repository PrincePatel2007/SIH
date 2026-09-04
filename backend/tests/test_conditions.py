"""
test_conditions.py — Pytest tests for app.conditions.

Coverage
--------
1. get_speed_multiplier
   - Correct multiplier at exact tier boundaries (0.85, 0.60, 0.40, 0.20, 0.0)
   - Correct multiplier at intensity == 1.0 (0.0)
   - Midpoints between boundaries
   - Out-of-range intensity raises ValueError

2. apply_weather_to_train — intensity == 1.0 branch
   - Train NOT yet departed (current_block_id is None) → CANCELLED, speed 0.0
   - Train already en-route (current_block_id set) → HALTED, speed 0.0, NOT CANCELLED
   - Once intensity drops, departed train gets normal speed again (NORMAL outcome)

3. get_effective_speed — restriction stacking with weather
   - Restriction below weather-reduced speed → restriction wins
   - Restriction above weather-reduced speed → weather wins
   - No restriction → weather multiplier alone
   - Weather multiplier == 0 → always 0 regardless of restriction

4. MaintenanceWindow
   - Blocks pathfinding through the affected block/segment
   - Mid-route train gets a valid alternate path when one exists
   - Mid-route train raises NoAlternatePathError when no alternate exists

5. SpeedRestriction active/inactive time gating
   - is_active returns correct values inside and outside the window
   - active_restriction_for_block returns most restrictive when multiple active

6. ConditionsEngine integration
   - effective_speed_for stacks weather + restriction correctly
   - apply_weather records CANCELLED in engine for not-departed trains
   - apply_weather does NOT mark CANCELLED for already-departed trains

All numbers are hand-verifiable.
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
from app.conditions import (
    ConditionsEngine,
    MaintenanceWindow,
    NoAlternatePathError,
    SpeedRestriction,
    WeatherTrainOutcome,
    apply_weather_to_train,
    find_alternate_path,
    get_effective_speed,
    get_speed_multiplier,
)


# ===========================================================================
# Helpers
# ===========================================================================

T0 = datetime(2025, 6, 1, 8, 0, 0, tzinfo=timezone.utc)


def dt(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _make_train(
    train_id: str = "t-1",
    *,
    max_speed: float = 120.0,
    avg_speed: float = 100.0,
    route: Optional[list[RouteHop]] = None,
    current_block_id: Optional[str] = None,
) -> TrainRuntime:
    model = Train(
        id=train_id,
        name=f"Train-{train_id}",
        color="#aabbcc",
        priority=PriorityTier.ORDINARY,
        num_carriages=4,
        max_speed=max_speed,
        avg_speed=avg_speed,
        route=route or [],
        schedule={},
        driver_duty_status=DriverDutyStatus.NORMAL,
        current_block_id=current_block_id,
    )
    return TrainRuntime(model)


def _make_simple_network(
    km: float = 100.0,
    weather_intensity: Optional[float] = None,
) -> tuple[NetworkGraph, str, str, str]:
    """
    sta_A --[seg_AB, 5 blocks]--> sta_B
    Returns (graph, sta_A_id, sta_B_id, first_block_id).
    Attaches a WeatherCell to the first block if weather_intensity is given.
    """
    g = NetworkGraph()
    g.add_station("Alpha", station_id="sta_A")
    g.add_station("Beta", station_id="sta_B")
    g.add_track(
        "sta_A", "sta_B",
        length_km=km, num_blocks=5, segment_id="seg_AB",
        geometry=[Point(x=0, y=0), Point(x=km * 100, y=0)],
    )
    first_block_id = g.get_segment("seg_AB").ordered_block_ids[0]
    if weather_intensity is not None:
        cell = WeatherCell(id="wc_1", intensity=weather_intensity, affected_block_ids=[first_block_id])
        g.add_weather_cell(cell)
        g.get_block(first_block_id).weather_cell_id = "wc_1"
    return g, "sta_A", "sta_B", first_block_id


def _make_diamond_network() -> tuple[NetworkGraph, str, str, str, str]:
    """
    Diamond topology with an alternate path:
        sta_A → jct_1 → sta_B  (seg_AJ, seg_JB)   [main route, upper]
        sta_A → jct_2 → sta_B  (seg_AK, seg_KB)   [alternate, lower]
        sta_A → jct_1 and sta_A → jct_2 share sta_A.

    Returns (graph, "sta_A", "sta_B", "jct_1", "jct_2").
    """
    g = NetworkGraph()
    g.add_station("Alpha", station_id="sta_A")
    g.add_junction(junction_id="jct_1")
    g.add_junction(junction_id="jct_2")
    g.add_station("Beta", station_id="sta_B")

    for s, e, sid in [
        ("sta_A", "jct_1", "seg_AJ"),
        ("jct_1", "sta_B", "seg_JB"),
        ("sta_A", "jct_2", "seg_AK"),
        ("jct_2", "sta_B", "seg_KB"),
    ]:
        g.add_track(s, e, length_km=50.0, num_blocks=3, segment_id=sid,
                    geometry=[Point(x=0, y=0), Point(x=5000, y=0)])
    return g, "sta_A", "sta_B", "jct_1", "jct_2"


# ===========================================================================
# 1. get_speed_multiplier — boundary values
# ===========================================================================


class TestGetSpeedMultiplier:
    def test_intensity_1_0_returns_0(self):
        assert get_speed_multiplier(1.0) == 0.0

    def test_intensity_0_85_boundary(self):
        """Exact 0.85 must return 0.2."""
        assert get_speed_multiplier(0.85) == pytest.approx(0.2)

    def test_intensity_just_below_085(self):
        """0.84999 is in the [0.60, 0.85) tier → 0.5."""
        assert get_speed_multiplier(0.849) == pytest.approx(0.5)

    def test_intensity_0_60_boundary(self):
        assert get_speed_multiplier(0.60) == pytest.approx(0.5)

    def test_intensity_just_below_060(self):
        """0.599 is in [0.40, 0.60) → 0.7."""
        assert get_speed_multiplier(0.599) == pytest.approx(0.7)

    def test_intensity_0_40_boundary(self):
        assert get_speed_multiplier(0.40) == pytest.approx(0.7)

    def test_intensity_just_below_040(self):
        assert get_speed_multiplier(0.399) == pytest.approx(0.85)

    def test_intensity_0_20_boundary(self):
        assert get_speed_multiplier(0.20) == pytest.approx(0.85)

    def test_intensity_just_below_020(self):
        assert get_speed_multiplier(0.199) == pytest.approx(1.0)

    def test_intensity_0_0_returns_1(self):
        """Clear sky → no speed effect."""
        assert get_speed_multiplier(0.0) == pytest.approx(1.0)

    def test_intensity_midpoint_severe(self):
        """0.90 is in [0.85, 1.0) → 0.2."""
        assert get_speed_multiplier(0.90) == pytest.approx(0.2)

    def test_intensity_midpoint_heavy(self):
        """0.70 is in [0.60, 0.85) → 0.5."""
        assert get_speed_multiplier(0.70) == pytest.approx(0.5)

    def test_intensity_midpoint_clear(self):
        """0.10 is in [0.0, 0.20) → 1.0."""
        assert get_speed_multiplier(0.10) == pytest.approx(1.0)

    def test_intensity_above_1_raises(self):
        with pytest.raises(ValueError, match="1.0"):
            get_speed_multiplier(1.001)

    def test_intensity_below_0_raises(self):
        with pytest.raises(ValueError, match="0.0"):
            get_speed_multiplier(-0.01)


# ===========================================================================
# 2. apply_weather_to_train — intensity==1.0 branching
# ===========================================================================


class TestApplyWeatherToTrain:
    def test_not_departed_intensity_1_is_cancelled(self):
        """
        Train with current_block_id=None (not yet departed) + intensity 1.0
        → CANCELLED, speed == 0.0, and ConditionsEngine records the cancellation.
        """
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=1.0)
        train = _make_train(current_block_id=None)  # not departed
        blk = g.get_block(blk_id)

        speed, outcome = apply_weather_to_train(train, blk, g)
        assert outcome == WeatherTrainOutcome.CANCELLED
        assert speed == 0.0

    def test_not_departed_intensity_1_never_gets_block(self):
        """
        Cancellation means the train must NOT be allowed a current_block_id.
        Test that the engine CAN enforce this given the outcome flag.
        """
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=1.0)
        train = _make_train(current_block_id=None)
        blk = g.get_block(blk_id)

        _, outcome = apply_weather_to_train(train, blk, g)
        # If outcome is CANCELLED, engine must not call update_position.
        # Verify the train still has no block after the call (pure function).
        assert train.current_block_id is None
        assert outcome == WeatherTrainOutcome.CANCELLED

    def test_departed_intensity_1_is_halted_not_cancelled(self):
        """
        Train with current_block_id set (already en-route) + intensity 1.0
        → HALTED, NOT CANCELLED.  It must resume when intensity drops.
        """
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=1.0)
        train = _make_train(current_block_id=blk_id)   # already en-route
        blk = g.get_block(blk_id)

        speed, outcome = apply_weather_to_train(train, blk, g)
        assert outcome == WeatherTrainOutcome.HALTED
        assert speed == 0.0

    def test_halted_train_resumes_when_intensity_drops(self):
        """
        After intensity drops below 1.0, apply_weather_to_train must return
        NORMAL with a non-zero speed for a previously halted (en-route) train.

        Intensity 0.7 is in the [0.60, 0.85) tier → multiplier = 0.5
        → effective speed = 120 * 0.5 = 60 km/h.
        """
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=1.0)
        train = _make_train(max_speed=120.0, current_block_id=blk_id)
        blk = g.get_block(blk_id)

        # Verify halted first
        speed, outcome = apply_weather_to_train(train, blk, g)
        assert outcome == WeatherTrainOutcome.HALTED

        # Drop intensity to 0.7 → [0.60, 0.85) tier → multiplier 0.5 → 60 km/h
        cell = g.get_weather_cell("wc_1")
        cell.intensity = 0.7
        speed2, outcome2 = apply_weather_to_train(train, blk, g)
        assert outcome2 == WeatherTrainOutcome.NORMAL
        assert speed2 == pytest.approx(120.0 * 0.5)  # = 60.0

    def test_normal_weather_returns_full_speed(self):
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=0.0)
        train = _make_train(max_speed=100.0, current_block_id=blk_id)
        blk = g.get_block(blk_id)
        speed, outcome = apply_weather_to_train(train, blk, g)
        assert outcome == WeatherTrainOutcome.NORMAL
        assert speed == pytest.approx(100.0)

    def test_no_weather_cell_returns_full_speed(self):
        """Block with no weather cell → full max_speed, NORMAL."""
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=None)
        train = _make_train(max_speed=80.0, current_block_id=blk_id)
        blk = g.get_block(blk_id)
        speed, outcome = apply_weather_to_train(train, blk, g)
        assert outcome == WeatherTrainOutcome.NORMAL
        assert speed == pytest.approx(80.0)


# ===========================================================================
# 3. get_effective_speed — restriction stacking
# ===========================================================================


class TestGetEffectiveSpeed:
    def test_no_restriction_weather_only(self):
        """Weather at 0.5 multiplier, no restriction → max_speed * 0.5."""
        speed = get_effective_speed(120.0, 0.5, None)
        assert speed == pytest.approx(60.0)

    def test_restriction_below_weather_speed_wins(self):
        """
        max_speed=120, multiplier=0.7 → weather_speed=84.
        Restriction=60 < 84 → restriction caps it at 60.
        """
        speed = get_effective_speed(120.0, 0.7, 60.0)
        assert speed == pytest.approx(60.0)

    def test_restriction_above_weather_speed_ignored(self):
        """
        max_speed=120, multiplier=0.4 → weather_speed=48.
        Restriction=80 > 48 → weather wins, speed stays at 48.
        """
        speed = get_effective_speed(120.0, 0.4, 80.0)
        assert speed == pytest.approx(48.0)

    def test_restriction_equals_weather_speed(self):
        """Exactly equal → either value; both give the same result."""
        speed = get_effective_speed(100.0, 0.6, 60.0)
        assert speed == pytest.approx(60.0)

    def test_weather_multiplier_zero_always_zero(self):
        """Intensity==1.0 → multiplier 0 → speed 0 regardless of restriction."""
        speed = get_effective_speed(160.0, 0.0, 999.0)
        assert speed == 0.0

    def test_train_higher_max_speed_than_restriction(self):
        """
        A fast train (200 km/h) with a 40 km/h restriction and clear weather
        must be capped at 40 km/h.
        """
        speed = get_effective_speed(200.0, 1.0, 40.0)
        assert speed == pytest.approx(40.0)


# ===========================================================================
# 4. MaintenanceWindow — pathfinding and re-routing
# ===========================================================================


class TestMaintenanceWindow:
    def test_blocks_pathfinding_through_affected_segment(self):
        """
        On a linear A→B network (only one path), blocking the only segment
        must return no path (find_path_excluding_segments returns None).
        """
        g, sta_a, sta_b, _ = _make_simple_network()
        seg = g.get_segment("seg_AB")
        # Exclude the only segment.
        result = g.find_path_excluding_segments("sta_A", "sta_B", {"seg_AB"})
        assert result is None

    def test_alternate_path_found_in_diamond(self):
        """
        Diamond topology: block upper path → lower path is the alternate.
        """
        g, sta_a, sta_b, jct_1, jct_2 = _make_diamond_network()
        # Block the upper-left segment (sta_A → jct_1).
        upper_seg_id = g.get_segment("seg_AJ").id
        result = g.find_path_excluding_segments("sta_A", "sta_B", {upper_seg_id})
        # Alternate must exist (through jct_2).
        assert result is not None
        assert "seg_AK" in result or "seg_KB" in result  # uses lower path

    def test_no_alternate_raises_error(self):
        """
        Linear network with only one path: find_alternate_path must raise
        NoAlternatePathError when the segment is blocked.
        """
        g, sta_a, sta_b, blk_id = _make_simple_network()
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(route=route, current_block_id=blk_id)
        # Block the only segment.
        blocked = {blk_id}
        with pytest.raises(NoAlternatePathError, match="sta_A"):
            find_alternate_path(train, "sta_A", g, blocked)

    def test_alternate_path_in_diamond_for_mid_route_train(self):
        """
        Diamond: train mid-route on upper path; block upper segment.
        find_alternate_path must return segments through the lower path.
        """
        g, sta_a, sta_b, jct_1, jct_2 = _make_diamond_network()
        # Block the seg_AJ segment's first block.
        upper_blk_id = g.get_segment("seg_AJ").ordered_block_ids[0]
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id=jct_1, segment_id="seg_AJ"),
            RouteHop(node_id="sta_B", segment_id="seg_JB"),
        ]
        train = _make_train(route=route, current_block_id=upper_blk_id)
        blocked = {upper_blk_id}
        alt = find_alternate_path(train, "sta_A", g, blocked)
        # Alt path must not use seg_AJ.
        assert "seg_AJ" not in alt
        # Must reach sta_B somehow.
        assert "seg_KB" in alt or "seg_AK" in alt

    def test_maintenance_window_is_active(self):
        mw = MaintenanceWindow(
            id="mw-1",
            block_ids=["blk_x"],
            start_time=T0,
            end_time=dt(60),
        )
        assert mw.is_active(dt(30)) is True
        assert mw.is_active(T0 - timedelta(minutes=1)) is False
        assert mw.is_active(dt(60)) is False  # exclusive end

    def test_maintenance_window_inactive_outside_window(self):
        mw = MaintenanceWindow(
            id="mw-2",
            block_ids=["blk_x"],
            start_time=dt(120),
            end_time=dt(180),
        )
        assert mw.is_active(T0) is False
        assert mw.is_active(dt(119)) is False
        assert mw.is_active(dt(150)) is True

    def test_conditions_engine_active_blocked_block_ids(self):
        engine = ConditionsEngine()
        mw1 = MaintenanceWindow("mw-1", ["blk_a", "blk_b"], T0, dt(60))
        mw2 = MaintenanceWindow("mw-2", ["blk_c"], dt(120), dt(180))  # not active at T0
        engine.add_maintenance_window(mw1)
        engine.add_maintenance_window(mw2)

        blocked = engine.active_blocked_block_ids(T0 + timedelta(minutes=30))
        assert "blk_a" in blocked
        assert "blk_b" in blocked
        assert "blk_c" not in blocked  # mw2 not yet active


# ===========================================================================
# 5. SpeedRestriction — time gating and stacking
# ===========================================================================


class TestSpeedRestriction:
    def test_is_active_within_window(self):
        sr = SpeedRestriction("sr-1", "blk_x", 40.0, T0, dt(60))
        assert sr.is_active(T0) is True
        assert sr.is_active(dt(30)) is True
        assert sr.is_active(dt(59)) is True

    def test_is_inactive_outside_window(self):
        sr = SpeedRestriction("sr-1", "blk_x", 40.0, T0, dt(60))
        assert sr.is_active(T0 - timedelta(seconds=1)) is False
        assert sr.is_active(dt(60)) is False  # end is exclusive

    def test_restriction_caps_speed_above_train_max(self):
        """
        Train max 160 km/h, weather clear (1.0x), restriction = 80 km/h.
        Effective speed must be 80.
        """
        speed = get_effective_speed(160.0, 1.0, 80.0)
        assert speed == pytest.approx(80.0)

    def test_restriction_does_not_raise_speed(self):
        """
        A restriction of 200 km/h on a train limited to 100 km/h by weather
        must not raise the effective speed above weather limit.
        """
        speed = get_effective_speed(120.0, 0.5, 200.0)  # weather → 60 km/h
        assert speed == pytest.approx(60.0)

    def test_most_restrictive_active_restriction_returned(self):
        """
        When two restrictions are active for the same block, the one with the
        lower cap (more restrictive) must be returned.
        """
        engine = ConditionsEngine()
        sr_low = SpeedRestriction("sr-low", "blk_x", 30.0, T0, dt(120))
        sr_high = SpeedRestriction("sr-high", "blk_x", 80.0, T0, dt(120))
        engine.add_speed_restriction(sr_low)
        engine.add_speed_restriction(sr_high)

        active = engine.active_restriction_for_block("blk_x", dt(60))
        assert active is not None
        assert active.max_speed_kmh == pytest.approx(30.0)

    def test_expired_restriction_not_returned(self):
        engine = ConditionsEngine()
        sr = SpeedRestriction("sr-1", "blk_x", 40.0, T0, dt(60))
        engine.add_speed_restriction(sr)

        # Before window: not active
        assert engine.active_restriction_for_block("blk_x", T0 - timedelta(minutes=1)) is None
        # During window: active
        assert engine.active_restriction_for_block("blk_x", dt(30)) is not None
        # After window: not active
        assert engine.active_restriction_for_block("blk_x", dt(60)) is None


# ===========================================================================
# 6. ConditionsEngine integration
# ===========================================================================


class TestConditionsEngine:
    def test_effective_speed_for_stacks_weather_and_restriction(self):
        """
        Block with weather intensity 0.5 (multiplier=0.5) and a speed
        restriction of 40 km/h.
        Train max_speed=120.
        Weather alone → 60 km/h; restriction caps to 40 km/h.
        """
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=0.5)
        train = _make_train(max_speed=120.0)
        engine = ConditionsEngine()
        sr = SpeedRestriction("sr-1", blk_id, 40.0, T0, dt(120))
        engine.add_speed_restriction(sr)

        blk = g.get_block(blk_id)
        speed = engine.effective_speed_for(train, blk, g, dt(60))
        assert speed == pytest.approx(40.0)

    def test_effective_speed_no_conditions_is_max_speed(self):
        g, sta_a, sta_b, blk_id = _make_simple_network()
        train = _make_train(max_speed=160.0)
        engine = ConditionsEngine()
        blk = g.get_block(blk_id)
        speed = engine.effective_speed_for(train, blk, g, dt(0))
        assert speed == pytest.approx(160.0)

    def test_apply_weather_marks_cancelled_in_engine_for_not_departed(self):
        """
        ConditionsEngine.apply_weather on intensity==1.0 + not-departed train
        must register the train as CANCELLED in the engine.
        """
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=1.0)
        train = _make_train("t-cancel", current_block_id=None)
        engine = ConditionsEngine()
        blk = g.get_block(blk_id)

        outcome = engine.apply_weather(train, blk, g)
        assert outcome == WeatherTrainOutcome.CANCELLED
        assert engine.is_cancelled("t-cancel") is True

    def test_apply_weather_does_not_cancel_departed_train(self):
        """
        ConditionsEngine.apply_weather on intensity==1.0 + departed train
        must return HALTED but NOT record the train as cancelled.
        """
        g, sta_a, sta_b, blk_id = _make_simple_network(weather_intensity=1.0)
        train = _make_train("t-halted", current_block_id=blk_id)
        engine = ConditionsEngine()
        blk = g.get_block(blk_id)

        outcome = engine.apply_weather(train, blk, g)
        assert outcome == WeatherTrainOutcome.HALTED
        assert engine.is_cancelled("t-halted") is False

    def test_reinstate_train_removes_cancellation(self):
        engine = ConditionsEngine()
        engine.cancel_train("t-x")
        assert engine.is_cancelled("t-x") is True
        engine.reinstate_train("t-x")
        assert engine.is_cancelled("t-x") is False

    def test_find_alternate_path_for_delegates_correctly(self):
        """
        ConditionsEngine.find_alternate_path_for must return an alternate path
        in a diamond network when the upper path block is under maintenance.
        """
        g, sta_a, sta_b, jct_1, jct_2 = _make_diamond_network()
        upper_blk_id = g.get_segment("seg_AJ").ordered_block_ids[0]

        engine = ConditionsEngine()
        mw = MaintenanceWindow("mw-1", [upper_blk_id], T0, dt(120))
        engine.add_maintenance_window(mw)

        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id=jct_1, segment_id="seg_AJ"),
            RouteHop(node_id="sta_B", segment_id="seg_JB"),
        ]
        train = _make_train(route=route, current_block_id=upper_blk_id)
        alt = engine.find_alternate_path_for(train, "sta_A", g, dt(60))
        assert alt is not None
        assert "seg_AJ" not in alt  # blocked segment excluded

    def test_find_alternate_path_raises_when_no_alternate(self):
        """Linear (no alternate) + maintenance on the only segment → error."""
        g, sta_a, sta_b, blk_id = _make_simple_network()
        engine = ConditionsEngine()
        mw = MaintenanceWindow("mw-1", [blk_id], T0, dt(120))
        engine.add_maintenance_window(mw)

        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(route=route, current_block_id=blk_id)
        with pytest.raises(NoAlternatePathError):
            engine.find_alternate_path_for(train, "sta_A", g, dt(60))

    def test_recompute_halted_train_clears_future_arrivals(self):
        """
        When effective_speed == 0 (halted), recompute_expected_arrival_after_speed_change
        must set future expected_arrivals to None.
        """
        from app.arbitration import ArbitrationRegistry
        g, sta_a, sta_b, _ = _make_simple_network()
        registry = ArbitrationRegistry.from_network(g)

        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AB"),
        ]
        train = _make_train(route=route)
        # Pre-set an expected_arrival for sta_B.
        train.update_expected_arrival("sta_B", T0 + timedelta(hours=1))
        assert train._model.schedule["sta_B"].expected_arrival is not None

        engine = ConditionsEngine()
        engine.recompute_expected_arrival_after_speed_change(
            train=train,
            current_node_id="sta_A",
            granted_pass_time=T0,
            network=g,
            registry=registry,
            effective_speed_kmh=0.0,  # halted
        )
        # Future arrivals must be cleared.
        assert train._model.schedule["sta_B"].expected_arrival is None
