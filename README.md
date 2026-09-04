# TrainNet ETA Simulator

A train network ETA-prediction simulator with two user-facing modes:

- **Editor mode** — visually author a rail network (tracks, stations, junctions,
  signals) and trains (routes, schedules, priorities), save as a named layout.
- **Simulator mode** — replay a saved layout with trains moving in simulated time,
  ETAs updating live in response to conflicts, weather, and priority arbitration.

---

## Repository structure

```
SIH/
├── backend/           Python / FastAPI / Pydantic v2 (source of truth)
│   ├── app/
│   │   ├── main.py    FastAPI app, REST + WebSocket endpoints
│   │   └── models.py  Pydantic domain models (contracts)
│   ├── tests/
│   │   └── test_models.py  Round-trip serialisation + API tests
│   ├── simulations/   Saved network+schedule JSON files (gitkeep)
│   └── pyproject.toml
├── frontend/          React / TypeScript / Vite / Konva.js
│   ├── src/
│   │   ├── App.tsx    Main page: Konva canvas + health indicator
│   │   ├── App.css    Dark-mode design system
│   │   ├── api.ts     HTTP helpers (proxied through Vite)
│   │   └── types.ts   TypeScript interfaces (mirrors models.py)
│   └── vite.config.ts Dev proxy: /api → :8000, /ws → :8000
└── README.md
```

---

## Prerequisites

| Tool | Minimum version | Purpose |
|---|---|---|
| **Python** | 3.9 | Backend runtime |
| **uv** | 0.12 | Python package manager (replaces pip/venv) |
| **Node.js** | 18 | Frontend build tooling |
| **npm** | 9 | Frontend package manager |

### Install `uv` (if not already installed)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env   # or restart your shell
```

---

## Running locally (development)

### 1. Backend — FastAPI (port 8000)

```bash
cd backend
uv run uvicorn app.main:app --reload --port 8000
```

API docs auto-generated at http://localhost:8000/docs

### 2. Frontend — Vite dev server (port 5173)

```bash
cd frontend
npm run dev
```

Open http://localhost:5173 — the **Backend: connected** indicator in the top-right
confirms the two halves are talking.

---

## How the two halves communicate

```
Browser (localhost:5173)
        │
        │  GET /api/*          ← Vite proxy →   FastAPI (localhost:8000)
        │  WS  /ws/*           ← Vite proxy →   FastAPI (localhost:8000)
```

In development, the Vite dev server proxies all `/api` and `/ws` paths to
`localhost:8000`, so the browser never sees a cross-origin request.
In production, deploy both behind the same reverse proxy (nginx / Caddy) on
the same origin to achieve the same effect.

---

## Running tests

```bash
cd backend
uv run pytest tests/ -v
```

Current test suite:
- Round-trip JSON serialisation for every domain model
- `GET /api/health` returns `{"status": "ok"}`

---

## Saved simulations

Named layout files are stored in `backend/simulations/` as JSON.
The directory ships with a `.gitkeep` to preserve it in git.
The REST endpoint `POST /api/simulations/{name}` (Phase 1) will write here.

---

## Package manager choice: `uv`

The backend uses [`uv`](https://github.com/astral-sh/uv) instead of
`pip` + `venv` because:
- Single binary, no global install of pip/virtualenv needed.
- `uv.lock` gives fully reproducible installs.
- `uv run <cmd>` auto-activates the venv — no `source .venv/bin/activate` needed.

The lockfile is committed (`backend/uv.lock`) so CI and other contributors get
identical dependency versions.
