"""
test_simulations.py — Tests for POST/GET /api/simulations/{name}.

Tests cover:
1. Infeasible schedule (avg_speed > max_speed) → 400 with violation detail.
2. Valid layout with null origin segment_id round-trips correctly:
   - Returns 200.
   - Null segment_id is preserved (not coerced to "" or rejected).
   - Computed schedule has entries for BOTH nodes (build_schedule ran).
   - Saved JSON file exists on disk.
"""

from __future__ import annotations

import json
import pathlib
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.simulations_router import SIMULATIONS_DIR


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers — minimal but structurally valid layouts
# ---------------------------------------------------------------------------

def _two_station_layout(
    *,
    train_name: str = "T1",
    train_id: str = "trn-test-01",
    station_a_id: str = "sta-A",
    station_b_id: str = "sta-B",
    segment_id: str = "seg-AB",
    track_id: str = "trk-AB",
    max_speed: float = 120.0,
    avg_speed: float = 80.0,
    origin_departure_iso: str = "2026-09-07T06:00:00Z",
) -> dict:
    """
    A minimal two-station, one-segment layout.

    The segment has one block of 5 km (travel time at 80 km/h ≈ 3.75 min,
    well within any reasonable schedule window).
    """
    return {
        "tracks": [
            {
                "id": track_id,
                "segment_id": segment_id,
                "geometry": [{"x": 0, "y": 0}, {"x": 500, "y": 0}],
                "directionality": "bidirectional",
                "restricted_to_priority": None,
            }
        ],
        "segments": [
            {
                "id": segment_id,
                "start_node_id": station_a_id,
                "end_node_id": station_b_id,
                "ordered_block_ids": [f"blk-{segment_id}-0"],
            }
        ],
        "stations": [
            {"id": station_a_id, "name": "Station A", "platform_tracks": [], "rotation_deg": 0, "station_type": "terminus"},
            {"id": station_b_id, "name": "Station B", "platform_tracks": [], "rotation_deg": 0, "station_type": "terminus"},
        ],
        "junctions": [],
        "signals": [],
        "trains": [
            {
                "id": train_id,
                "name": train_name,
                "color": "#3b82f6",
                "priority": 2,
                "num_carriages": 4,
                "max_speed": max_speed,
                "avg_speed": avg_speed,
                "route": [
                    {"node_id": station_a_id, "segment_id": None},   # origin — null segment_id
                    {"node_id": station_b_id, "segment_id": segment_id},
                ],
                "schedule": {},
                "driver_duty_status": "normal",
                "current_block_id": None,
                "current_position_in_block": 0.0,
            }
        ],
        "authoring_hints": [
            {
                "train_id": train_id,
                "origin_departure_iso": origin_departure_iso,
                "dwell_minutes": {},
            }
        ],
        "segment_overrides": [],
        "station_positions": {station_a_id: {"x": 100, "y": 200}, station_b_id: {"x": 600, "y": 200}},
        "junction_positions": {},
        "signal_positions": {},
        "train_positions": {train_id: {"x": 100, "y": 200}},
    }


# ---------------------------------------------------------------------------
# Test 1: Infeasible schedule is rejected
# ---------------------------------------------------------------------------

