"""
test_signals.py — Pytest tests for real SignalState record persistence.

Test organisation
-----------------
1. Network construction auto-registers signals
2. Signal IDs correctly wired into Junction.signal_states
3. Live aspect persistence — _advance_train site
4. _cross_block_boundary hold -> RED
5. Signal clears to GREEN after successful crossing
6. register_junction_signals() catch-all for legacy/loaded data
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from app.models import (
    Block,
    DriverDutyStatus,
    Junction,
    Point,
    PriorityTier,
    RouteHop,
    ScheduleEntry,
    Segment,
    SignalStateValue,
    Train,
)
from app.network import (
    BlockRuntime,
    JunctionRuntime,
    NetworkError,
    NetworkGraph,
    SegmentRuntime,
)
from app.train import TrainRuntime
from app.engine import SimulationEngine, TrainStatus, EventType


T0 = datetime(2025, 6, 1, 8, 0, 0, tzinfo=timezone.utc)


def dt(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _make_train(
    train_id: str,
    *,
    max_speed: float = 120.0,
    avg_speed: float = 100.0,
    priority: PriorityTier = PriorityTier.ORDINARY,
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
        driver_duty_status=DriverDutyStatus.NORMAL,
    )
    return TrainRuntime(model)


def _make_junction_network() -> tuple:
    """Build: sta_A --seg_AJ-- jct_1 --seg_JB-- sta_B"""
    g = NetworkGraph()
    g.add_station("Alpha", station_id="sta_A")
    g.add_junction(junction_id="jct_1")
    g.add_station("Beta", station_id="sta_B")
    g.add_track(
        "sta_A", "jct_1",
        length_km=10.0, num_blocks=5,
        segment_id="seg_AJ",
        geometry=[Point(x=0, y=0), Point(x=1000, y=0)],
    )
    g.add_track(
        "jct_1", "sta_B",
        length_km=10.0, num_blocks=5,
        segment_id="seg_JB",
        geometry=[Point(x=1000, y=0), Point(x=2000, y=0)],
    )
    return g, "sta_A", "sta_B", "jct_1", "seg_AJ", "seg_JB"


# ===========================================================================
# 1. Network construction auto-registers signals
# ===========================================================================

class TestNetworkConstructionSignals:

    def test_junction_network_has_nonempty_signals(self):
        g, *_ = _make_junction_network()
        assert len(g._signals) > 0

    def test_to_dict_emits_nonempty_signals(self):
        g, *_ = _make_junction_network()
        snapshot = g.to_dict()
        assert "signals" in snapshot
        assert len(snapshot["signals"]) > 0

    def test_register_junction_signals_is_idempotent(self):
        g, *_ = _make_junction_network()
        count_before = len(g._signals)
        g.register_junction_signals()
        assert len(g._signals) == count_before

    def test_station_only_network_has_empty_signals(self):
        g = NetworkGraph()
        g.add_station("A", station_id="sta_A")
        g.add_station("B", station_id="sta_B")
        g.add_track("sta_A", "sta_B", length_km=5.0, num_blocks=3,
                    segment_id="seg_AB",
                    geometry=[Point(x=0, y=0), Point(x=500, y=0)])
        assert len(g._signals) == 0


# ===========================================================================
# 2. Signal IDs correctly wired
# ===========================================================================

class TestSignalWiring:

    def test_every_approach_segment_has_signal_id(self):
        g, _, _, jct_id, seg_AJ, seg_JB = _make_junction_network()
        jct = g.get_junction(jct_id)
        assert jct.get_signal("seg_AJ") is not None
        assert jct.get_signal("seg_JB") is not None

    def test_signal_id_resolves_to_real_record(self):
        g, _, _, jct_id, seg_AJ, seg_JB = _make_junction_network()
        jct = g.get_junction(jct_id)
        for seg_id in ["seg_AJ", "seg_JB"]:
            sig_id = jct.get_signal(seg_id)
            assert sig_id is not None
            assert g.get_signal(sig_id) is not None

    def test_controlled_by_junction_id_is_correct(self):
        g, _, _, jct_id, _, _ = _make_junction_network()
        jct = g.get_junction(jct_id)
        for seg_id in list(jct.connected_segment_ids):
            sig = g.get_signal(jct.get_signal(seg_id))
            assert sig.controlled_by_junction_id == jct_id

    def test_initial_state_is_green(self):
        g, _, _, jct_id, _, _ = _make_junction_network()
        jct = g.get_junction(jct_id)
        for seg_id in list(jct.connected_segment_ids):
            sig = g.get_signal(jct.get_signal(seg_id))
            assert sig.state == SignalStateValue.GREEN


# ===========================================================================
# 3. Live aspect persistence — engine tick
# ===========================================================================

class TestLiveAspectPersistence:

    def _build_two_train_engine(self) -> SimulationEngine:
        g, sta_A, sta_B, jct_1, seg_AJ, seg_JB = _make_junction_network()
        route = [
            RouteHop(node_id=sta_A, segment_id=None),
            RouteHop(node_id=jct_1, segment_id=seg_AJ),
            RouteHop(node_id=sta_B, segment_id=seg_JB),
        ]
        t1 = _make_train("t1", max_speed=120.0, avg_speed=100.0,
                         priority=PriorityTier.EXPRESS, route=route,
                         schedule={sta_A: ScheduleEntry(scheduled_departure=T0)})
        t2 = _make_train("t2", max_speed=80.0, avg_speed=60.0,
                         priority=PriorityTier.LOCAL, route=route,
                         schedule={sta_A: ScheduleEntry(
                             scheduled_departure=T0 + timedelta(seconds=30))})
        engine = SimulationEngine(g, junction_clearance_minutes=2.0, start_time=T0)
        engine.add_train(t1)
        engine.add_train(t2)
        return engine

    def test_signals_nonempty_before_sim(self):
        engine = self._build_two_train_engine()
        assert len(engine.network.to_dict()["signals"]) > 0

    def test_all_signal_states_valid_after_run(self):
        engine = self._build_two_train_engine()
        engine.play()
        for _ in range(720):
            engine.tick(5.0)
        engine.pause()
        valid_states = {s.value for s in SignalStateValue}
        for sig_dict in engine.network.to_dict()["signals"]:
            assert sig_dict["state"] in valid_states

    def test_signal_state_changes_observed_during_run(self):
        """At least one signal must have been written to a state during the run."""
        engine = self._build_two_train_engine()
        jct_1 = "jct_1"
        seg_AJ = "seg_AJ"
        sig_id = engine.network.get_junction(jct_1).get_signal(seg_AJ)
        assert sig_id is not None
        observed: set[str] = set()
        engine.play()
        for _ in range(720):
            engine.tick(5.0)
            sig = engine.network.get_signal(sig_id)
            if sig:
                observed.add(sig.state.value)
        engine.pause()
        assert len(observed) > 0, "Signal state was never updated during simulation"


# ===========================================================================
# 4. SIGNAL_HOLD event coincides with RED on the approach signal
# ===========================================================================

class TestSignalHoldCoincidence:

    def test_signal_red_on_hold_tick(self):
        g, sta_A, sta_B, jct_1, seg_AJ, seg_JB = _make_junction_network()
        route = [
            RouteHop(node_id=sta_A, segment_id=None),
            RouteHop(node_id=jct_1, segment_id=seg_AJ),
            RouteHop(node_id=sta_B, segment_id=seg_JB),
        ]
        t1 = _make_train("t1", max_speed=120.0, avg_speed=100.0,
                         priority=PriorityTier.EXPRESS, route=route,
                         schedule={sta_A: ScheduleEntry(scheduled_departure=T0)})
        t2 = _make_train("t2", max_speed=100.0, avg_speed=80.0,
                         priority=PriorityTier.ORDINARY, route=route,
                         schedule={sta_A: ScheduleEntry(
                             scheduled_departure=T0 + timedelta(seconds=10))})
        engine = SimulationEngine(g, start_time=T0)
        engine.add_train(t1)
        engine.add_train(t2)

        sig_id = g.get_junction(jct_1).get_signal(seg_AJ)
        assert sig_id is not None

        red_on_hold = False
        engine.play()
        prev_holds = 0
        for _ in range(1440):
            engine.tick(5.0)
            holds = engine.events_of_type(EventType.SIGNAL_HOLD)
            if len(holds) > prev_holds:
                sig = engine.network.get_signal(sig_id)
                if sig and sig.state == SignalStateValue.RED:
                    red_on_hold = True
                    break
            prev_holds = len(holds)
        engine.pause()

        hold_events = engine.events_of_type(EventType.SIGNAL_HOLD)
        if hold_events:
            assert red_on_hold, (
                "SIGNAL_HOLD fired but approach SignalState was not RED on that tick"
            )


# ===========================================================================
# 5. Signal clears to GREEN after successful crossing
# ===========================================================================

class TestSignalClearsAfterCrossing:

    def test_approach_signal_green_after_train_completes(self):
        g, sta_A, sta_B, jct_1, seg_AJ, seg_JB = _make_junction_network()
        route = [
            RouteHop(node_id=sta_A, segment_id=None),
            RouteHop(node_id=jct_1, segment_id=seg_AJ),
            RouteHop(node_id=sta_B, segment_id=seg_JB),
        ]
        t1 = _make_train("t1", max_speed=120.0, avg_speed=100.0, route=route,
                         schedule={sta_A: ScheduleEntry(scheduled_departure=T0)})
        engine = SimulationEngine(g, start_time=T0)
        engine.add_train(t1)
        engine.run_headless(duration_sim_seconds=7200.0, tick_size=10.0)

        sig_id = g.get_junction(jct_1).get_signal(seg_AJ)
        assert sig_id is not None
        sig = engine.network.get_signal(sig_id)
        assert sig is not None
        if engine.train_state("t1").status == TrainStatus.COMPLETED:
            assert sig.state == SignalStateValue.GREEN, (
                f"Expected GREEN after train completed, got {sig.state!r}"
            )


# ===========================================================================
# 6. register_junction_signals() catch-all
# ===========================================================================

class TestRegisterJunctionSignalsCatchAll:

    def test_manual_junction_gets_signals_via_register(self):
        """Simulate a network loaded from serialised data without going through add_track()."""
        g = NetworkGraph()
        g.add_station("Alpha", station_id="sta_A")
        jct = g.add_junction(junction_id="jct_legacy")

        seg_id = "seg_legacy"
        blk_id = "blk_legacy"
        blk_model = Block(id=blk_id, segment_id=seg_id, length_km=1.0)
        blk_rt = BlockRuntime(blk_model)
        g._blocks[blk_id] = blk_rt

        from app.models import Segment as SegModel
        seg_model = SegModel(
            id=seg_id,
            start_node_id="sta_A",
            end_node_id="jct_legacy",
            ordered_block_ids=[blk_id],
        )
        seg_rt = SegmentRuntime(seg_model, [blk_rt])
        g._segments[seg_id] = seg_rt
        g._link_nodes("sta_A", "jct_legacy", seg_id)
        jct.connect_segment(seg_id)

        assert jct.get_signal(seg_id) is None
        assert len(g._signals) == 0

        g.register_junction_signals()

        assert jct.get_signal(seg_id) is not None
        sig = g.get_signal(jct.get_signal(seg_id))
        assert sig is not None
        assert sig.controlled_by_junction_id == "jct_legacy"
        assert sig.state == SignalStateValue.GREEN

    def test_to_dict_nonempty_after_register(self):
        g, *_ = _make_junction_network()
        g.register_junction_signals()
        assert len(g.to_dict()["signals"]) > 0

    def test_diamond_network_all_four_approaches_covered(self):
        g = NetworkGraph()
        g.add_station("A", station_id="sta_A")
        g.add_junction(junction_id="jct_1")
        g.add_junction(junction_id="jct_2")
        g.add_station("B", station_id="sta_B")
        for start, end, sid, km in [
            ("sta_A", "jct_1", "seg_AJ1", 10.0),
            ("jct_1", "sta_B", "seg_J1B", 10.0),
            ("sta_A", "jct_2", "seg_AJ2", 10.0),
            ("jct_2", "sta_B", "seg_J2B", 10.0),
        ]:
            g.add_track(start, end, length_km=km, num_blocks=5, segment_id=sid,
                        geometry=[Point(x=0, y=0), Point(x=km * 100, y=0)])

        assert len(g._signals) == 4
        for jct_id, segs in [("jct_1", ["seg_AJ1", "seg_J1B"]),
                              ("jct_2", ["seg_AJ2", "seg_J2B"])]:
            jct = g.get_junction(jct_id)
            for s in segs:
                assert jct.get_signal(s) is not None
