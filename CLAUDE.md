# CLAUDE.md — SIH 26028 Train ETA Simulator

This file is project-level context for Claude Code. Read it before making changes, and
keep it up to date as phases complete — treat stale sections here as a bug, not a footnote.

## What this system is

A train network ETA-prediction simulator with two user-facing modes:
- **Editor mode** — author a rail network (tracks, stations, junctions, signals) and
  trains (routes, schedules, priorities) visually, save it as a named layout.
- **Simulator mode** — replay/run that layout forward through simulated time: trains
  move, ETAs update live in response to conflicts/weather/delays, priority-based
  arbitration plays out at junctions.

## Tech stack

- **Backend**: Python, FastAPI, Pydantic v2, pytest — `/backend`
- **Frontend**: React + TypeScript, Vite, Konva.js (react-konva) — `/frontend`
- **Transport**: REST for save/load (`/api/simulations/*`), WebSocket for live simulation
  streaming (`/ws/simulate/{name}`)

## Domain vocabulary

- **Block** — smallest track unit (~1km, configurable per block, not hardcoded).
  Mutually exclusive occupancy: exactly one train or none. This is the core
  collision-prevention primitive.
- **Segment** — an ordered chain of Blocks between two Nodes. Non-conflicting — never
  branches internally.
- **Node** — either a **Junction** (≥2 ways in/out, a merge or crossing point) or a
  **Station**. A Junction has NO internal blocks — the whole junction is ONE atomic
  block for occupancy purposes, because trains can't safely split occupancy across a
  crossing point.
- **Track** — the geometric/visual representation of a Segment's path (straight or
  curved). A rendering concern layered on top of Block/Segment logic, NOT a separate
  source of truth. Track geometry is derived FROM segment structure; occupancy truth
  lives in Block only.
- **Train** — has a priority tier (1=express > 2=ordinary > 3=local) and a
  `driver_duty_status` that can override numeric priority entirely (over-duty, or
  journeys ≥12h, get max priority regardless of tier).
- **Schedule** — per-train, per-node: `scheduled` (fixed at authoring) vs. `expected`
  (continuously live-updated as the sim runs) vs. `actual` (fixed once it happens).

## The core control loop

This is what makes the engine event-propagation-driven, not a simple tick-and-redraw:

1. A train approaching a Junction sends that junction its ETA.
2. The junction arbitrates among all pending requests — override status > numeric
   priority > earliest ETA — and grants an actual pass time, which may be later than
   requested.
3. The train updates its OWN `expected_arrival` for that node, then immediately
   recomputes and pushes an updated ETA for the NEXT node in its route, triggering that
   node's arbitration too.
4. This chains forward continuously — an update at one junction ripples through the
   rest of the train's route and potentially forces re-arbitration for OTHER trains
   downstream.

The tick loop in `engine.py` still exists for animation/time-advancement, but
ETA-recalculation must be triggered by state changes (grants, delays, weather changes),
not just by clock ticks — otherwise ETAs go stale between ticks even though real-world
conditions already changed them.

## Backend layering (respect this — don't let logic leak across boundaries)

| File | Owns | Does NOT know about |
|---|---|---|
| `network.py` | Static graph: Blocks, Segments, Junctions, Stations, Tracks, pathfinding | Trains, time |
| `train.py` | Train model, schedule feasibility (distance/speed/time physics) | Other trains, arbitration |
| `arbitration.py` | Junction priority resolution + ETA-propagation chain | — (this is where network.py and train.py meet) |
| `conditions.py` | Weather/speed-restriction/maintenance modifiers to effective speed & pathfinding availability | — |
| `engine.py` | Sim clock, tick loop, event log. **The ONLY layer allowed to mutate Block occupancy.** | — |
| `main.py` | FastAPI routes: REST save/load, WebSocket streaming | — |

If you ever find yourself mutating `block.occupied_by` from inside `arbitration.py` or
`train.py`, stop — that belongs in `engine.py`'s tick/move path. If you need
`engine.py` state to test something in `network.py`, that's a sign a dependency has
leaked the wrong direction — flag it rather than working around it.

## Frontend rules

- `editor/` — authors the static network + trains + schedules, saves via REST. All
  validation the editor surfaces (route connectivity, schedule feasibility) calls the
  SAME backend validation functions used at save-time — never reimplement feasibility
  math on the frontend, the two will eventually disagree.
- `simulator/` — read-only rendering of the WebSocket stream. Curve-position
  interpolation is a rendering concern; which block a train is actually in is
  backend-authoritative, always. The frontend never decides occupancy, only displays it.
- `types.ts` mirrors the backend Pydantic models field-for-field (see reference below).
  If the two disagree, the backend is right by construction — fix the frontend.

## Weather speed-multiplier table (single lookup, used both departed and not-departed)

