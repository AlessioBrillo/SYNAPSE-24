"""SYNAPSE-24 Data Plane service layer (no FastAPI dependency).

Pure-Python service over stored sessions + the stateless worker. Importable
without fastapi/uvicorn so it can be unit-tested in the dev environment where
only the project's own dependencies (pydantic, numpy, ...) are installed.

The FastAPI app that exposes this service lives in ``backend/main.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

# Make the repo `worker` package importable (job_contract + hrv_worker).
_sys_path = str(Path(__file__).resolve().parent.parent)
if _sys_path not in sys.path:
    sys.path.insert(0, _sys_path)

from synapse24.report import build_session_report  # noqa: E402
from synapse24.sessions import (  # noqa: E402
    index_directory,
)
from worker.hrv_worker import dispatch  # noqa: E402
from worker.job_contract import Job, JobType  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _sessions_dir() -> Path:
    return Path(os.environ.get("SYNAPSE_SESSIONS_DIR", "data/sessions"))


class JobRunRequest(BaseModel):
    computation_type: str
    file_path: str


class JobRunResponse(BaseModel):
    ok: bool
    payload: dict[str, Any] = {}
    error: str = ""
    algorithm_version: str = ""
    job_id: str = ""


class DataPlaneService:
    """Read-side service over stored sessions + stateless worker."""

    def __init__(self, sessions_dir: Path | str) -> None:
        self.sessions_dir = Path(sessions_dir)
        self._catalog: Any = None
        self._reload()

    def _reload(self) -> None:
        if self.sessions_dir.exists():
            self._catalog = index_directory(self.sessions_dir)
        else:
            self._catalog = None
        logger.info(
            "Data plane sessions dir=%s n_sessions=%s",
            self.sessions_dir,
            len(self._catalog.sessions) if self._catalog else 0,
        )

    def catalog(self) -> Any:
        self._reload()
        return self._catalog

    def session(self, session_id: str) -> dict[str, Any] | None:
        cat = self.catalog()
        if cat is None or session_id not in cat.sessions:
            return None
        rec = cat.sessions[session_id]
        return {
            "session_id": rec.session_id,
            "device_serial": rec.device_serial,
            "path": rec.path,
            "format": rec.format,
            "size_bytes": rec.size_bytes,
            "sha256": rec.sha256,
            "started_at": rec.started_at,
            "ended_at": rec.ended_at,
            "duration_s": rec.duration_s,
            "streams": [
                {
                    "name": s.name,
                    "type": s.type,
                    "sampling_rate_hz": s.sampling_rate_hz,
                    "n_samples": s.n_samples,
                }
                for s in rec.streams
            ],
            "n_samples_total": rec.n_samples_total,
            "xdf_valid": rec.xdf_valid,
            "fit_valid": rec.fit_valid,
            "indexed_at": rec.indexed_at,
        }

    def report(self, session_id: str, jobs: dict[str, dict] | None = None) -> dict[str, Any] | None:
        cat = self.catalog()
        if cat is None or session_id not in cat.sessions:
            return None
        return build_session_report(cat.sessions[session_id], jobs or {})

    def run_job(self, computation_type: str, file_path: str) -> dict[str, Any]:
        try:
            JobType(computation_type)
        except ValueError:
            return {
                "job_id": "unknown",
                "ok": False,
                "error": f"unsupported computation type {computation_type}",
                "algorithm_version": "synapse-worker-v1",
            }
        digest = hashlib.sha256(f"{file_path}|{computation_type}|api-v1".encode()).hexdigest()[:16]
        job = Job(
            job_id=f"api-{digest}",
            computation_type=JobType(computation_type),
            algorithm_version="api-v1",
            file_ref=file_path,
            file_sha256="",
        )
        return json.loads(dispatch(job.to_json(), Path(file_path)))

    def job_types(self) -> list[dict[str, str]]:
        return [
            {
                "name": JobType.HRV_TRENDS.value,
                "description": "HRV trends per window (FIT or XDF)",
            },
            {
                "name": JobType.STRESS_DAILY.value,
                "description": "Daily stress from edge triage labels (FIT)",
            },
            {
                "name": JobType.SQI_REPORT.value,
                "description": "Signal quality report per window (FIT)",
            },
            {
                "name": JobType.SLEEP_STAGING.value,
                "description": "YASA sleep staging on EEG (XDF)",
            },
        ]
