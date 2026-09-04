"""
FastAPI application entry-point.

Routes
------
GET  /api/health          → { "status": "ok" }
WS   /ws/sim              → echo endpoint (placeholder for Phase 5 streaming)

CORS is configured for the Vite dev-server origin (localhost:5173).
In production, replace allow_origins with your actual domain.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

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


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@app.get("/api/health", tags=["meta"])
async def health_check() -> dict[str, str]:
    """Liveness probe — confirms the backend process is up."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# WebSocket endpoints
# ---------------------------------------------------------------------------


@app.websocket("/ws/sim")
async def ws_sim(websocket: WebSocket) -> None:
    """
    Placeholder WebSocket endpoint.

    Phase 0: simple echo so the frontend can verify connectivity.
    Phase 5: replaced with one-way engine tick snapshot broadcasting.
    """
    await websocket.accept()
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_text(f"echo: {data}")
    except WebSocketDisconnect:
        pass
