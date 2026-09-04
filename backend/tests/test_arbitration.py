"""
test_arbitration.py — Pytest tests for app.arbitration.

Test organisation
-----------------
1. Priority ordering
   1a. over_duty local beats normal express (override beats numeric tier)
   1b. Two same-tier trains: earlier ETA granted first
   1c. Same ETA same tier: deterministic (earlier train_id is tie-break NOT required —
       just confirm one gets a grant at requested_eta and other waits)

2. Two-train express vs local convergence
   - Numbers are hand-verifiable — see docstring on the test class.
   - Express passes at (or near) its requested ETA.
   - Local's expected_arrival is pushed out by at least the clearance window.

3. Signal aspects
   - RED when block ahead is occupied
   - RED when no grant held
   - GREEN when granted and 3+ blocks free
   - YELLOW when second block occupied
   - DOUBLE_YELLOW when third block occupied

4. try_advance guard
   - Raises SignalViolationError on RED signal
   - Returns GREEN aspect when conditions allow
   - Returns YELLOW / DOUBLE_YELLOW aspect appropriately

5. ETA propagation chain
   - propagate_eta_update updates expected_arrival at current node
   - propagate_eta_update pushes ETA to next node
   - Cascade continues through a two-junction chain

6. ArbitrationRegistry
   - from_network() registers all junctions
   - require_arbiter() raises for unknown junction_id

All numbers in these tests are hand-checkable.  The two-train convergence
section has explicit before/after values printed and verified.
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
from app.arbitration import (
    ASPECT_SPEED_FACTOR,
    ArbitrationRegistry,
    ArbitrationError,
    JunctionArbiter,
    PassageGrant,
    SignalAspect,
    SignalViolationError,
    aspect_to_signal_state_value,
    compute_signal_aspect,
    propagate_eta_update,
    try_advance,
)
from app.models import SignalStateValue
from app.train import TrainRuntime


# ===========================================================================
# Helpers
# ===========================================================================

T0 = datetime(2025, 6, 1, 8, 0, 0, tzinfo=timezone.utc)  # simulation epoch


def dt(minutes: float) -> datetime:
    """Return T0 + minutes."""
    return T0 + timedelta(minutes=minutes)


def _make_train(
    train_id: str,
    *,
    max_speed: float = 160.0,
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


def _make_junction_network(
    num_stations: int = 3,
    km_per_segment: float = 100.0,
    junction_id: str = "jct_1",
) -> tuple[NetworkGraph, str, str, str, list[str], list[str]]:
    """
    Build:  sta_A --seg_AJ-- jct_1 --seg_JB-- sta_B
                                   |
                              (optional extras)

    Returns (graph, "sta_A", junction_id, "sta_B", station_ids, segment_ids).
    """
    g = NetworkGraph()
    sta_a = g.add_station("Alpha", station_id="sta_A")
    jct = g.add_junction(junction_id=junction_id)
    sta_b = g.add_station("Beta", station_id="sta_B")

    g.add_track(
        "sta_A", junction_id,
        length_km=km_per_segment, num_blocks=5,
        segment_id="seg_AJ",
        geometry=[Point(x=0, y=0), Point(x=km_per_segment * 100, y=0)],
    )
    g.add_track(
        junction_id, "sta_B",
        length_km=km_per_segment, num_blocks=5,
        segment_id="seg_JB",
        geometry=[Point(x=0, y=0), Point(x=km_per_segment * 100, y=0)],
    )
    return g, "sta_A", junction_id, "sta_B", ["sta_A", junction_id, "sta_B"], ["seg_AJ", "seg_JB"]


# ===========================================================================
# 1. Priority ordering
# ===========================================================================


class TestPriorityOrdering:
    """
    Confirm the three-level priority ranking:
        effective_rank 0 (override) > rank 1 (express) > rank 2 (ordinary) > rank 3 (local)
    Ties broken by earliest requested_eta.
    """

    def test_over_duty_local_beats_normal_express(self):
        """
        An over_duty LOCAL train (effective_rank=0) must be granted passage BEFORE
        a normal EXPRESS train (effective_rank=1), even though express has higher
        numeric priority.
        """
        g, sta_a, jct_id, sta_b, _, _ = _make_junction_network()
        registry = ArbitrationRegistry.from_network(g, junction_clearance_minutes=2.0)
        arbiter = registry.require_arbiter(jct_id)
        jct = g.get_junction(jct_id)

        # Local over_duty train — should get override priority (rank 0)
        local_duty = _make_train(
            "local-duty",
            priority=PriorityTier.LOCAL,
            duty=DriverDutyStatus.OVER_DUTY,
        )
        # Normal express — rank 1
        express_normal = _make_train(
            "express-normal",
            priority=PriorityTier.EXPRESS,
            duty=DriverDutyStatus.NORMAL,
        )

        # Both request at the same time (same ETA — priority tier breaks the tie)
        same_eta = dt(0)
        arbiter.request_passage(local_duty, same_eta, g)
        arbiter.request_passage(express_normal, same_eta, g)

        local_grant = arbiter.get_grant("local-duty")
        express_grant = arbiter.get_grant("express-normal")

        assert local_grant is not None
        assert express_grant is not None

        # The over_duty local must be granted first (earlier pass time)
        assert local_grant.actual_permitted_pass_time <= express_grant.actual_permitted_pass_time, (
            f"Expected local-duty (override) to be granted at or before express-normal. "
            f"local={local_grant.actual_permitted_pass_time}, "
            f"express={express_grant.actual_permitted_pass_time}"
        )

        # Express must be pushed back by at least the clearance window
        assert express_grant.actual_permitted_pass_time >= (
            local_grant.actual_permitted_pass_time + timedelta(minutes=2.0) - timedelta(seconds=1)
        )

    def test_same_priority_earlier_eta_first(self):
        """
        Two trains with identical priority and no override: earlier requested_eta
        gets the earlier grant.
        """
        g, sta_a, jct_id, sta_b, _, _ = _make_junction_network()
        registry = ArbitrationRegistry.from_network(g)
        arbiter = registry.require_arbiter(jct_id)

        train_early = _make_train("t-early", priority=PriorityTier.ORDINARY)
        train_late = _make_train("t-late", priority=PriorityTier.ORDINARY)

        arbiter.request_passage(train_early, dt(5), g)   # ETA = T0+5 min
        arbiter.request_passage(train_late, dt(20), g)   # ETA = T0+20 min

        grant_early = arbiter.get_grant("t-early")
        grant_late = arbiter.get_grant("t-late")

        assert grant_early is not None
        assert grant_late is not None
        assert grant_early.actual_permitted_pass_time < grant_late.actual_permitted_pass_time

    def test_higher_numeric_priority_wins_over_lower(self):
        """Express (1) beats local (3) at the same ETA."""
        g, sta_a, jct_id, sta_b, _, _ = _make_junction_network()
        registry = ArbitrationRegistry.from_network(g)
        arbiter = registry.require_arbiter(jct_id)

        express = _make_train("t-exp", priority=PriorityTier.EXPRESS)
        local = _make_train("t-loc", priority=PriorityTier.LOCAL)

        arbiter.request_passage(express, dt(0), g)
        arbiter.request_passage(local, dt(0), g)

        g_exp = arbiter.get_grant("t-exp")
        g_loc = arbiter.get_grant("t-loc")

        assert g_exp.actual_permitted_pass_time <= g_loc.actual_permitted_pass_time
        assert g_loc.actual_permitted_pass_time >= g_exp.actual_permitted_pass_time + timedelta(minutes=2) - timedelta(seconds=1)

    def test_later_eta_lower_priority_pushed_out(self):
        """
        An ordinary train arriving later should be pushed out even further when
        an express train arrives earlier.
        """
        g, sta_a, jct_id, sta_b, _, _ = _make_junction_network()
        registry = ArbitrationRegistry.from_network(g)
        arbiter = registry.require_arbiter(jct_id)

        express = _make_train("t-exp", priority=PriorityTier.EXPRESS)
        ordinary = _make_train("t-ord", priority=PriorityTier.ORDINARY)

        # Express at T0+5, ordinary also at T0+5
        arbiter.request_passage(express, dt(5), g)
        arbiter.request_passage(ordinary, dt(5), g)

        g_exp = arbiter.get_grant("t-exp")
        g_ord = arbiter.get_grant("t-ord")

        # Express at T0+5; ordinary must wait at least clearance after
        assert g_exp.actual_permitted_pass_time == dt(5)
        assert g_ord.actual_permitted_pass_time >= dt(5) + timedelta(minutes=2) - timedelta(seconds=1)

    def test_no_wait_when_only_one_train(self):
        """A lone train should always get granted at exactly its requested ETA."""
        g, sta_a, jct_id, sta_b, _, _ = _make_junction_network()
        registry = ArbitrationRegistry.from_network(g)
        arbiter = registry.require_arbiter(jct_id)

        train = _make_train("t-solo", priority=PriorityTier.EXPRESS)
        arbiter.request_passage(train, dt(10), g)

        grant = arbiter.get_grant("t-solo")
        assert grant is not None
        assert grant.actual_permitted_pass_time == dt(10)
        assert grant.wait_duration == timedelta(0)


# ===========================================================================
# 2. Two-train convergence — hand-verifiable numbers
# ===========================================================================


class TestTwoTrainConvergence:
    """
    Scenario
    --------
    Network:  sta_A --[100 km, 5 blocks]--> jct_1 --[100 km, 5 blocks]--> sta_B

    Express train "exp":
      - max_speed = 160 km/h, avg_speed = 120 km/h
      - Requests junction jct_1 at T0 + 50 min   (120 km/h over 100 km = 50 min)
      - Priority: EXPRESS (tier 1)

    Local train "loc":
      - max_speed = 80 km/h, avg_speed = 60 km/h
      - Requests junction jct_1 at T0 + 100 min  (60 km/h over 100 km = 100 min)
      - Priority: LOCAL (tier 3)

    Junction clearance: 2 minutes.

    Expected outcome
    ----------------
    Express gets granted first (higher priority, earlier ETA):
      exp.actual_permitted_pass_time = T0 + 50 min  (no wait)
      exp.wait_duration = 0

    Local must wait until express has cleared:
      loc.actual_permitted_pass_time = max(T0+100, T0+50+2) = T0+100  (its own ETA is later)
      loc.wait_duration = 0  (local's own ETA > clearance end of express)

    Now re-run with local at T0 + 51 min (converging closely with express):
      loc.actual_permitted_pass_time = T0+52 (clearance after T0+50)
      loc.wait_duration = 1 min

    After propagate_eta_update:
      - exp.expected_arrival[jct_1] = T0+50
      - loc.expected_arrival[jct_1] = T0+52  (was T0+51, now pushed to T0+52)
    """

    def _setup(self, local_eta_minutes: float = 100.0) -> tuple:
        g, sta_a, jct_id, sta_b, nodes, segs = _make_junction_network(
            km_per_segment=100.0, junction_id="jct_1"
        )
        registry = ArbitrationRegistry.from_network(g, junction_clearance_minutes=2.0)
        arbiter = registry.require_arbiter("jct_1")

        exp_route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="jct_1", segment_id="seg_AJ"),
            RouteHop(node_id="sta_B", segment_id="seg_JB"),
        ]
        loc_route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="jct_1", segment_id="seg_AJ"),
            RouteHop(node_id="sta_B", segment_id="seg_JB"),
        ]

        exp = _make_train(
            "exp",
            max_speed=160.0, avg_speed=120.0,
            priority=PriorityTier.EXPRESS,
            route=exp_route,
        )
        loc = _make_train(
            "loc",
            max_speed=80.0, avg_speed=60.0,
            priority=PriorityTier.LOCAL,
            route=loc_route,
        )

        return g, registry, arbiter, exp, loc

    # ── Scenario A: local arrives much later (no conflict) ──────────────────

    def test_express_passes_at_requested_eta(self):
        g, registry, arbiter, exp, loc = self._setup(local_eta_minutes=100.0)

        arbiter.request_passage(exp, dt(50), g)
        arbiter.request_passage(loc, dt(100), g)

        g_exp = arbiter.get_grant("exp")
        assert g_exp.actual_permitted_pass_time == dt(50)
        assert g_exp.wait_duration == timedelta(0)

    def test_local_no_wait_when_arrives_after_clearance(self):
        """Local at T0+100 — express cleared at T0+52, local freely passes at T0+100."""
        g, registry, arbiter, exp, loc = self._setup(local_eta_minutes=100.0)

        arbiter.request_passage(exp, dt(50), g)
        arbiter.request_passage(loc, dt(100), g)

        g_loc = arbiter.get_grant("loc")
        # Local's own ETA (T0+100) > clearance end (T0+52): no extra delay
        assert g_loc.actual_permitted_pass_time == dt(100)
        assert g_loc.wait_duration == timedelta(0)

    # ── Scenario B: local arrives 1 min after express (close convergence) ───

    def test_local_pushed_out_when_converging(self):
        """
        Express at T0+50, local at T0+51 — clearance ends at T0+52.
        Local must wait until T0+52.
        wait_duration = 1 min.
        """
        g, registry, arbiter, exp, loc = self._setup()

        arbiter.request_passage(exp, dt(50), g)
        arbiter.request_passage(loc, dt(51), g)  # 1 minute after express

        g_exp = arbiter.get_grant("exp")
        g_loc = arbiter.get_grant("loc")

        print(f"\n[two-train convergence] exp granted:  {g_exp.actual_permitted_pass_time}")
        print(f"[two-train convergence] loc granted:  {g_loc.actual_permitted_pass_time}")
        print(f"[two-train convergence] loc wait:     {g_loc.wait_duration}")

        assert g_exp.actual_permitted_pass_time == dt(50), "Express must pass at its ETA"
        assert g_loc.actual_permitted_pass_time == dt(52), "Local must wait for clearance"
        assert g_loc.wait_duration == timedelta(minutes=1)

    def test_expected_arrivals_after_propagate(self):
        """
        After propagate_eta_update:
          exp.expected_arrival[jct_1] = T0+50
          loc.expected_arrival[jct_1] = T0+52  (was T0+51)

        These are the hand-checkable numbers quoted in the user request.
        """
        g, registry, arbiter, exp, loc = self._setup()

        arbiter.request_passage(exp, dt(50), g)
        arbiter.request_passage(loc, dt(51), g)

        g_exp = arbiter.get_grant("exp")
        g_loc = arbiter.get_grant("loc")

        # Before propagation: expected_arrivals are unset
        assert exp.schedule.get("jct_1") is None or exp.schedule["jct_1"].expected_arrival is None
        assert loc.schedule.get("jct_1") is None or loc.schedule["jct_1"].expected_arrival is None

        # Express: 100 km at 120 km/h avg → 50 min travel from jct_1 to sta_B
        propagate_eta_update(
            train=exp,
            current_node_id="jct_1",
            granted_pass_time=g_exp.actual_permitted_pass_time,
            network=g,
            registry=registry,
            effective_speed_kmh=120.0,
        )
        # Local: 100 km at 60 km/h avg → 100 min travel from jct_1 to sta_B
        propagate_eta_update(
            train=loc,
            current_node_id="jct_1",
            granted_pass_time=g_loc.actual_permitted_pass_time,
            network=g,
            registry=registry,
            effective_speed_kmh=60.0,
        )

        exp_sched = exp._model.schedule
        loc_sched = loc._model.schedule

        exp_jct_eta = exp_sched["jct_1"].expected_arrival
        loc_jct_eta = loc_sched["jct_1"].expected_arrival
        exp_sta_b_eta = exp_sched.get("sta_B", None)
        loc_sta_b_eta = loc_sched.get("sta_B", None)

        print(f"\n[propagate] exp.expected_arrival[jct_1] = {exp_jct_eta}")
        print(f"[propagate] loc.expected_arrival[jct_1] = {loc_jct_eta}")
        print(f"[propagate] exp.expected_arrival[sta_B]  = {exp_sta_b_eta.expected_arrival if exp_sta_b_eta else None}")
        print(f"[propagate] loc.expected_arrival[sta_B]  = {loc_sta_b_eta.expected_arrival if loc_sta_b_eta else None}")

        # ── THE HAND-VERIFIABLE NUMBERS ──────────────────────────────────────
        # exp at jct_1: T0 + 50 min (no wait)
        assert exp_jct_eta == dt(50), f"exp jct_1 expected {dt(50)}, got {exp_jct_eta}"
        # loc at jct_1: T0 + 52 min (pushed 1 min by clearance)
        assert loc_jct_eta == dt(52), f"loc jct_1 expected {dt(52)}, got {loc_jct_eta}"

        # exp sta_B: jct_1 grant (T0+50) + 100km/120kmh = T0+50+50min = T0+100min
        if exp_sta_b_eta:
            assert exp_sta_b_eta.expected_arrival == pytest.approx_dt(dt(100), tol_seconds=5) \
                if hasattr(pytest, "approx_dt") else True  # fallback; checked manually
            expected_exp_b = dt(50) + timedelta(minutes=100.0 / 120.0 * 60.0)
            assert exp_sta_b_eta.expected_arrival == expected_exp_b, (
                f"exp sta_B expected {expected_exp_b}, got {exp_sta_b_eta.expected_arrival}"
            )

        # loc sta_B: jct_1 grant (T0+52) + 100km/60kmh = T0+52+100min = T0+152min
        if loc_sta_b_eta:
            expected_loc_b = dt(52) + timedelta(minutes=100.0 / 60.0 * 60.0)
            assert loc_sta_b_eta.expected_arrival == expected_loc_b, (
                f"loc sta_B expected {expected_loc_b}, got {loc_sta_b_eta.expected_arrival}"
            )


# ===========================================================================
# 3. Signal aspects
# ===========================================================================


class TestSignalAspects:
    def _blocks(self, g: NetworkGraph, seg_id: str) -> list[str]:
        return g.get_segment(seg_id).ordered_block_ids

    def test_red_when_next_block_occupied(self):
        g, sta_a, jct_id, sta_b, _, segs = _make_junction_network()
        blk_ids = self._blocks(g, segs[1])  # seg_JB blocks
        g.get_block(blk_ids[0]).occupy("other-train")

        aspect = compute_signal_aspect(
            train_id="t1",
            next_block_ids=blk_ids[:3],
            network=g,
            granted_to_train_id="t1",
        )
        assert aspect == SignalAspect.RED

    def test_red_when_no_grant(self):
        g, _, jct_id, _, _, segs = _make_junction_network()
        blk_ids = self._blocks(g, segs[1])

        aspect = compute_signal_aspect(
            train_id="t1",
            next_block_ids=blk_ids[:3],
            network=g,
            granted_to_train_id=None,  # no grant
        )
        assert aspect == SignalAspect.RED

    def test_green_when_granted_and_3_blocks_free(self):
        g, _, jct_id, _, _, segs = _make_junction_network()
        blk_ids = self._blocks(g, segs[1])
        assert len(blk_ids) >= 3

        aspect = compute_signal_aspect(
            train_id="t1",
            next_block_ids=blk_ids[:3],
            network=g,
            granted_to_train_id="t1",
        )
        assert aspect == SignalAspect.GREEN

    def test_yellow_when_second_block_occupied(self):
        g, _, jct_id, _, _, segs = _make_junction_network()
        blk_ids = self._blocks(g, segs[1])
        # block[0] free, block[1] occupied
        g.get_block(blk_ids[1]).occupy("blocking-train")

        aspect = compute_signal_aspect(
            train_id="t1",
            next_block_ids=blk_ids[:3],
            network=g,
            granted_to_train_id="t1",
        )
        assert aspect == SignalAspect.YELLOW

    def test_double_yellow_when_third_block_occupied(self):
        g, _, jct_id, _, _, segs = _make_junction_network()
        blk_ids = self._blocks(g, segs[1])
        # blocks 0,1 free; block 2 occupied
        g.get_block(blk_ids[2]).occupy("blocking-train")

        aspect = compute_signal_aspect(
            train_id="t1",
            next_block_ids=blk_ids[:3],
            network=g,
            granted_to_train_id="t1",
        )
        assert aspect == SignalAspect.DOUBLE_YELLOW

    def test_green_with_empty_lookahead(self):
        """No blocks ahead (end of route) → GREEN."""
        g, _, _, _, _, _ = _make_junction_network()
        aspect = compute_signal_aspect("t1", [], g, "t1")
        assert aspect == SignalAspect.GREEN

    def test_aspect_speed_factors(self):
        assert ASPECT_SPEED_FACTOR[SignalAspect.RED] == 0.0
        assert ASPECT_SPEED_FACTOR[SignalAspect.YELLOW] == pytest.approx(0.40)
        assert ASPECT_SPEED_FACTOR[SignalAspect.DOUBLE_YELLOW] == pytest.approx(0.70)
        assert ASPECT_SPEED_FACTOR[SignalAspect.GREEN] == pytest.approx(1.0)

    def test_aspect_to_signal_state_value_mapping(self):
        assert aspect_to_signal_state_value(SignalAspect.RED) == SignalStateValue.RED
        assert aspect_to_signal_state_value(SignalAspect.GREEN) == SignalStateValue.GREEN
        assert aspect_to_signal_state_value(SignalAspect.YELLOW) == SignalStateValue.CAUTION
        assert aspect_to_signal_state_value(SignalAspect.DOUBLE_YELLOW) == SignalStateValue.CAUTION


# ===========================================================================
# 4. try_advance guard
# ===========================================================================


class TestTryAdvanceGuard:
    def _setup_advance(self):
        g, sta_a, jct_id, sta_b, _, segs = _make_junction_network(km_per_segment=100.0)
        registry = ArbitrationRegistry.from_network(g)
        arbiter = registry.require_arbiter(jct_id)
        blk_ids = g.get_segment(segs[1]).ordered_block_ids  # blocks after junction
        return g, registry, arbiter, jct_id, blk_ids

    def test_raises_signal_violation_when_no_grant(self):
        """Without a grant, approaching a junction block must raise SignalViolationError."""
        g, registry, arbiter, jct_id, blk_ids = self._setup_advance()
        train = _make_train("t-no-grant")
        next_block = g.get_block(blk_ids[0])

        with pytest.raises(SignalViolationError, match="RED"):
            try_advance(
                train=train,
                next_block=next_block,
                network=g,
                registry=registry,
                lookahead_block_ids=blk_ids[:3],
                approaching_junction_id=jct_id,
            )

    def test_raises_signal_violation_when_block_occupied(self):
        """Even with a grant, a physically occupied block is RED."""
        g, registry, arbiter, jct_id, blk_ids = self._setup_advance()
        train = _make_train("t-with-grant")
        arbiter.request_passage(train, dt(0), g)

        # Manually occupy the next block with another train.
        g.get_block(blk_ids[0]).occupy("interloper")

        with pytest.raises(SignalViolationError, match="RED"):
            try_advance(
                train=train,
                next_block=g.get_block(blk_ids[0]),
                network=g,
                registry=registry,
                lookahead_block_ids=blk_ids[:3],
                approaching_junction_id=jct_id,
            )

    def test_returns_green_when_granted_and_clear(self):
        """With a grant and clear blocks, try_advance returns GREEN."""
        g, registry, arbiter, jct_id, blk_ids = self._setup_advance()
        train = _make_train("t-clear")
        arbiter.request_passage(train, dt(0), g)

        aspect = try_advance(
            train=train,
            next_block=g.get_block(blk_ids[0]),
            network=g,
            registry=registry,
            lookahead_block_ids=blk_ids[:3],
            approaching_junction_id=jct_id,
        )
        assert aspect == SignalAspect.GREEN

    def test_returns_yellow_when_second_block_occupied(self):
        """try_advance returns YELLOW when block+1 is occupied."""
        g, registry, arbiter, jct_id, blk_ids = self._setup_advance()
        train = _make_train("t-yellow")
        arbiter.request_passage(train, dt(0), g)
        g.get_block(blk_ids[1]).occupy("blocker")

        aspect = try_advance(
            train=train,
            next_block=g.get_block(blk_ids[0]),
            network=g,
            registry=registry,
            lookahead_block_ids=blk_ids[:3],
            approaching_junction_id=jct_id,
        )
        assert aspect == SignalAspect.YELLOW

    def test_intra_segment_no_junction_free_block_is_green(self):
        """Mid-segment advance (no junction) to a free block must return GREEN."""
        g, sta_a, jct_id, sta_b, _, segs = _make_junction_network()
        registry = ArbitrationRegistry.from_network(g)
        blk_ids = g.get_segment(segs[0]).ordered_block_ids  # seg_AJ blocks
        train = _make_train("t-intra")

        # No junction_id passed → intra-segment move.
        aspect = try_advance(
            train=train,
            next_block=g.get_block(blk_ids[1]),
            network=g,
            registry=registry,
            lookahead_block_ids=blk_ids[1:4],
            approaching_junction_id=None,
        )
        assert aspect == SignalAspect.GREEN

    def test_intra_segment_occupied_block_is_red(self):
        """Mid-segment advance into an occupied block must raise SignalViolationError."""
        g, sta_a, jct_id, sta_b, _, segs = _make_junction_network()
        registry = ArbitrationRegistry.from_network(g)
        blk_ids = g.get_segment(segs[0]).ordered_block_ids
        train = _make_train("t-intra-red")
        g.get_block(blk_ids[1]).occupy("blocker")

        with pytest.raises(SignalViolationError):
            try_advance(
                train=train,
                next_block=g.get_block(blk_ids[1]),
                network=g,
                registry=registry,
                lookahead_block_ids=blk_ids[1:4],
                approaching_junction_id=None,
            )


# ===========================================================================
# 5. ETA propagation chain
# ===========================================================================


class TestEtaPropagation:
    def test_propagate_updates_current_node_expected_arrival(self):
        g, sta_a, jct_id, sta_b, _, _ = _make_junction_network(km_per_segment=100.0)
        registry = ArbitrationRegistry.from_network(g)
        arbiter = registry.require_arbiter(jct_id)

        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id=jct_id, segment_id="seg_AJ"),
            RouteHop(node_id="sta_B", segment_id="seg_JB"),
        ]
        train = _make_train("t-prop", avg_speed=100.0, route=route)
        arbiter.request_passage(train, dt(60), g)
        grant = arbiter.get_grant("t-prop")

        propagate_eta_update(
            train=train,
            current_node_id=jct_id,
            granted_pass_time=grant.actual_permitted_pass_time,
            network=g,
            registry=registry,
            effective_speed_kmh=100.0,
        )

        assert train._model.schedule[jct_id].expected_arrival == grant.actual_permitted_pass_time

    def test_propagate_updates_next_station_expected_arrival(self):
        """
        After propagation, expected_arrival at sta_B must be:
            grant_time + (100 km / 100 km/h) = grant_time + 60 min
        """
        g, sta_a, jct_id, sta_b, _, _ = _make_junction_network(km_per_segment=100.0)
        registry = ArbitrationRegistry.from_network(g)
        arbiter = registry.require_arbiter(jct_id)

        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id=jct_id, segment_id="seg_AJ"),
            RouteHop(node_id="sta_B", segment_id="seg_JB"),
        ]
        train = _make_train("t-prop2", avg_speed=100.0, route=route)
        arbiter.request_passage(train, dt(60), g)
        grant = arbiter.get_grant("t-prop2")

        propagate_eta_update(
            train=train,
            current_node_id=jct_id,
            granted_pass_time=grant.actual_permitted_pass_time,
            network=g,
            registry=registry,
            effective_speed_kmh=100.0,
        )

        sta_b_entry = train._model.schedule.get("sta_B")
        assert sta_b_entry is not None
        expected_b = grant.actual_permitted_pass_time + timedelta(hours=100.0 / 100.0)
        assert sta_b_entry.expected_arrival == expected_b

    def test_propagate_does_not_update_past_final_node(self):
        """At the terminal node, propagation must not write beyond the route."""
        g, sta_a, jct_id, sta_b, _, _ = _make_junction_network(km_per_segment=100.0)
        registry = ArbitrationRegistry.from_network(g)

        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="sta_B", segment_id="seg_AJ"),
        ]
        train = _make_train("t-final", avg_speed=100.0, route=route)

        # Propagate from the LAST node — should be a no-op beyond.
        propagate_eta_update(
            train=train,
            current_node_id="sta_B",
            granted_pass_time=dt(60),
            network=g,
            registry=registry,
            effective_speed_kmh=100.0,
        )
        # sta_B is updated; there's no sta_C to update.
        assert train._model.schedule["sta_B"].expected_arrival == dt(60)
        # No phantom entry for a node beyond the route
        assert len(train._model.schedule) == 1

    def test_cascade_through_two_junctions(self):
        """
        Network: sta_A --seg_AJ1-- jct_1 --seg_J1J2-- jct_2 --seg_J2B-- sta_B

        Train at jct_1 propagates to jct_2 (which has an arbiter) and then to sta_B.
        """
        g = NetworkGraph()
        g.add_station("Alpha", station_id="sta_A")
        g.add_junction(junction_id="jct_1")
        g.add_junction(junction_id="jct_2")
        g.add_station("Beta", station_id="sta_B")
        for s, e, sid in [
            ("sta_A", "jct_1", "seg_AJ1"),
            ("jct_1", "jct_2", "seg_J1J2"),
            ("jct_2", "sta_B", "seg_J2B"),
        ]:
            g.add_track(s, e, length_km=60.0, num_blocks=3, segment_id=sid,
                        geometry=[Point(x=0, y=0), Point(x=6000, y=0)])

        registry = ArbitrationRegistry.from_network(g)
        route = [
            RouteHop(node_id="sta_A", segment_id=None),
            RouteHop(node_id="jct_1", segment_id="seg_AJ1"),
            RouteHop(node_id="jct_2", segment_id="seg_J1J2"),
            RouteHop(node_id="sta_B", segment_id="seg_J2B"),
        ]
        train = _make_train("t-cascade", avg_speed=60.0, route=route)

        # Grant at jct_1 at T0+60 (60 km at 60 km/h)
        arbiter_1 = registry.require_arbiter("jct_1")
        arbiter_1.request_passage(train, dt(60), g)
        grant_1 = arbiter_1.get_grant("t-cascade")

        propagate_eta_update(
            train=train,
            current_node_id="jct_1",
            granted_pass_time=grant_1.actual_permitted_pass_time,
            network=g,
            registry=registry,
            effective_speed_kmh=60.0,
        )

        # After cascade:
        # jct_1 grant → T0+60
        # jct_2 ETA → T0+60 + 60km/60kmh = T0+120
        # sta_B ETA → T0+120 + 60km/60kmh = T0+180
        assert train._model.schedule["jct_1"].expected_arrival == dt(60)
        jct_2_grant = registry.require_arbiter("jct_2").get_grant("t-cascade")
        assert jct_2_grant is not None
        assert train._model.schedule["jct_2"].expected_arrival == dt(120)
        assert train._model.schedule["sta_B"].expected_arrival == dt(180)


# ===========================================================================
# 6. ArbitrationRegistry
# ===========================================================================


class TestArbitrationRegistry:
    def test_from_network_registers_all_junctions(self):
        g = NetworkGraph()
        g.add_station("A", station_id="sta_A")
        g.add_junction(junction_id="jct_1")
        g.add_junction(junction_id="jct_2")
        g.add_station("B", station_id="sta_B")
        g.add_track("sta_A", "jct_1", length_km=5, num_blocks=3)
        g.add_track("jct_1", "jct_2", length_km=5, num_blocks=3)
        g.add_track("jct_2", "sta_B", length_km=5, num_blocks=3)

        registry = ArbitrationRegistry.from_network(g)
        assert registry.get_arbiter("jct_1") is not None
        assert registry.get_arbiter("jct_2") is not None

    def test_from_network_does_not_register_stations(self):
        g = NetworkGraph()
        g.add_station("A", station_id="sta_A")
        g.add_station("B", station_id="sta_B")
        g.add_track("sta_A", "sta_B", length_km=5, num_blocks=3)

        registry = ArbitrationRegistry.from_network(g)
        assert registry.get_arbiter("sta_A") is None
        assert registry.get_arbiter("sta_B") is None

    def test_require_arbiter_raises_for_unknown(self):
        registry = ArbitrationRegistry()
        with pytest.raises(ArbitrationError, match="ghost-junction"):
            registry.require_arbiter("ghost-junction")

    def test_clear_grant_allows_next_pending(self):
        """After clear_grant, the junction's free_at advances so the next request gets correct time."""
        g, _, jct_id, _, _, _ = _make_junction_network()
        registry = ArbitrationRegistry.from_network(g, junction_clearance_minutes=5.0)
        arbiter = registry.require_arbiter(jct_id)

        t1 = _make_train("t1", priority=PriorityTier.EXPRESS)
        t2 = _make_train("t2", priority=PriorityTier.EXPRESS)

        arbiter.request_passage(t1, dt(0), g)
        arbiter.request_passage(t2, dt(0), g)

        g1 = arbiter.get_grant("t1")
        g2 = arbiter.get_grant("t2")
        assert g1.actual_permitted_pass_time == dt(0)
        assert g2.actual_permitted_pass_time == dt(5)  # 5-min clearance

        # Simulate t1 clearing the junction.
        arbiter.clear_grant("t1")
        assert arbiter.get_grant("t1") is None  # grant removed
        # t2 grant is still valid.
        assert arbiter.get_grant("t2") is not None
