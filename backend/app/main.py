"""
FastAPI application entry-point.

Routes
------
GET  /api/health                → { "status": "ok" }
POST /api/simulations/{name}    → save layout (simulations_router)
GET  /api/simulations/{name}    → load layout (simulations_router)
WS   /ws/simulate/{name}        → live simulation streaming

CORS is configured for the Vite dev-server origin (localhost:5173).
In production, replace allow_origins with your actual domain.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from app.simulations_router import router as simulations_router, LayoutPayload, SIMULATIONS_DIR
from app.network import (
    NetworkGraph, BlockRuntime, SegmentRuntime, TrackRuntime,
    StationRuntime, JunctionRuntime,
)
from app.models import Block, ScheduleEntry
from app.train import TrainRuntime
from app.engine import SimulationEngine

logger = logging.getLogger(__name__)

app = FastAPI(
    title="TrainNet ETA Simulator",
    description="Backend API for the train network ETA-prediction simulator.",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(simulations_router)

# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/api/health", tags=["meta"])
async def health_check() -> dict[str, str]:
    """Liveness probe — confirms the backend process is up."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# WebSocket — /ws/simulate/{name}
# ---------------------------------------------------------------------------

def _parse_dt(v) -> Optional[datetime]:
    """Parse an ISO string or pass-through a datetime; return None for None."""
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v).replace("Z", "+00:00"))


def _iso(v) -> Optional[str]:
    """Serialise a datetime to ISO-8601 string, or return None."""
    if v is None:
        return None
    return v.isoformat() if hasattr(v, "isoformat") else str(v)


def _build_engine_from_payload(payload: LayoutPayload) -> SimulationEngine:
    """
    Reconstruct a NetworkGraph + SimulationEngine from a saved LayoutPayload.

    Steps:
      1. Build NetworkGraph (mirrors simulations_router._build_network).
      2. Register junction signals.
      3. Create SimulationEngine, register all trains.
    """
    graph = NetworkGraph()

    # Nodes
    for sta in payload.stations:
        graph._stations[sta.id] = StationRuntime(sta)
        graph._register_node(sta.id)
    for jct in payload.junctions:
        graph._junctions[jct.id] = JunctionRuntime(jct)
        graph._register_node(jct.id)

    # Signals
    for sig in payload.signals:
        graph._signals[sig.id] = sig

    # Override map
    override_map = {o.segment_id: o for o in payload.segment_overrides}

    # Segments + blocks
    for seg in payload.segments:
        override = override_map.get(seg.id)
        blocks: list[BlockRuntime] = []
        block_ids = seg.ordered_block_ids if seg.ordered_block_ids else [f"blk-{seg.id}-0"]
        for blk_id in block_ids:
            blk_model = Block(
                id=blk_id,
                segment_id=seg.id,
                length_km=1.0,
                speed_restriction=override.speed_restriction if override else None,
                maintenance_window=override.maintenance_window if override else None,
            )
            blk_rt = BlockRuntime(blk_model)
            blocks.append(blk_rt)
            graph._blocks[blk_id] = blk_rt
        seg_rt = SegmentRuntime(seg, blocks)
        graph._segments[seg.id] = seg_rt
        if seg.start_node_id and seg.end_node_id:
            graph._link_nodes(seg.start_node_id, seg.end_node_id, seg.id)

    # Tracks (visual layer)
    for trk in payload.tracks:
        graph._tracks[trk.id] = TrackRuntime(trk)

    # Auto-register junction signals.
    try:
        graph.register_junction_signals()
    except Exception as exc:
        logger.warning("register_junction_signals failed: %s", exc)

    # Find earliest origin departure across all trains → sim start clock.
    start_time: Optional[datetime] = None
    for train_model in payload.trains:
        if not train_model.route:
            continue
        entry = train_model.schedule.get(train_model.route[0].node_id)
        if entry and entry.scheduled_departure:
            dt = _parse_dt(entry.scheduled_departure)
            if dt and (start_time is None or dt < start_time):
                start_time = dt
    if start_time is None:
        start_time = datetime.now(tz=timezone.utc)

    engine = SimulationEngine(network=graph, start_time=start_time)

    # Register trains.
    for train_model in payload.trains:
        if not train_model.route:
            continue
        train_rt = TrainRuntime(train_model)
        # Ensure all schedule datetimes are actual datetime objects (not ISO strings).
        fixed_schedule: dict = {}
        for node_id, entry in train_model.schedule.items():
            fixed_schedule[node_id] = ScheduleEntry(
                scheduled_arrival=_parse_dt(entry.scheduled_arrival),
                scheduled_departure=_parse_dt(entry.scheduled_departure),
                expected_arrival=_parse_dt(entry.expected_arrival),
                actual_arrival=_parse_dt(entry.actual_arrival),
            )
        train_rt._model.schedule = fixed_schedule
        try:
            engine.add_train(train_rt)
        except Exception as exc:
            logger.warning("Could not add train %s: %s", train_model.id, exc)

    return engine