| Intensity | Multiplier |
|---|---|
| == 1.0 | 0 if already departed (halted in place); CANCELLED if not yet departed |
| ≥ 0.85 | 0.2x |
| ≥ 0.60 | 0.5x |
| ≥ 0.40 | 0.7x |
| ≥ 0.20 | 0.85x |
| ≥ 0.0 | 1.0x |

Speed restrictions and maintenance windows stack independently of weather (min of
whichever caps apply).

## Named constants (don't let these hide as magic numbers)

- `SIDING_DELAY_THRESHOLD_MINUTES = 5` — delay a higher-priority train must be projected
  to incur before a slower train ahead gets rerouted to a siding.
- Track snap-connect distance (editor) — name it, don't inline it.
- Arrival-popup ETA threshold (simulator) — configurable, defaults to ~2 sim-minutes.

## `models.py` field reference

### Enumerations
| Enum | Members |
|---|---|
| `SignalState` | `RED`, `YELLOW`, `GREEN` |
| `TrainPriority` | `EXPRESS=1`, `ORDINARY=2`, `LOCAL=3` |
| `DriverDutyStatus` | `NORMAL`, `OVER_DUTY` |
| `TrackDirectionality` | `BIDIRECTIONAL`, `UNIDIRECTIONAL` |

### Sub-models

**`GeometryPoint`** — `x: float`, `y: float`, `cx: float?` (Bézier control-point x),
`cy: float?` (Bézier control-point y)

**`MaintenanceWindow`** — `start_iso: str` (ISO-8601 UTC), `end_iso: str`

**`ScheduleEntry`** — `scheduled_arrival: str?`, `scheduled_departure: str?`,
`expected_arrival: str?`, `expected_departure: str?`, `actual_arrival: str?`,
`actual_departure: str?`

**`RouteHop`** — `node_id: str`, `segment_id: str` (segment traversed **to reach** this
node)

### Core domain models

**`Block`** — `id: str`, `segment_id: str`, `length_km: float` (>0),
`occupied_by: str?` (train_id), `weather_cell_id: str?`,
`speed_restriction: float?` (km/h cap), `maintenance_window: MaintenanceWindow?`

**`Segment`** — `id: str`, `start_node_id: str`, `end_node_id: str`,
`block_ids: list[str]` (ordered start→end)

**`Junction`** — `id: str`, `connected_segment_ids: list[str]`,
`signal_states: dict[str, SignalState]` (keyed by approach `segment_id`)

**`Station`** — `id: str`, `name: str`, `platform_tracks: list[str]` (track IDs),
`rotation_deg: float` (default 0.0), `station_type: str`
(`"terminus"` / `"through"` / `"junction"`)

**`Track`** — `id: str`, `segment_id: str`, `geometry: list[GeometryPoint]`,
`directionality: TrackDirectionality`, `restricted_to_priority: int?` (1/2/3)

**`Train`** — `id: str`, `name: str`, `color: str` (hex, e.g. `#E63946`),
`priority: TrainPriority`, `num_carriages: int` (≥1), `max_speed: float` (km/h),
`avg_speed: float` (km/h), `route: list[RouteHop]`,
`schedule: dict[str, ScheduleEntry]` (keyed by `node_id`),
`driver_duty_status: DriverDutyStatus`, `current_block_id: str?`,
`current_position_in_block: float` (0.0–1.0)

**`Schedule`** *(standalone API transport)* — `train_id: str`, `node_id: str`,
`entry: ScheduleEntry`

**`WeatherCell`** — `id: str`, `intensity: float` (0.0–1.0),
`affected_block_ids: list[str]`

## Build status

- [x] Phase 0 — Scaffolding
- [x] Phase 1 — Core network data model (`network.py`)
- [x] Phase 2 — Train & schedule model (`train.py`)
- [x] Phase 3 — Junction arbitration & signals (`arbitration.py`)
- [x] Phase 4 — Weather, speed restrictions, maintenance (`conditions.py`)
- [x] Phase 5 — Collision prevention, sidings, sim clock (`engine.py`)
- [ ] Phase 6 — Editor canvas & track geometry
- [ ] Phase 7 — Editor: trains, validation, save/load
- [ ] Phase 8 — Simulator: rendering & movement
- [ ] Phase 9 — Simulator: interaction & UI polish
- [ ] Phase 10 — Historical data, demo network, final integration

*(Update the checklist above as phases land — this is the fastest way for a fresh
Claude Code session to know where the project actually stands.)*

## Working conventions

- Read the relevant existing file(s) before editing — don't assume field names or
  signatures, confirm them against this doc and the actual code.
- Every phase's backend work ships with pytest coverage; every phase's frontend
  interaction work ships with at least a smoke test. Run the full suite, not just new
  tests, before calling a phase done.
- Don't reimplement backend physics/feasibility logic in the frontend — call the
  backend's validation endpoints/functions instead.
- If a task seems to require crossing one of the layering boundaries above (e.g.
  frontend computing occupancy, or `train.py` touching `Block.occupied_by` directly),
  stop and flag it rather than working around it silently.
