"""
Round-trip serialisation tests for every domain model.

Strategy
--------
1. Construct a minimal but valid instance of each model.
2. Serialise to a JSON string via model.model_dump_json().
3. Deserialise back via Model.model_validate_json(json_str).
4. Assert the re-hydrated instance equals the original.

This catches field renames, type coercions that lose precision, and any
validator that rejects its own serialised output.

We also verify that the FastAPI /api/health endpoint returns 200.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.models import (
    Block,
    DriverDutyStatus,
    Junction,
    MaintenanceWindow,
    Point,
    PriorityTier,
    RouteHop,
    ScheduleEntry,
    Segment,
    SignalState,
    SignalStateValue,
    Station,
    StationType,
    Track,
    TrackDirectionality,
    Train,
    WeatherCell,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _roundtrip(model_instance):
    """Serialise → JSON string → re-hydrate → compare."""
    json_str = model_instance.model_dump_json()
    # Confirm the output is valid JSON before handing it back to Pydantic.
    parsed = json.loads(json_str)
    assert isinstance(parsed, dict), "model_dump_json must produce a JSON object"

    rehydrated = model_instance.__class__.model_validate_json(json_str)
    assert rehydrated == model_instance, (
        f"{model_instance.__class__.__name__} round-trip mismatch:\n"
        f"  original   = {model_instance!r}\n"
        f"  rehydrated = {rehydrated!r}"
    )
    return rehydrated


# ---------------------------------------------------------------------------
# Model round-trip tests
# ---------------------------------------------------------------------------


def test_weather_cell_roundtrip():
    wc = WeatherCell(
        id="wc-1",
        intensity=0.65,
        affected_block_ids=["blk-1", "blk-2"],
    )
    _roundtrip(wc)


def test_signal_state_roundtrip():
    ss = SignalState(
        id="sig-1",
        block_id="blk-1",
        state=SignalStateValue.CAUTION,
        controlled_by_junction_id="jct-1",
    )
    _roundtrip(ss)


def test_signal_state_autonomous_roundtrip():
    """controlled_by_junction_id should survive as None."""
    ss = SignalState(id="sig-2", block_id="blk-2")
    rt = _roundtrip(ss)
    assert rt.controlled_by_junction_id is None


def test_block_roundtrip_full():
    blk = Block(
        id="blk-1",
        segment_id="seg-1",
        length_km=1.2,
        occupied_by="train-42",
        weather_cell_id="wc-1",
        speed_restriction=80.0,
        maintenance_window=MaintenanceWindow(
            start_iso="2025-06-01T00:00:00Z",
            end_iso="2025-06-02T00:00:00Z",
        ),
    )
    _roundtrip(blk)


def test_block_roundtrip_minimal():
    """All optional fields default correctly after a round-trip."""
    blk = Block(id="blk-2", segment_id="seg-1", length_km=0.8)
    rt = _roundtrip(blk)
    assert rt.occupied_by is None
    assert rt.weather_cell_id is None
    assert rt.speed_restriction is None
    assert rt.maintenance_window is None


def test_segment_roundtrip():
    seg = Segment(
        id="seg-1",
        start_node_id="jct-A",
        end_node_id="sta-B",
        ordered_block_ids=["blk-1", "blk-2", "blk-3"],
    )
    _roundtrip(seg)


def test_junction_roundtrip():
    jct = Junction(
        id="jct-1",
        connected_segment_ids=["seg-1", "seg-2", "seg-3"],
        signal_states={"seg-1": "sig-1", "seg-2": "sig-2"},
    )
    _roundtrip(jct)


def test_station_roundtrip():
    sta = Station(
        id="sta-1",
        name="Central Station",
        platform_tracks=["trk-1", "trk-2"],
        rotation_deg=45.0,
        station_type=StationType.JUNCTION_STATION,
    )
    _roundtrip(sta)


def test_track_roundtrip():
    trk = Track(
        id="trk-1",
        segment_id="seg-1",
        geometry=[Point(x=0.0, y=0.0), Point(x=100.0, y=50.0)],
        directionality=TrackDirectionality.ONE_WAY_FORWARD,
        restricted_to_priority=1,
    )
    _roundtrip(trk)


def test_track_unrestricted_roundtrip():
    trk = Track(id="trk-2", segment_id="seg-2")
    rt = _roundtrip(trk)
    assert rt.restricted_to_priority is None


def test_schedule_entry_roundtrip():
    now = datetime(2025, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
    entry = ScheduleEntry(
        scheduled_arrival=now,
        scheduled_departure=datetime(2025, 6, 15, 8, 5, 0, tzinfo=timezone.utc),
        expected_arrival=now,
        actual_arrival=None,
    )
    _roundtrip(entry)


def test_train_roundtrip():
    now = datetime(2025, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
    train = Train(
        id="train-1",
        name="Rajdhani Express",
        color="#e63946",
        priority=PriorityTier.EXPRESS,
        num_carriages=18,
        max_speed=160.0,
        avg_speed=120.0,
        route=[
            RouteHop(node_id="sta-origin", segment_id=None),
            RouteHop(node_id="jct-1", segment_id="seg-1"),
            RouteHop(node_id="sta-dest", segment_id="seg-2"),
        ],
        schedule={
            "sta-origin": ScheduleEntry(
                scheduled_departure=now,
                expected_arrival=None,
            ),
            "sta-dest": ScheduleEntry(
                scheduled_arrival=datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            ),
        },
        driver_duty_status=DriverDutyStatus.NORMAL,
        current_block_id="blk-3",
        current_position_in_block=0.42,
    )
    _roundtrip(train)


def test_train_no_position_roundtrip():
    """A train that hasn't entered the network yet."""
    train = Train(
        id="train-2",
        name="Local Shuttle",
        color="#2a9d8f",
        num_carriages=4,
        max_speed=80.0,
        avg_speed=60.0,
    )
    rt = _roundtrip(train)
    assert rt.current_block_id is None
    assert rt.current_position_in_block == 0.0


# ---------------------------------------------------------------------------
# API / integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
