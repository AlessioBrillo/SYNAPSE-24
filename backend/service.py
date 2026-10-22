"""SYNAPSE-24 Data Plane service layer (no FastAPI dependency).

Pure-Python service over stored sessions + the stateless worker. Importable
without fastapi/uvicorn so it can be unit-tested in the dev environment where
only the project's own dependencies (pydantic, numpy, ...) are installed.

The FastAPI app that exposes this service lives in ``backend/main.py``.

Security model
--------------
This module is the only place where a client-supplied path reaches the
filesystem, so it owns the following invariants:

* **Path containment.** Every client-supplied path is resolved
  (``Path.resolve()``: collapses ``..`` and follows symlinks) and is rejected
  unless it lands inside an *allowed root*. The allowed roots are the
  sessions directory plus the extra roots listed in the ``SYNAPSE_DATA_ROOTS``
  environment variable (``os.pathsep`` separated). A rejected path is never
  opened, never hashed and never handed to the worker.
* **Bounded IO.** The session catalog is re-indexed only when the sessions
  tree actually changed, and per-file sha256 digests are cached on
  ``(path, size, mtime_ns)`` so a job run does not re-hash the world.

Authentication is a transport concern and lives in ``backend/main.py``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import threading
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

#: Error code returned to clients whose path escapes the allowed roots.
PATH_OUTSIDE_ALLOWED_ROOTS = "path outside allowed roots"

#: Environment variable holding extra allowed roots (``os.pathsep`` separated).
DATA_ROOTS_ENV = "SYNAPSE_DATA_ROOTS"

#: Read size used when hashing a file.
_HASH_CHUNK = 65536

#: Upper bound on the number of cached file digests (FIFO eviction).
_SHA_CACHE_MAX = 4096


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
    file_sha256: str = ""


class DataPlaneService:
    """Read-side service over stored sessions + stateless worker.

    The service owns the filesystem boundary of the Data Plane API:

    * client-supplied paths are resolved and confined to the allowed roots
      (``sessions_dir`` plus the roots in ``SYNAPSE_DATA_ROOTS``);
    * the session catalog is cached and re-indexed only when the sessions
      tree changed (``refresh()`` forces a re-index);
    * per-file sha256 digests are cached on ``(path, size, mtime_ns)``.
    """

    def __init__(self, sessions_dir: Path | str) -> None:
        self.sessions_dir = Path(sessions_dir)
        self._catalog: Any = None
        self._catalog_fingerprint: tuple[tuple[str, int, int], ...] | None = None
        self._catalog_loaded = False
        self._sha256_cache: dict[tuple[str, int, int], str] = {}
        self._lock = threading.Lock()

    # -- catalog ----------------------------------------------------------

    def _tree_fingerprint(self) -> tuple[tuple[str, int, int], ...]:
        """Cheap fingerprint of the sessions tree.

        Collects ``(path, size, mtime_ns)`` for every file and
        ``(path, 0, mtime_ns)`` for every directory under the sessions root.
        Stat-only (no file content is read), so it is orders of magnitude
        cheaper than re-indexing (which hashes every session file).
        """
        items: list[tuple[str, int, int]] = []
        try:
            for dirpath, dirnames, filenames in os.walk(self.sessions_dir):
                dirnames.sort()
                dir_path = Path(dirpath)
                try:
                    st = dir_path.stat()
                    items.append((str(dir_path), 0, st.st_mtime_ns))
                except OSError:
                    continue
                for name in sorted(filenames):
                    file_path = dir_path / name
                    try:
                        st = file_path.stat()
                    except OSError:
                        continue
                    items.append((str(file_path), st.st_size, st.st_mtime_ns))
        except OSError:  # unreadable root: treat as empty
            return ()
        return tuple(items)

    def _reload(self, fingerprint: tuple[tuple[str, int, int], ...]) -> None:
        if self.sessions_dir.exists():
            self._catalog = index_directory(self.sessions_dir)
        else:
            self._catalog = None
        self._catalog_fingerprint = fingerprint
        self._catalog_loaded = True
        logger.info(
            "Data plane sessions dir=%s n_sessions=%s",
            self.sessions_dir,
            len(self._catalog.sessions) if self._catalog else 0,
        )

    def catalog(self, *, refresh: bool = False) -> Any:
        """Return the session catalog, re-indexing only when it changed."""
        with self._lock:
            fingerprint = self._tree_fingerprint()
            if refresh or not self._catalog_loaded or fingerprint != self._catalog_fingerprint:
                self._reload(fingerprint)
            return self._catalog

    def refresh(self) -> Any:
        """Force a re-index of the sessions directory and return the catalog."""
        with self._lock:
            self._catalog_loaded = False
        return self.catalog(refresh=True)

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

    # -- path containment -------------------------------------------------

    def allowed_roots(self) -> list[Path]:
        """Roots a client-supplied path is allowed to point into.

        The sessions directory always qualifies; ``SYNAPSE_DATA_ROOTS`` adds
        extra roots (``os.pathsep`` separated) for deployments that mount
        additional data volumes.
        """
        roots: list[Path] = [self.sessions_dir]
        extra = os.environ.get(DATA_ROOTS_ENV, "")
        for part in extra.split(os.pathsep):
            candidate = part.strip()
            if candidate:
                roots.append(Path(candidate))
        resolved: list[Path] = []
        for root in roots:
            try:
                resolved.append(root.resolve())
            except (OSError, RuntimeError, ValueError):  # pragma: no cover - exotic paths
                continue
        return resolved

    def resolve_allowed_path(self, file_path: str | Path) -> Path | None:
        """Resolve ``file_path`` and return it only when it is allowed.

        Returns ``None`` when the path escapes every allowed root. The path is
        never opened by this method.
        """
        if file_path is None:
            return None
        try:
            candidate = Path(file_path).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        for root in self.allowed_roots():
            if candidate == root or candidate.is_relative_to(root):
                return candidate
        return None

    # -- digests ----------------------------------------------------------

    def file_sha256(self, path: Path) -> str:
        """sha256 of the file content, cached on ``(path, size, mtime_ns)``."""
        try:
            st = path.stat()
        except OSError:
            return ""
        key = (str(path), st.st_size, st.st_mtime_ns)
        with self._lock:
            cached = self._sha256_cache.get(key)
        if cached is not None:
            return cached
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                    digest.update(chunk)
        except OSError:
            return ""
        value = digest.hexdigest()
        with self._lock:
            self._sha256_cache[key] = value
            while len(self._sha256_cache) > _SHA_CACHE_MAX:
                self._sha256_cache.pop(next(iter(self._sha256_cache)))
        return value

    # -- jobs -------------------------------------------------------------

    def run_job(self, computation_type: str, file_path: str) -> dict[str, Any]:
        """Run a worker job on a file that must live inside the allowed roots.

        A path outside the allowed roots is rejected without ever opening the
        file (``ok=False``, ``error="path outside allowed roots"``).
        """
        resolved = self.resolve_allowed_path(file_path)
        if resolved is None:
            logger.warning(
                "rejected job request: path outside allowed roots (roots=%s)",
                [str(r) for r in self.allowed_roots()],
            )
            return {
                "job_id": "unknown",
                "ok": False,
                "error": PATH_OUTSIDE_ALLOWED_ROOTS,
                "algorithm_version": "synapse-worker-v1",
                "file_sha256": "",
            }
        try:
            job_type = JobType(computation_type)
        except ValueError:
            return {
                "job_id": "unknown",
                "ok": False,
                "error": f"unsupported computation type {computation_type}",
                "algorithm_version": "synapse-worker-v1",
                "file_sha256": "",
            }
        if not resolved.is_file():
            return {
                "job_id": "unknown",
                "ok": False,
                "error": "file not found",
                "algorithm_version": "synapse-worker-v1",
                "file_sha256": "",
            }
        digest = hashlib.sha256(f"{resolved}|{computation_type}|api-v1".encode()).hexdigest()[:16]
        file_sha256 = self.file_sha256(resolved)
        job = Job(
            job_id=f"api-{digest}",
            computation_type=job_type,
            algorithm_version="api-v1",
            file_ref=str(resolved),
            file_sha256=file_sha256,
        )
        try:
            result: dict[str, Any] = json.loads(dispatch(job.to_json(), resolved))
        except Exception:
            logger.exception("worker dispatch failed for job %s", job.job_id)
            return {
                "job_id": job.job_id,
                "ok": False,
                "error": "job execution failed",
                "algorithm_version": "synapse-worker-v1",
                "file_sha256": file_sha256,
            }
        result["file_sha256"] = file_sha256
        return result

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
