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
- **Track** — the geometric/visual representation of a Segment's path, as an ordered
  list of `Point`s (plain x/y — no bezier control points; curves are polylines/splines
  through multiple points, not single-control-point beziers). A rendering concern
  layered on top of Block/Segment logic, NOT a separate source of truth. Track geometry
  is derived FROM segment structure; occupancy truth lives in Block only.
- **Signal** — a per-Block record (`SignalState` model), not just a display-derived
  value. Optionally governed by a Junction (`controlled_by_junction_id`), but can exist
  autonomously on a plain block. `engine.py`'s `_persist_signal_aspect()` writes the live
  aspect to the actual `SignalState` record on every advance/hold/clear — confirmed
  working as of the post-Phase-5 signal-persistence fix (see Build status). If a signal
  lookup ever fails (unregistered junction, missing approach signal, stale `signal_id`),
  it logs a `WARNING` and no-ops rather than crashing the tick loop — check logs, not
  just test failures, if signal data ever looks wrong downstream.
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
| `arbitration.py` | Junction priority resolution + ETA-propagation chain. Grant/deny decisions ultimately drive signal aspect (persisted by `engine.py`, not written here directly). | — (this is where network.py and train.py meet) |
| `conditions.py` | Weather/speed-restriction/maintenance modifiers to effective speed & pathfinding availability | — |
| `engine.py` | Sim clock, tick loop, event log, **and `_persist_signal_aspect()`** which writes the live signal aspect to the real `SignalState` record via `network.set_signal_state()` at three points (advance, hold, clear). The ONLY layer allowed to mutate Block occupancy. | — |
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

*(Verified against the actual repo after Phase 5 — this superseded an earlier draft
version of this section that used different enum/field names. If you see references to
`TrainPriority`, `GeometryPoint`, or `SignalState`-as-enum anywhere else, e.g. in old
phase prompts, they refer to the pre-verification names below and need updating.)*

### Enumerations
| Enum | Members |
|---|---|
| `StationType` | `terminus`, `through`, `junction_station` |
| `TrackDirectionality` | `bidirectional`, `one_way_forward`, `one_way_reverse` |
| `DriverDutyStatus` | `normal`, `over_duty` |
| `SignalStateValue` | `green`, `red`, `caution` |
| `PriorityTier` | `EXPRESS=1`, `ORDINARY=2`, `LOCAL=3` |

### Sub-models

**`Point`** — `x: float`, `y: float`. **No Bézier control-point fields** (`cx`/`cy`
were dropped) — curve geometry is an ordered list of plain points, not a
single-control-point quadratic bezier. Phase 6's editor prompt (drag a bezier
control-point handle) needs to be revised to a multi-point spline/polyline approach, or
`cx`/`cy` need to be reintroduced — decide which before starting Phase 6.

**`MaintenanceWindow`** — `start_iso: str` (ISO-8601), `end_iso: str` (ISO-8601)

**`ScheduleEntry`** — `scheduled_arrival: datetime?`, `scheduled_departure: datetime?`,
`expected_arrival: datetime?` (live-updated by arbitration), `actual_arrival: datetime?`
(set once, then frozen). **No `expected_departure` or `actual_departure`** — departure
timing beyond `scheduled_departure` is not currently tracked; confirm that's intentional
before Phase 5/9 code assumes it exists.

**`RouteHop`** — `node_id: str`, `segment_id: str?` (segment used to reach this hop;
**null for the origin hop**, since there's nothing to traverse to reach your own start).

### Core domain models

**`WeatherCell`** — `id: str`, `intensity: float` [0–1], `affected_block_ids: list[str]`

**`SignalState`** *(standalone model, not a bare enum value on Junction — confirmed
actively written, not dead code)* —
`id: str`, `block_id: str` (block this signal guards), `state: SignalStateValue`
(default `GREEN`), `controlled_by_junction_id: str?` (null = autonomous signal not
governed by junction arbitration). `network.py`'s `_ensure_junction_signal()` /
`register_junction_signals()` create and wire these records per approach; `engine.py`'s
`_persist_signal_aspect()` keeps `.state` in sync with the live aspect on every
advance/hold/clear. `to_dict()` now emits real, non-empty `"signals"` data.

**`Block`** — `id: str`, `segment_id: str`, `length_km: float` (>0),
`occupied_by: str?` (train_id), `weather_cell_id: str?`,
`speed_restriction: float?` (km/h cap), `maintenance_window: MaintenanceWindow?`

**`Segment`** — `id: str`, `start_node_id: str`, `end_node_id: str`,
`ordered_block_ids: list[str]` (index 0 = adjacent to start node)

**`Junction`** — `id: str`, `connected_segment_ids: list[str]`,
`signal_states: dict[str, str]` — **keys are approach `segment_id`, values are
`SignalState.id`** (a foreign key, not an inline enum) — look up the actual state via
the `SignalState` model above.

**`Station`** — `id: str`, `name: str`, `platform_tracks: list[str]` (track IDs),
`rotation_deg: float` (default 0.0), `station_type: StationType` (default `through`)

**`Track`** — `id: str`, `segment_id: str`, `geometry: list[Point]`,
`directionality: TrackDirectionality` (default `bidirectional`),
`restricted_to_priority: int?` [1–3] — read as "minimum priority tier allowed," null =
unrestricted

**`Train`** — `id: str`, `name: str`, `color: str` (CSS hex, e.g. `#e63946`),
`priority: PriorityTier` (default `ORDINARY`), `num_carriages: int` (≥1),
`max_speed: float` (>0, km/h), `avg_speed: float` (>0, km/h),
`route: list[RouteHop]`, `schedule: dict[str, ScheduleEntry]` (keyed by `node_id`),
`driver_duty_status: DriverDutyStatus` (default `normal`), `current_block_id: str?`
(null if not yet entered or journey complete), `current_position_in_block: float` [0–1]

**Not confirmed present** — the standalone `Schedule` transport model
(`train_id`/`node_id`/`entry`) referenced in an earlier draft of this doc did not appear
in this listing. Confirm whether it still exists (may just be outside the viewed line
range) or was removed in favor of reading `Train.schedule` directly.

## Build status

- [x] Phase 0 — Scaffolding
- [x] Phase 1 — Core network data model (`network.py`)
- [x] Phase 2 — Train & schedule model (`train.py`)
- [x] Phase 3 — Junction arbitration & signals (`arbitration.py`)
- [x] Phase 4 — Weather, speed restrictions, maintenance (`conditions.py`)
- [x] Phase 5 — Collision prevention, sidings, sim clock (`engine.py`)
- [x] **Post-Phase-5 fix** — `SignalState` records were being defined but never
  persisted (`_signals` always empty, `resolve()` never wrote to it). Fixed:
  `network.py` now wires a real `SignalState` per approach via
  `_ensure_junction_signal()`/`register_junction_signals()`; `engine.py`'s
  `_persist_signal_aspect()` writes the live aspect on every advance/hold/clear, logging
  a `WARNING` (not raising) on any lookup failure. Full suite confirmed green after the fix.
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