def _build_tick_snapshot(engine: SimulationEngine) -> dict:
    """
    Build a JSON-serialisable snapshot of the current engine state.

    Snapshot shape
    --------------
    {
      "sim_clock": "<ISO>",
      "playing": bool,
      "speed_multiplier": float,
      "trains": [{ id, name, color, num_carriages, priority, status,
                   current_block_id, current_position_in_block, route,
                   schedule: {node_id: {scheduled_arrival, scheduled_departure,
                                        expected_arrival, actual_arrival}} }],
      "blocks": [{ id, segment_id, length_km, occupied_by,
                   speed_restriction, maintenance_window, weather_cell_id }],
      "signals": [{ id, block_id, state, controlled_by_junction_id }],
      "weather_cells": [{ id, intensity, affected_block_ids }],
    }
    """
    graph = engine.network

    # ── Trains ─────────────────────────────────────────────────────────────
    trains_out = []
    for train_id, ts in engine._train_states.items():
        tr = ts.train          # TrainRuntime (exposes: id, name, priority, route, schedule, etc.)
        m  = tr._model         # Train Pydantic model (exposes: color, num_carriages, all fields)
        schedule_out: dict[str, dict] = {}
        for node_id, entry in tr.schedule.items():
            schedule_out[node_id] = {
                "scheduled_arrival":   _iso(entry.scheduled_arrival),
                "scheduled_departure": _iso(entry.scheduled_departure),
                "expected_arrival":    _iso(entry.expected_arrival),
                "actual_arrival":      _iso(entry.actual_arrival),
            }
        trains_out.append({
            "id":                        train_id,
            "name":                      tr.name,
            "color":                     m.color,
            "num_carriages":             m.num_carriages,
            "priority":                  int(tr.priority),
            "status":                    ts.status.value,
            "current_block_id":          ts.current_block_id,
            "current_position_in_block": ts.position_in_block,
            "route":                     [{"node_id": h.node_id, "segment_id": h.segment_id} for h in tr.route],
            "schedule":                  schedule_out,
        })

    # ── Blocks ──────────────────────────────────────────────────────────────
    blocks_out = []
    for blk in graph.all_blocks:
        mw = blk.maintenance_window
        blocks_out.append({
            "id": blk.id,
            "segment_id": blk.segment_id,
            "length_km": blk.length_km,
            "occupied_by": blk.occupied_by,
            "speed_restriction": blk.speed_restriction,
            "maintenance_window": {"start_iso": mw.start_iso, "end_iso": mw.end_iso} if mw else None,
            "weather_cell_id": blk.weather_cell_id,
        })

    # ── Signals (live three-state: green / caution / red) ──────────────────
    signals_out = []
    for sig_id, sig in graph._signals.items():
        state_val = sig.state if isinstance(sig.state, str) else sig.state.value
        signals_out.append({
            "id": sig_id,
            "block_id": getattr(sig, "block_id", ""),
            "state": state_val,
            "controlled_by_junction_id": getattr(sig, "controlled_by_junction_id", None),
        })

    # ── Weather cells ───────────────────────────────────────────────────────
    weather_out = []
    for wc in graph._weather_cells.values():
        weather_out.append({
            "id": wc.id,
            "intensity": wc.intensity,
            "affected_block_ids": list(wc.affected_block_ids) if hasattr(wc, "affected_block_ids") else [],
        })

    return {
        "sim_clock":        engine.sim_clock.isoformat(),
        "playing":          engine._playing,
        "speed_multiplier": engine.speed_multiplier,
        "trains":           trains_out,
        "blocks":           blocks_out,
        "signals":          signals_out,
        "weather_cells":    weather_out,
        "event_log":        engine.event_log[-30:],   # last 30 events for SidePanel history
    }


