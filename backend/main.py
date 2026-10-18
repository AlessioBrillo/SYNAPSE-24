"""SYNAPSE-24 Data Plane API (FastAPI).

The service logic lives in ``backend/service.py`` (importable without fastapi).
This module only assembles the FastAPI app; fastapi/uvicorn are imported
lazily so ``import backend.main`` works in environments that only have the
project's own dependencies installed.

Run:
    uvicorn backend.main:app   (fastapi/uvicorn required)
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from backend.service import DataPlaneService, JobRunRequest, JobRunResponse  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

connected_clients: set[Any] = set()

_FASTAPI_AVAILABLE = True
try:  # fastapi/uvicorn are runtime deps, not dev deps
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect  # noqa: F401
    from fastapi.middleware.cors import CORSMiddleware  # noqa: F401
except ImportError:  # pragma: no cover - dev environment without fastapi
    FastAPI = None  # type: ignore[assignment]
    WebSocket = None  # type: ignore[assignment]
    WebSocketDisconnect = Exception  # type: ignore[assignment]
    CORSMiddleware = None  # type: ignore[assignment]
    _FASTAPI_AVAILABLE = False

app: Any = None


def _sessions_dir() -> Path:
    import os

    return Path(os.environ.get("SYNAPSE_SESSIONS_DIR", "data/sessions"))


def create_app() -> Any:
    """Build the FastAPI app. Requires fastapi."""
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError("fastapi is not installed; install backend/requirements.txt")

    from fastapi import FastAPI as _FA
    from fastapi import WebSocket as _WS
    from fastapi import WebSocketDisconnect as _WSD
    from fastapi.middleware.cors import CORSMiddleware as _CORS

    connected_clients_local: set[_WS] = set()

    @asynccontextmanager
    async def lifespan(application: _FA):
        logger.info("SYNAPSE-24 Data Plane API started")
        yield
        logger.info("SYNAPSE-24 Data Plane API shut down")

    application = _FA(
        title="SYNAPSE-24 Data Plane API",
        description="Read-side interfaces for Averyn: sessions, reports, worker jobs.",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        _CORS,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ------------------------------------------------------------------
    # Legacy dashboard endpoints (kept functional)
    # ------------------------------------------------------------------
    @application.get("/api/health")
    async def health_check():
        return {"status": "ok", "service": "synapse24-data-plane"}

    @application.get("/api/status")
    async def get_status():
        svc = DataPlaneService(_sessions_dir())
        cat = svc.catalog()
        n = len(cat.sessions) if cat else 0
        return {
            "current_tier": "T0",
            "n_sessions": n,
            "sessions_dir": str(svc.sessions_dir),
        }

    @application.get("/api/sync")
    async def get_sync_status():
        return {
            "pods": {},
            "note": "clock sync requires hardware; not available in software-only mode",
        }

    @application.get("/api/quality/{pod_id}")
    async def get_signal_quality(pod_id: str):
        svc = DataPlaneService(_sessions_dir())
        rec = svc.session(pod_id)
        if rec is None:
            return {"error": "session not found"}
        return {
            "session_id": rec["session_id"],
            "format": rec["format"],
            "xdf_valid": rec["xdf_valid"],
            "fit_valid": rec["fit_valid"],
            "n_samples_total": rec["n_samples_total"],
        }

    @application.get("/api/live/{pod_id}")
    async def get_live_data(pod_id: str, duration_s: float = 10.0):
        return {
            "error": "live streaming requires hardware / LSL relay; not available in software-only mode",
            "pod_id": pod_id,
        }

    @application.post("/api/control/tier")
    async def control_tier():
        return {"error": "tier control requires hardware; not available in software-only mode"}

    @application.post("/api/control/record")
    async def control_recording():
        return {"error": "recording requires hardware; not available in software-only mode"}

    @application.get("/api/config")
    async def get_config():
        import yaml

        hw = Path("config/hardware.yaml")
        if hw.exists():
            with hw.open(encoding="utf-8") as fh:
                return yaml.safe_load(fh)
        return {"error": "config/hardware.yaml not found"}

    # ------------------------------------------------------------------
    # Data plane (Averyn-facing) endpoints
    # ------------------------------------------------------------------
    @application.get("/api/v1/sessions")
    async def list_sessions():
        svc = DataPlaneService(_sessions_dir())
        cat = svc.catalog()
        if cat is None:
            return {"sessions": [], "count": 0}
        sessions = [
            {
                "session_id": rec.session_id,
                "device_serial": rec.device_serial,
                "format": rec.format,
                "started_at": rec.started_at,
                "ended_at": rec.ended_at,
                "duration_s": rec.duration_s,
                "n_samples_total": rec.n_samples_total,
                "n_streams": len(rec.streams),
            }
            for rec in cat.sessions.values()
        ]
        return {"sessions": sessions, "count": len(sessions)}

    @application.get("/api/v1/sessions/{session_id}")
    async def get_session(session_id: str):
        svc = DataPlaneService(_sessions_dir())
        rec = svc.session(session_id)
        if rec is None:
            return {"error": f"session {session_id} not found"}
        return rec

    @application.get("/api/v1/sessions/{session_id}/report")
    async def get_session_report(session_id: str):
        svc = DataPlaneService(_sessions_dir())
        rep = svc.report(session_id)
        if rep is None:
            return {"error": f"session {session_id} not found"}
        return rep

    @application.get("/api/v1/metrics")
    async def get_metrics():
        svc = DataPlaneService(_sessions_dir())
        cat = svc.catalog()
        if cat is None:
            return {"count": 0, "by_format": {}, "total_samples": 0}
        by_format: dict[str, int] = {}
        total = 0
        for rec in cat.sessions.values():
            by_format[rec.format] = by_format.get(rec.format, 0) + 1
            total += rec.n_samples_total
        return {"count": len(cat.sessions), "by_format": by_format, "total_samples": total}

    @application.get("/api/v1/jobs/types")
    async def list_job_types():
        svc = DataPlaneService(_sessions_dir())
        return {"types": svc.job_types()}

    @application.post("/api/v1/jobs/run", response_model=JobRunResponse)
    async def run_job(request: JobRunRequest):
        svc = DataPlaneService(_sessions_dir())
        result = svc.run_job(request.computation_type, request.file_path)
        return JobRunResponse(
            ok=result.get("ok", False),
            payload=result.get("payload", {}),
            error=result.get("error", ""),
            algorithm_version=result.get("algorithm_version", ""),
            job_id=result.get("job_id", ""),
        )

    # ------------------------------------------------------------------
    # WebSocket
    # ------------------------------------------------------------------
    @application.websocket("/ws/live")
    async def websocket_live(websocket: _WS):
        await websocket.accept()
        connected_clients_local.add(websocket)
        try:
            while True:
                await asyncio.sleep(2.0)
                svc = DataPlaneService(_sessions_dir())
                cat = svc.catalog()
                await websocket.send_json(
                    {
                        "type": "status_update",
                        "data": {
                            "n_sessions": len(cat.sessions) if cat else 0,
                            "sessions_dir": str(svc.sessions_dir),
                        },
                    }
                )
        except _WSD:
            connected_clients_local.discard(websocket)

    return application


if _FASTAPI_AVAILABLE:
    app = create_app()  # type: ignore[assignment]

    if __name__ == "__main__":
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=8000)
