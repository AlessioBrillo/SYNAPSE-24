"""SYNAPSE-24 Data Plane API (FastAPI).

The service logic lives in ``backend/service.py`` (importable without fastapi).
This module only assembles the FastAPI app; fastapi/uvicorn are imported
lazily so ``import backend.main`` works in environments that only have the
project's own dependencies installed.

Run:
    uvicorn backend.main:app   (fastapi/uvicorn required)

Security model
--------------
* **Authentication.** When ``SYNAPSE_API_TOKEN`` is set, every ``/api/v1/*``
  endpoint and every legacy ``/api/*`` endpoint requires
  ``Authorization: Bearer <token>``; a missing or wrong token gets HTTP 401.
  The token is read from the environment on every request, so it can be
  rotated without restarting the process. ``/api/health`` is deliberately
  exempt: it is the liveness probe used by supervisors, container
  orchestrators and load balancers, which must be able to check that the
  process is alive without holding a credential. It returns a static status
  string and no session data, so it leaks nothing. When
  ``SYNAPSE_API_TOKEN`` is *not* set the API stays open for local development
  and a loud WARNING is logged at startup.
* **CORS.** Only explicit origins are allowed: the Vite dev server by default
  (``http://localhost:5173``, ``http://127.0.0.1:5173``), extended with the
  comma-separated ``SYNAPSE_CORS_ORIGINS``. The wildcard ``*`` is never
  accepted - it is invalid together with ``allow_credentials=True`` (CORS
  spec) and would expose the API to every website.
* **Path containment.** Client-supplied paths are validated by the service
  layer (see ``backend/service.py``): a path outside the allowed roots is
  rejected without ever being opened. The API layer never touches the
  filesystem on behalf of a client.
* **Non-blocking.** The service layer is synchronous, so every call made from
  an ``async`` endpoint is dispatched to a worker thread instead of blocking
  the event loop.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from backend.service import DataPlaneService, JobRunRequest, JobRunResponse  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_FASTAPI_AVAILABLE = True
try:  # fastapi/uvicorn are runtime deps, not dev deps
    from fastapi import (  # noqa: F401
        Depends,
        FastAPI,
        HTTPException,
        Request,
        WebSocket,
        WebSocketDisconnect,
    )
    from fastapi.middleware.cors import CORSMiddleware  # noqa: F401
    from starlette.concurrency import run_in_threadpool  # noqa: F401
except ImportError:  # pragma: no cover - dev environment without fastapi
    Depends = None  # type: ignore[assignment,misc]
    FastAPI = None  # type: ignore[assignment,misc]
    HTTPException = None  # type: ignore[assignment,misc]
    Request = None  # type: ignore[assignment,misc]
    WebSocket = None  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]
    CORSMiddleware = None  # type: ignore[assignment,misc]
    run_in_threadpool = None  # type: ignore[assignment]
    _FASTAPI_AVAILABLE = False

app: Any = None

#: Environment variable holding the bearer token protecting the API.
TOKEN_ENV = "SYNAPSE_API_TOKEN"

#: Environment variable holding extra CORS origins (comma separated).
CORS_ORIGINS_ENV = "SYNAPSE_CORS_ORIGINS"

#: Origins always allowed (the Averyn / Vite dev server).
DEFAULT_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")

# Shared service instances, keyed by resolved sessions directory. The service
# caches the session catalog, so a single instance must be reused across
# requests instead of being rebuilt per endpoint call.
_services: dict[str, DataPlaneService] = {}


def _sessions_dir() -> Path:
    return Path(os.environ.get("SYNAPSE_SESSIONS_DIR", "data/sessions"))


def _service() -> DataPlaneService:
    """Return the shared service for the configured sessions directory."""
    sessions = _sessions_dir()
    try:
        key = str(sessions.resolve())
    except (OSError, RuntimeError, ValueError):  # pragma: no cover - exotic paths
        key = str(sessions)
    svc = _services.get(key)
    if svc is None:
        svc = DataPlaneService(sessions)
        _services[key] = svc
    return svc


def _reset_services() -> None:
    """Drop the cached service instances (used by tests)."""
    _services.clear()


def _api_token() -> str:
    """Return the configured bearer token (empty when the API is open)."""
    return os.environ.get(TOKEN_ENV, "").strip()


def _bearer_token(header: str) -> str:
    """Extract the token from an ``Authorization: Bearer <token>`` header."""
    parts = header.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return ""


def _cors_origins() -> list[str]:
    """Allowed CORS origins: the dev defaults plus ``SYNAPSE_CORS_ORIGINS``."""
    origins: list[str] = list(DEFAULT_CORS_ORIGINS)
    extra = os.environ.get(CORS_ORIGINS_ENV, "")
    for chunk in extra.split(","):
        origin = chunk.strip()
        if not origin:
            continue
        if origin == "*":
            logger.warning(
                "ignoring wildcard CORS origin '*': a wildcard origin is invalid "
                "together with allow_credentials=True"
            )
            continue
        if origin not in origins:
            origins.append(origin)
    return origins


async def require_api_token(request: Request) -> None:
    """FastAPI dependency enforcing the bearer token when one is configured.

    When ``SYNAPSE_API_TOKEN`` is unset the API stays open (local
    development) and this dependency is a no-op. Otherwise a missing or
    incorrect ``Authorization: Bearer <token>`` header yields HTTP 401.
    """
    expected = _api_token()
    if not expected:
        return
    provided = _bearer_token(request.headers.get("authorization", ""))
    if provided and hmac.compare_digest(provided.encode(), expected.encode()):
        return
    raise HTTPException(
        status_code=401,
        detail="missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _auth_dependencies() -> list[Any]:
    """Route dependencies enforcing the bearer token.

    Applied to every ``/api`` route except the ``/api/health`` liveness
    probe, so the protection cannot drift between endpoints.
    """
    return [Depends(require_api_token)]


def _read_hardware_config() -> dict[str, Any]:
    """Read ``config/hardware.yaml`` (blocking; called from a worker thread)."""
    import yaml

    hw = Path("config/hardware.yaml")
    if hw.exists():
        with hw.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    return {"error": "config/hardware.yaml not found"}


def _register_legacy_endpoints(application: Any) -> None:
    """Register the legacy dashboard endpoints (kept functional)."""

    @application.get("/api/health")
    async def health_check() -> dict[str, Any]:
        """Liveness probe.

        Intentionally left unauthenticated (see the module docstring): it is
        used by supervisors and container orchestrators that cannot hold a
        credential, and it exposes no session data.
        """
        return {"status": "ok", "service": "synapse24-data-plane"}

    @application.get("/api/status", dependencies=_auth_dependencies())
    async def get_status() -> dict[str, Any]:
        svc = _service()
        cat = await run_in_threadpool(svc.catalog)
        n = len(cat.sessions) if cat else 0
        return {
            "current_tier": "T0",
            "n_sessions": n,
            "sessions_dir": str(svc.sessions_dir),
        }

    @application.get("/api/sync", dependencies=_auth_dependencies())
    async def get_sync_status() -> dict[str, Any]:
        return {
            "pods": {},
            "note": "clock sync requires hardware; not available in software-only mode",
        }

    @application.get("/api/quality/{pod_id}", dependencies=_auth_dependencies())
    async def get_signal_quality(pod_id: str) -> dict[str, Any]:
        svc = _service()
        rec = await run_in_threadpool(svc.session, pod_id)
        if rec is None:
            return {"error": "session not found"}
        return {
            "session_id": rec["session_id"],
            "format": rec["format"],
            "xdf_valid": rec["xdf_valid"],
            "fit_valid": rec["fit_valid"],
            "n_samples_total": rec["n_samples_total"],
        }

    @application.get("/api/live/{pod_id}", dependencies=_auth_dependencies())
    async def get_live_data(pod_id: str, duration_s: float = 10.0) -> dict[str, Any]:
        return {
            "error": "live streaming requires hardware / LSL relay; not available in software-only mode",
            "pod_id": pod_id,
        }

    @application.post("/api/control/tier", dependencies=_auth_dependencies())
    async def control_tier() -> dict[str, Any]:
        return {"error": "tier control requires hardware; not available in software-only mode"}

    @application.post("/api/control/record", dependencies=_auth_dependencies())
    async def control_recording() -> dict[str, Any]:
        return {"error": "recording requires hardware; not available in software-only mode"}

    @application.get("/api/config", dependencies=_auth_dependencies())
    async def get_config() -> dict[str, Any]:
        cfg = await run_in_threadpool(_read_hardware_config)
        return cfg


def _register_data_plane_endpoints(application: Any) -> None:
    """Register the Averyn-facing data plane endpoints."""

    @application.get("/api/v1/sessions", dependencies=_auth_dependencies())
    async def list_sessions() -> dict[str, Any]:
        svc = _service()
        cat = await run_in_threadpool(svc.catalog)
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

    @application.get("/api/v1/sessions/{session_id}", dependencies=_auth_dependencies())
    async def get_session(session_id: str) -> dict[str, Any]:
        svc = _service()
        rec = await run_in_threadpool(svc.session, session_id)
        if rec is None:
            return {"error": f"session {session_id} not found"}
        return rec

    @application.get("/api/v1/sessions/{session_id}/report", dependencies=_auth_dependencies())
    async def get_session_report(session_id: str) -> dict[str, Any]:
        svc = _service()
        rep = await run_in_threadpool(svc.report, session_id)
        if rep is None:
            return {"error": f"session {session_id} not found"}
        return rep

    @application.get("/api/v1/metrics", dependencies=_auth_dependencies())
    async def get_metrics() -> dict[str, Any]:
        svc = _service()
        cat = await run_in_threadpool(svc.catalog)
        if cat is None:
            return {"count": 0, "by_format": {}, "total_samples": 0}
        by_format: dict[str, int] = {}
        total = 0
        for rec in cat.sessions.values():
            by_format[rec.format] = by_format.get(rec.format, 0) + 1
            total += rec.n_samples_total
        return {"count": len(cat.sessions), "by_format": by_format, "total_samples": total}

    @application.get("/api/v1/jobs/types", dependencies=_auth_dependencies())
    async def list_job_types() -> dict[str, Any]:
        svc = _service()
        return {"types": svc.job_types()}

    @application.post(
        "/api/v1/jobs/run",
        response_model=JobRunResponse,
        dependencies=_auth_dependencies(),
    )
    async def run_job(request: JobRunRequest) -> JobRunResponse:
        svc = _service()
        result = await run_in_threadpool(svc.run_job, request.computation_type, request.file_path)
        return JobRunResponse(
            ok=result.get("ok", False),
            payload=result.get("payload", {}),
            error=result.get("error", ""),
            algorithm_version=result.get("algorithm_version", ""),
            job_id=result.get("job_id", ""),
            file_sha256=result.get("file_sha256", ""),
        )


def _register_websocket(application: Any) -> None:
    """Register the WebSocket live endpoint."""
    connected_clients_local: set[WebSocket] = set()

    @application.websocket("/ws/live")
    async def websocket_live(websocket: WebSocket) -> None:
        expected = _api_token()
        if expected:
            provided = _bearer_token(websocket.headers.get("authorization", "")) or (
                websocket.query_params.get("token", "")
            )
            if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
                await websocket.close(code=1008, reason="unauthorized")
                return
        await websocket.accept()
        connected_clients_local.add(websocket)
        try:
            while True:
                await asyncio.sleep(2.0)
                svc = _service()
                cat = await run_in_threadpool(svc.catalog)
                await websocket.send_json(
                    {
                        "type": "status_update",
                        "data": {
                            "n_sessions": len(cat.sessions) if cat else 0,
                            "sessions_dir": str(svc.sessions_dir),
                        },
                    }
                )
        except WebSocketDisconnect:
            connected_clients_local.discard(websocket)


def create_app() -> Any:
    """Build the FastAPI app. Requires fastapi."""
    if not _FASTAPI_AVAILABLE:
        raise RuntimeError("fastapi is not installed; install backend/requirements.txt")

    @asynccontextmanager
    async def lifespan(application: Any) -> AsyncIterator[None]:
        if not _api_token():
            logger.warning(
                "SYNAPSE_API_TOKEN is not set - the SYNAPSE-24 Data Plane API is "
                "UNAUTHENTICATED. Every client that can reach this port can read "
                "the session catalog and run worker jobs. Set SYNAPSE_API_TOKEN "
                "before exposing the API outside the local machine."
            )
        logger.info("SYNAPSE-24 Data Plane API started")
        yield
        logger.info("SYNAPSE-24 Data Plane API shut down")

    application = FastAPI(
        title="SYNAPSE-24 Data Plane API",
        description="Read-side interfaces for Averyn: sessions, reports, worker jobs.",
        version="1.0.0",
        lifespan=lifespan,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_legacy_endpoints(application)
    _register_data_plane_endpoints(application)
    _register_websocket(application)

    return application


if _FASTAPI_AVAILABLE:
    app = create_app()  # type: ignore[assignment]

    if __name__ == "__main__":
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=8000)
