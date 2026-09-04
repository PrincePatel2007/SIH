"""
test_train.py — Pytest tests for app.train.

Test coverage
-------------
1. TrainRuntime.is_max_priority_override
   - True for over_duty driver (regardless of journey length or priority tier)
   - True for a journey >= 12 hours at avg_speed (regardless of duty status)
   - False for normal duty + short journey
   - Boundary: exactly 12.0 h is True; 11.999... h is False
   - The property uses avg_speed + route distance, not a magic constant

2. build_schedule
   - Produces correct scheduled_arrival times at every hop
   - Window at every hop is >= minimum travel time at max_speed
   - Padding is reflected in the schedule (window > bare travel time)
   - Origin has scheduled_departure set, scheduled_arrival=None
   - expected_arrival and actual_arrival are always None on the output
   - Raises ScheduleError (with a hop-identifying message) when avg_speed >
     max_speed (making the infeasibility check fire)
   - Raises ValueError for bad inputs (negative padding, zero avg_speed, empty route)

3. validate_schedule
   - Returns empty list for a valid schedule produced by build_schedule
   - Returns one ScheduleViolation per manually-corrupted hop
   - Violation carries correct hop_index, node ids, distance, and min/actual times
   - Multiple corrupt hops → multiple violations, one per hop
   - Does NOT raise; it only accumulates and returns

4. Physics helpers
   - segment_distance_km returns sum of block lengths
   - route_total_distance_km sums correctly across multiple segments
   - travel_time_hours respects the distance/speed formula
   - travel_time_hours raises ValueError for non-positive speed
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
)
from app.network import NetworkGraph
from app.train import (
    ScheduleError,
    ScheduleViolation,
    TrainRuntime,
    build_schedule,
    route_total_distance_km,
    segment_distance_km,
    travel_time_hours,
    validate_schedule,
)


# ===========================================================================
# Helpers / fixtures
# ===========================================================================

EPOCH = datetime(2025, 1, 1, 8, 0, 0, tzinfo=timezone.utc)


def _make_train(
    *,
    train_id: str = "t-1",
    max_speed: float = 160.0,
    avg_speed: float = 120.0,
    priority: PriorityTier = PriorityTier.ORDINARY,
    driver_duty_status: DriverDutyStatus = DriverDutyStatus.NORMAL,
    route: Optional[list[RouteHop]] = None,
    schedule: Optional[dict[str, ScheduleEntry]] = None,
) -> TrainRuntime:
    model = Train(
        id=train_id,
        name="Test Train",
        color="#aabbcc",
        priority=priority,
        num_carriages=8,
        max_speed=max_speed,
        avg_speed=avg_speed,
        route=route or [],
        schedule=schedule or {},
        driver_duty_status=driver_duty_status,
    )
    return TrainRuntime(model)


def _make_linear_network(
    num_stations: int = 4,
    km_per_segment: float = 100.0,
) -> tuple[NetworkGraph, list[str], list[str]]:
    """
    Build a straight-line network:  sta_0 --seg_01-- sta_1 --seg_12-- sta_2 ...

    Returns (graph, station_ids, segment_ids).
    segment_ids[i] is the segment between station_ids[i] and station_ids[i+1].
    """
    g = NetworkGraph()
    station_ids: list[str] = []
    for i in range(num_stations):
        s = g.add_station(f"Station {i}", station_id=f"sta_{i}")
        station_ids.append(s.id)

    segment_ids: list[str] = []
    for i in range(num_stations - 1):
        seg_id = f"seg_{i}{i+1}"
        g.add_track(
            station_ids[i],
            station_ids[i + 1],
            length_km=km_per_segment,
            num_blocks=10,
            segment_id=seg_id,
            geometry=[Point(x=0, y=0), Point(x=km_per_segment * 100, y=0)],
        )
        segment_ids.append(seg_id)

    return g, station_ids, segment_ids


def _make_route(station_ids: list[str], segment_ids: list[str]) -> list[RouteHop]:
    """Build RouteHops for a straight-line network."""
    hops = [RouteHop(node_id=station_ids[0], segment_id=None)]
    for i, seg_id in enumerate(segment_ids):
        hops.append(RouteHop(node_id=station_ids[i + 1], segment_id=seg_id))
    return hops


# ===========================================================================
# 1. is_max_priority_override
# ===========================================================================


class TestIsPriorityOverride:
    """
    Test the is_max_priority_override flag under all trigger conditions.
    """

    def test_over_duty_triggers_override_regardless_of_journey(self):
        """OVER_DUTY alone must trigger the override even for a zero-km route."""
        train = _make_train(driver_duty_status=DriverDutyStatus.OVER_DUTY)
        g, *_ = _make_linear_network(num_stations=2, km_per_segment=1.0)
        assert train.is_max_priority_override(g) is True

    def test_over_duty_triggers_override_even_with_low_priority(self):
        train = _make_train(
            driver_duty_status=DriverDutyStatus.OVER_DUTY,
            priority=PriorityTier.LOCAL,
        )
        g, *_ = _make_linear_network(num_stations=2, km_per_segment=1.0)
        assert train.is_max_priority_override(g) is True

    def test_normal_duty_short_journey_no_override(self):
        """Normal duty + short journey must NOT trigger the override."""
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=50.0)
        route = _make_route(stations, segments)
        # 50 km at 100 km/h avg → 0.5 h journey — well under 12 h.
        train = _make_train(avg_speed=100.0, max_speed=120.0, route=route)
        assert train.is_max_priority_override(g) is False

    def test_long_journey_triggers_override(self):
        """
        A journey >= 12 h must trigger the override.

        12 hours at 100 km/h avg = 1200 km.
        3 segments × 400 km each = 1200 km → exactly 12 h → override True.
        """
        g, stations, segments = _make_linear_network(num_stations=4, km_per_segment=400.0)
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=100.0, max_speed=160.0, route=route)
        hours = train.estimated_total_journey_time_hours(g)
        assert hours == pytest.approx(12.0)
        assert train.is_max_priority_override(g) is True

    def test_just_under_12h_no_override(self):
        """
        11.99... hours must NOT trigger the override.

        3 segments × 399 km at 100 km/h → 11.97 h.
        """
        g, stations, segments = _make_linear_network(num_stations=4, km_per_segment=399.0)
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=100.0, max_speed=160.0, route=route)
        hours = train.estimated_total_journey_time_hours(g)
        assert hours < 12.0
        assert train.is_max_priority_override(g) is False

    def test_boundary_exactly_12h(self):
        """Exactly 12.0 h → override True (>= includes equality)."""
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=1200.0)
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=100.0, max_speed=160.0, route=route)
        hours = train.estimated_total_journey_time_hours(g)
        assert hours == pytest.approx(12.0)
        assert train.is_max_priority_override(g) is True

    def test_override_uses_avg_speed_not_max_speed(self):
        """
        Journey time is computed using avg_speed (normal conditions baseline),
        not max_speed.  A train with a high max_speed but low avg_speed on a
        long route should still trigger the override.

        1200 km at avg 100 km/h = 12 h  → override True.
        Same 1200 km at max 500 km/h doesn't matter.
        """
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=1200.0)
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=100.0, max_speed=500.0, route=route)
        assert train.is_max_priority_override(g) is True

    def test_override_false_for_express_train_on_short_route(self):
        """
        An express train (priority=1) on a short route with normal duty
        must NOT auto-trigger the override — numeric priority is not the flag.
        """
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=50.0)
        route = _make_route(stations, segments)
        train = _make_train(
            avg_speed=100.0,
            max_speed=160.0,
            priority=PriorityTier.EXPRESS,
            route=route,
        )
        assert train.is_max_priority_override(g) is False


# ===========================================================================
# 2. build_schedule
# ===========================================================================


class TestBuildSchedule:
    def _build(
        self,
        km_per_segment: float = 100.0,
        num_segments: int = 3,
        avg_speed: float = 100.0,
        max_speed: float = 160.0,
        padding_min: float = 5.0,
        departure: Optional[datetime] = None,
    ) -> tuple[dict[str, ScheduleEntry], list[str], list[str], TrainRuntime]:
        g, stations, segments = _make_linear_network(
            num_stations=num_segments + 1,
            km_per_segment=km_per_segment,
        )
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=avg_speed, max_speed=max_speed, route=route)
        sched = build_schedule(
            train, route, g,
            avg_speed=avg_speed,
            target_padding_minutes=padding_min,
            departure_time=departure or EPOCH,
        )
        return sched, stations, segments, train

    def test_origin_has_departure_no_arrival(self):
        sched, stations, *_ = self._build()
        origin = sched[stations[0]]
        assert origin.scheduled_departure == EPOCH
        assert origin.scheduled_arrival is None

    def test_all_nodes_present_in_schedule(self):
        sched, stations, segments, _ = self._build(num_segments=3)
        for sid in stations:
            assert sid in sched, f"Station {sid!r} missing from schedule."

    def test_expected_and_actual_are_none(self):
        sched, stations, *_ = self._build()
        for node_id, entry in sched.items():
            assert entry.expected_arrival is None, f"expected_arrival set for {node_id!r}"
            assert entry.actual_arrival is None, f"actual_arrival set for {node_id!r}"

    def test_times_are_monotonically_increasing(self):
        sched, stations, *_ = self._build(num_segments=3)
        prev_time = EPOCH
        for sta_id in stations[1:]:
            arr = sched[sta_id].scheduled_arrival
            assert arr is not None
            assert arr > prev_time, f"Arrival at {sta_id!r} is not after previous."
            prev_time = arr

    def test_window_covers_max_speed_travel_time(self):
        """
        The scheduled window at every hop must be >= minimum travel time at max_speed.
        """
        km = 100.0
        avg = 100.0
        max_spd = 160.0
        padding_min = 5.0

        sched, stations, segments, train = self._build(
            km_per_segment=km,
            num_segments=3,
            avg_speed=avg,
            max_speed=max_spd,
            padding_min=padding_min,
        )

        min_travel_hours = km / max_spd

        for i, seg_id in enumerate(segments):
            prev_node = stations[i]
            curr_node = stations[i + 1]
            departure = sched[prev_node].scheduled_departure
            arrival = sched[curr_node].scheduled_arrival
            assert departure is not None
            assert arrival is not None
            window_hours = (arrival - departure).total_seconds() / 3600.0
            assert window_hours >= min_travel_hours, (
                f"Hop {i+1} ({prev_node}→{curr_node}): window {window_hours:.4f}h "
                f"< min travel {min_travel_hours:.4f}h"
            )

    def test_padding_is_reflected(self):
        """
        With 10-min padding and 100 km at 100 km/h avg (60 min baseline),
        the window should be 70 min.
        """
        padding_min = 10.0
        km = 100.0
        avg = 100.0
        sched, stations, *_ = self._build(
            km_per_segment=km, num_segments=1, avg_speed=avg,
            max_speed=160.0, padding_min=padding_min,
        )
        departure = sched[stations[0]].scheduled_departure
        arrival = sched[stations[1]].scheduled_arrival
        window_min = (arrival - departure).total_seconds() / 60.0
        baseline_min = (km / avg) * 60.0
        assert window_min == pytest.approx(baseline_min + padding_min, abs=0.001)

    def test_zero_padding_still_feasible(self):
        """Padding = 0 should produce the bare baseline schedule (still feasible)."""
        sched, stations, segments, train = self._build(
            km_per_segment=100.0, avg_speed=100.0, max_speed=160.0, padding_min=0.0
        )
        # With 0 padding, window = bare travel time at avg_speed.
        for i, seg_id in enumerate(segments):
            departure = sched[stations[i]].scheduled_departure
            arrival = sched[stations[i + 1]].scheduled_arrival
            window_h = (arrival - departure).total_seconds() / 3600.0
            assert window_h == pytest.approx(100.0 / 100.0, abs=1e-6)

    def test_raises_schedule_error_when_avg_speed_exceeds_max_speed(self):
        """
        avg_speed > max_speed means the baseline schedule is tighter than
        max_speed allows — must raise ScheduleError naming the hop.
        """
        g, stations, segments = _make_linear_network(num_stations=3, km_per_segment=100.0)
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=200.0, max_speed=100.0, route=route)

        with pytest.raises(ScheduleError, match="infeasible"):
            build_schedule(train, route, g, avg_speed=200.0, target_padding_minutes=0.0)

    def test_schedule_error_names_the_hop(self):
        """The ScheduleError message must contain the node id of the infeasible hop."""
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=100.0)
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=200.0, max_speed=100.0, route=route)

        with pytest.raises(ScheduleError) as exc_info:
            build_schedule(train, route, g, avg_speed=200.0, target_padding_minutes=0.0)

        # The error must name the destination node id.
        assert stations[1] in str(exc_info.value)

    def test_raises_value_error_for_zero_avg_speed(self):
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=100.0)
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=100.0, max_speed=160.0, route=route)
        with pytest.raises(ValueError, match="avg_speed"):
            build_schedule(train, route, g, avg_speed=0.0, target_padding_minutes=5.0)

    def test_raises_value_error_for_negative_padding(self):
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=100.0)
        route = _make_route(stations, segments)
        train = _make_train(avg_speed=100.0, max_speed=160.0, route=route)
        with pytest.raises(ValueError, match="padding"):
            build_schedule(train, route, g, avg_speed=100.0, target_padding_minutes=-1.0)

    def test_raises_value_error_for_empty_route(self):
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=100.0)
        train = _make_train(avg_speed=100.0, max_speed=160.0)
        with pytest.raises(ValueError, match="route"):
            build_schedule(train, [], g, avg_speed=100.0, target_padding_minutes=5.0)


# ===========================================================================
# 3. validate_schedule
# ===========================================================================


class TestValidateSchedule:
    def _train_with_schedule(
        self,
        km_per_segment: float = 100.0,
        num_segments: int = 3,
        avg_speed: float = 100.0,
        max_speed: float = 160.0,
        padding_min: float = 5.0,
    ) -> tuple[TrainRuntime, NetworkGraph]:
        """Build a TrainRuntime whose schedule is freshly produced by build_schedule."""
        g, stations, segments = _make_linear_network(
            num_stations=num_segments + 1,
            km_per_segment=km_per_segment,
        )
        route = _make_route(stations, segments)
        sched = build_schedule(
            _make_train(avg_speed=avg_speed, max_speed=max_speed, route=route),
            route, g,
            avg_speed=avg_speed,
            target_padding_minutes=padding_min,
            departure_time=EPOCH,
        )
        from app.models import Train as TrainModel
        model = TrainModel(
            id="t-validate",
            name="Validation Train",
            color="#123456",
            num_carriages=8,
            max_speed=max_speed,
            avg_speed=avg_speed,
            route=route,
            schedule=sched,
        )
        return TrainRuntime(model), g

    def test_valid_schedule_has_no_violations(self):
        train, g = self._train_with_schedule()
        violations = validate_schedule(train, g)
        assert violations == [], f"Unexpected violations: {violations}"

    def test_corrupted_single_hop_is_detected(self):
        """
        Manually shorten one hop's window below the max_speed minimum and
        confirm validate_schedule catches it.
        """
        train, g = self._train_with_schedule(
            km_per_segment=100.0,
            max_speed=160.0,
            padding_min=5.0,
        )
        route = train.route
        sched = train.schedule

        # Corrupt hop 1: set arrival to just 1 second after departure.
        # min travel time = 100 km / 160 km/h = 37.5 min.  We set 1 second — impossibly short.
        prev_node = route[0].node_id
        curr_node = route[1].node_id
        departure = sched[prev_node].scheduled_departure
        sched[curr_node] = ScheduleEntry(
            scheduled_arrival=departure + timedelta(seconds=1),
            scheduled_departure=departure + timedelta(seconds=1),
        )
        # Write back by rebuilding the model.
        from app.models import Train as TrainModel
        corrupted_model = TrainModel(
            id=train.id,
            name=train.name,
            color="#123456",
            num_carriages=8,
            max_speed=train.max_speed,
            avg_speed=train.avg_speed,
            route=route,
            schedule=sched,
        )
        corrupted_train = TrainRuntime(corrupted_model)

        violations = validate_schedule(corrupted_train, g)
        assert len(violations) == 1
        v = violations[0]
        assert v.hop_index == 1
        assert v.from_node_id == prev_node
        assert v.to_node_id == curr_node

    def test_violation_has_correct_fields(self):
        """Spot-check ScheduleViolation fields for correctness."""
        km = 100.0
        max_spd = 160.0
        train, g = self._train_with_schedule(km_per_segment=km, max_speed=max_spd)
        route = train.route
        sched = train.schedule

        # Corrupt hop 1 to 1-minute window.
        prev_node = route[0].node_id
        curr_node = route[1].node_id
        departure = sched[prev_node].scheduled_departure
        sched[curr_node] = ScheduleEntry(
            scheduled_arrival=departure + timedelta(minutes=1),
            scheduled_departure=departure + timedelta(minutes=1),
        )

        from app.models import Train as TrainModel
        m = TrainModel(
            id="t-v", name="V", color="#000", num_carriages=1,
            max_speed=max_spd, avg_speed=100.0, route=route, schedule=sched,
        )
        v_train = TrainRuntime(m)
        violations = validate_schedule(v_train, g)
        assert violations
        v = violations[0]

        expected_min_hours = km / max_spd  # 100/160 = 0.625 h
        assert v.distance_km == pytest.approx(km)
        assert v.max_speed_kmh == pytest.approx(max_spd)
        assert v.min_travel_hours == pytest.approx(expected_min_hours)
        assert v.scheduled_window_hours == pytest.approx(1.0 / 60.0, abs=1e-6)
        assert "min" in v.detail.lower() or "hop" in v.detail.lower()

    def test_multiple_corrupted_hops_all_reported(self):
        """Corrupt two hops; both must appear in the violations list."""
        num_segs = 3
        train, g = self._train_with_schedule(num_segments=num_segs, km_per_segment=100.0, max_speed=160.0)
        route = train.route
        sched = dict(train.schedule)

        # Corrupt hops 1 and 3 (0-indexed: route[1] and route[3]).
        for hop_idx in [1, 3]:
            prev_node = route[hop_idx - 1].node_id
            curr_node = route[hop_idx].node_id
            dep = sched[prev_node].scheduled_departure
            sched[curr_node] = ScheduleEntry(
                scheduled_arrival=dep + timedelta(seconds=1),
                scheduled_departure=dep + timedelta(seconds=1),
            )

        from app.models import Train as TrainModel
        m = TrainModel(
            id="t-mc", name="MC", color="#000", num_carriages=1,
            max_speed=160.0, avg_speed=100.0, route=route, schedule=sched,
        )
        corrupted = TrainRuntime(m)
        violations = validate_schedule(corrupted, g)
        assert len(violations) == 2
        hop_indices = {v.hop_index for v in violations}
        assert hop_indices == {1, 3}

    def test_validate_does_not_raise(self):
        """validate_schedule must return a list, never raise, even on bad schedules."""
        train, g = self._train_with_schedule()
        route = train.route
        sched = train.schedule

        # Corrupt all hops to 0-width windows.
        for hop in route[1:]:
            prev = route[route.index(hop) - 1].node_id
            sched[hop.node_id] = ScheduleEntry(
                scheduled_arrival=sched[prev].scheduled_departure,
                scheduled_departure=sched[prev].scheduled_departure,
            )

        from app.models import Train as TrainModel
        m = TrainModel(
            id="t-nr", name="NR", color="#000", num_carriages=1,
            max_speed=160.0, avg_speed=100.0, route=route, schedule=sched,
        )
        corrupted = TrainRuntime(m)

        result = validate_schedule(corrupted, g)  # must not raise
        assert isinstance(result, list)


# ===========================================================================
# 4. Physics helpers
# ===========================================================================


class TestPhysicsHelpers:
    def test_segment_distance_km_sums_block_lengths(self):
        g, stations, segments = _make_linear_network(num_stations=2, km_per_segment=50.0)
        dist = segment_distance_km(segments[0], g)
        assert dist == pytest.approx(50.0)

    def test_route_total_distance_ignores_origin_hop(self):
        """First hop has segment_id=None and must not be counted."""
        g, stations, segments = _make_linear_network(num_stations=3, km_per_segment=100.0)
        route = _make_route(stations, segments)
        total = route_total_distance_km(route, g)
        assert total == pytest.approx(200.0)

    def test_route_total_distance_empty_route(self):
        g, *_ = _make_linear_network(num_stations=2, km_per_segment=100.0)
        assert route_total_distance_km([], g) == 0.0

    def test_route_total_distance_single_hop_no_segment(self):
        g, stations, *_ = _make_linear_network(num_stations=2, km_per_segment=100.0)
        route = [RouteHop(node_id=stations[0], segment_id=None)]
        assert route_total_distance_km(route, g) == 0.0

    def test_travel_time_hours_formula(self):
        assert travel_time_hours(100.0, 50.0) == pytest.approx(2.0)
        assert travel_time_hours(0.0, 100.0) == pytest.approx(0.0)
        assert travel_time_hours(200.0, 100.0) == pytest.approx(2.0)

    def test_travel_time_raises_for_zero_speed(self):
        with pytest.raises(ValueError, match="speed_kmh"):
            travel_time_hours(100.0, 0.0)

    def test_travel_time_raises_for_negative_speed(self):
        with pytest.raises(ValueError, match="speed_kmh"):
            travel_time_hours(100.0, -10.0)