def test_save_rejects_infeasible_schedule(client: TestClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    avg_speed > max_speed with a 50-block (50 km) segment triggers ScheduleError:
      baseline window = 50/200 h + 5 min = 20 min
      min travel at max_speed=50 = 50/50 h = 60 min
      20 < 60 → infeasible → HTTP 400.
    """
    monkeypatch.setattr("app.simulations_router.SIMULATIONS_DIR", tmp_path)

    layout = _two_station_layout(
        train_name="Impossible Train",
        max_speed=50.0,
        avg_speed=200.0,
    )
    # 50 blocks × 1 km (default) = 50 km total — makes the infeasibility obvious.
    layout["segments"][0]["ordered_block_ids"] = [f"blk-infeasible-{i}" for i in range(50)]

    response = client.post("/api/simulations/infeasible-test", json=layout)

    assert response.status_code == 400, f"Expected 400, got {response.status_code}: {response.text}"
    body = response.json()
    assert "errors" in body["detail"], f"Response body has no 'errors' key: {body}"
    errors = body["detail"]["errors"]
    assert len(errors) >= 1, "Expected at least one error in the list"
    combined = " ".join(errors).lower()
    assert any(
        kw in combined for kw in ("infeasible", "avg_speed", "max_speed", "min", "window", "hop")
    ), f"Error does not describe infeasibility: {errors}"
    assert not (tmp_path / "infeasible-test.json").exists()


# ---------------------------------------------------------------------------
# Test 2: Valid layout with null origin segment_id round-trips correctly
# ---------------------------------------------------------------------------

def test_save_roundtrip_with_null_origin_segment(client: TestClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A layout where the first RouteHop has segment_id=null (as required by the
    RouteHop contract for origin nodes).  The endpoint must:
      - Accept it (200).
      - Fill in the full schedule for BOTH nodes.
      - Preserve segment_id=null in the saved file (not coerced to "").
      - Return the same null on GET.
    """
    monkeypatch.setattr("app.simulations_router.SIMULATIONS_DIR", tmp_path)

    layout = _two_station_layout(
        train_name="Express A",
        max_speed=120.0,
        avg_speed=80.0,
    )
    name = "roundtrip-test"

    # POST — save.
    save_response = client.post(f"/api/simulations/{name}", json=layout)
    assert save_response.status_code == 200, (
        f"Expected 200, got {save_response.status_code}: {save_response.text}"
    )
    assert save_response.json()["ok"] is True

    # File must exist on disk.
    saved_file = tmp_path / f"{name}.json"
    assert saved_file.exists(), "Saved JSON file was not created"

    # Inspect raw JSON — null must not have been coerced.
    raw = json.loads(saved_file.read_text(encoding="utf-8"))
    origin_hop = raw["trains"][0]["route"][0]
    assert origin_hop["segment_id"] is None, (
        f"segment_id was coerced: expected null, got {origin_hop['segment_id']!r}"
    )

    # Schedule must have been filled for both stations.
    schedule = raw["trains"][0]["schedule"]
    assert "sta-A" in schedule, "Schedule missing origin node sta-A"
    assert "sta-B" in schedule, "Schedule missing destination node sta-B"

    # Origin node has a scheduled_departure (not null).
    assert schedule["sta-A"]["scheduled_departure"] is not None, (
        "Origin node scheduled_departure should not be null"
    )

    # Destination node has a scheduled_arrival (filled by build_schedule).
    assert schedule["sta-B"]["scheduled_arrival"] is not None, (
        "Destination node scheduled_arrival should not be null after build_schedule()"
    )

    # GET — load.
    load_response = client.get(f"/api/simulations/{name}")
    assert load_response.status_code == 200, (
        f"Expected 200 on GET, got {load_response.status_code}: {load_response.text}"
    )
    loaded = load_response.json()
    reloaded_origin = loaded["trains"][0]["route"][0]
    assert reloaded_origin["segment_id"] is None, (
        f"GET returned non-null segment_id for origin hop: {reloaded_origin['segment_id']!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: GET returns 404 for unknown name
# ---------------------------------------------------------------------------

def test_load_unknown_name_returns_404(client: TestClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.simulations_router.SIMULATIONS_DIR", tmp_path)
    response = client.get("/api/simulations/does-not-exist")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Test 4: Dwell minutes inflate scheduled_departure correctly
# ---------------------------------------------------------------------------

def test_dwell_minutes_applied(client: TestClient, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    When dwell_minutes = {"sta-B": 10}, the scheduled_departure at sta-B
    must be >= scheduled_arrival + 10 minutes.
    """
    monkeypatch.setattr("app.simulations_router.SIMULATIONS_DIR", tmp_path)

    layout = _two_station_layout()
    layout["authoring_hints"][0]["dwell_minutes"] = {"sta-B": 10}
    name = "dwell-test"

    resp = client.post(f"/api/simulations/{name}", json=layout)
    assert resp.status_code == 200, resp.text

    raw = json.loads((tmp_path / f"{name}.json").read_text())
    sta_b = raw["trains"][0]["schedule"]["sta-B"]
    from datetime import datetime as DT
    arr = DT.fromisoformat(sta_b["scheduled_arrival"].replace("Z", "+00:00"))
    dep = DT.fromisoformat(sta_b["scheduled_departure"].replace("Z", "+00:00"))
    dwell_actual_min = (dep - arr).total_seconds() / 60.0
    assert dwell_actual_min >= 10.0, (
        f"Dwell not applied: scheduled_departure - scheduled_arrival = {dwell_actual_min:.1f} min (expected ≥ 10)"
    )