def _layout_geometry(payload: LayoutPayload) -> dict:
    """Extract static canvas geometry for the initial snapshot."""
    return {
        "tracks": [
            {
                "id": t.id,
                "segment_id": t.segment_id,
                "geometry": [{"x": p.x, "y": p.y} for p in t.geometry],
                "directionality": t.directionality.value if hasattr(t.directionality, "value") else t.directionality,
            }
            for t in payload.tracks
        ],
        "segments": [
            {"id": s.id, "start_node_id": s.start_node_id,
             "end_node_id": s.end_node_id, "ordered_block_ids": s.ordered_block_ids}
            for s in payload.segments
        ],
        "stations": [
            {"id": s.id, "name": s.name, "platform_tracks": s.platform_tracks,
             "rotation_deg": s.rotation_deg, "station_type": s.station_type}
            for s in payload.stations
        ],
        "junctions": [
            {"id": j.id, "connected_segment_ids": j.connected_segment_ids}
            for j in payload.junctions
        ],
        "station_positions": payload.station_positions,
        "junction_positions": payload.junction_positions,
        "signal_positions": payload.signal_positions,
        "train_positions": payload.train_positions,
    }


@app.websocket("/ws/simulate/{name}")
async def ws_simulate(websocket: WebSocket, name: str) -> None:
    """
    Live simulation streaming for a saved layout.

    On connect:
      1. Loads simulations/{name}.json.
      2. Builds a SimulationEngine.
      3. Sends an initial snapshot containing both live state AND static
         layout geometry (tracks, stations, junctions, positions).
      4. Starts streaming tick snapshots every tick_interval_ms wall-clock ms.

    Client messages (JSON):
      { "action": "play" }
      { "action": "pause" }
      { "action": "set_speed", "value": 60.0 }     — speed_multiplier (sim seconds per real second)
      { "action": "tick_interval_ms", "value": 500 } — delivery rate
    """
    await websocket.accept()

    src = SIMULATIONS_DIR / f"{name}.json"
    if not src.exists():
        await websocket.send_text(json.dumps({"error": f"Layout {name!r} not found."}))
        await websocket.close(code=4404)
        return

    try:
        payload = LayoutPayload(**json.loads(src.read_text(encoding="utf-8")))
    except Exception as exc:
        await websocket.send_text(json.dumps({"error": f"Failed to parse layout: {exc}"}))
        await websocket.close(code=4500)
        return

    try:
        engine = _build_engine_from_payload(payload)
    except Exception as exc:
        await websocket.send_text(json.dumps({"error": f"Failed to build engine: {exc}"}))
        await websocket.close(code=4500)
        return

    tick_interval_ms: float = 500.0       # wall-clock ms between snapshots
    sim_seconds_per_tick: float = 30.0    # simulated seconds advanced per snapshot

    # Send initial snapshot with geometry.
    initial = _build_tick_snapshot(engine)
    initial["layout"] = _layout_geometry(payload)
    await websocket.send_text(json.dumps(initial))

    engine.play()

    async def receive_loop() -> None:
        nonlocal tick_interval_ms, sim_seconds_per_tick
        try:
            while True:
                text = await websocket.receive_text()
                try:
                    msg = json.loads(text)
                    action = msg.get("action", "")
                    if action == "play":
                        engine.play()
                    elif action == "pause":
                        engine.pause()
                    elif action == "set_speed":
                        val = float(msg.get("value", 1.0))
                        engine.set_speed_multiplier(val)
                        sim_seconds_per_tick = max(1.0, val * 30.0)
                    elif action == "tick_interval_ms":
                        tick_interval_ms = max(100.0, float(msg.get("value", 500.0)))
                except Exception as exc:
                    logger.warning("WS message parse error: %s", exc)
        except (WebSocketDisconnect, Exception):
            pass

    async def tick_loop() -> None:
        try:
            while True:
                await asyncio.sleep(tick_interval_ms / 1000.0)
                engine.tick(sim_seconds_per_tick)
                snapshot = _build_tick_snapshot(engine)
                await websocket.send_text(json.dumps(snapshot))
        except (WebSocketDisconnect, Exception) as exc:
            if not isinstance(exc, WebSocketDisconnect):
                logger.warning("Tick loop error: %s", exc)

    recv_task = asyncio.create_task(receive_loop())
    tick_task = asyncio.create_task(tick_loop())
    done, pending = await asyncio.wait(
        [recv_task, tick_task], return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
