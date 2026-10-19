"""Contract between Averyn and the Synapse stateless inference worker.

Averyn is the system of record (ADR-0010). The worker never has its own user
database: it receives a job (job id, reference to a file Averyn already
stored, computation type + version, **no direct user identifier**) and returns
a result keyed to that job id. Averyn attaches the result to the user's
activity/device as *imported data* with `source: "synapse-worker"` and the
worker's own `algorithm_version` (never coerced into an Averyn metric type).

This module is the machine-readable side of that contract: both Averyn (Ktor)
and the worker (Python) import it so job payloads can never drift silently.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from enum import StrEnum
from typing import Any


class JobType(StrEnum):
    """Computation types the worker can run. Extend only by TDD."""

    # Per-activity / per-window health signals from a stored FIT or XDF file.
    HRV_TRENDS = "hrv_trends"
    STRESS_DAILY = "stress_daily"
    SQI_REPORT = "sqi_report"
    # Sleep staging requires EEG input; PPG/IMU-only heuristics must use a
    # differently-named job type and never claim "YASA sleep staging".
    SLEEP_STAGING = "sleep_staging"


@dataclass(frozen=True)
class Job:
    """A dispatch from Averyn to the worker."""

    job_id: str
    computation_type: JobType
    algorithm_version: str  # worker's own version string, passed through verbatim
    file_ref: str  # reference to a file Averyn already stored (object key or path)
    file_sha256: str  # so the worker can verify it hasn't been tampered
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "computation_type": self.computation_type.value,
                "algorithm_version": self.algorithm_version,
                "file_ref": self.file_ref,
                "file_sha256": self.file_sha256,
                "created_at": self.created_at.isoformat(),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> "Job":
        obj = json.loads(text)
        return cls(
            job_id=obj["job_id"],
            computation_type=JobType(obj["computation_type"]),
            algorithm_version=obj["algorithm_version"],
            file_ref=obj["file_ref"],
            file_sha256=obj["file_sha256"],
            created_at=datetime.fromisoformat(obj["created_at"]),
        )

    @staticmethod
    def job_id_for(owner_scoped_key: str, computation_type: JobType, algorithm_version: str) -> str:
        """Deterministic job id so the same computation is never dispatched twice."""
        digest = hashlib.sha256(
            f"{owner_scoped_key}|{computation_type.value}|{algorithm_version}".encode()
        ).hexdigest()[:16]
        return f"syn-{computation_type.value}-{digest}"


@dataclass(frozen=True)
class JobResult:
    """The worker's answer, keyed to the job id."""

    job_id: str
    ok: bool
    # When ok: the computed payload (shape depends on computation_type).
    payload: dict[str, Any] = field(default_factory=dict)
    # When not ok: a short, non-health error message.
    error: str = ""
    algorithm_version: str = ""

    def to_json(self) -> str:
        return json.dumps(
            {
                "job_id": self.job_id,
                "ok": self.ok,
                "payload": self.payload,
                "error": self.error,
                "algorithm_version": self.algorithm_version,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, text: str) -> "JobResult":
        obj = json.loads(text)
        return cls(
            job_id=obj["job_id"],
            ok=obj["ok"],
            payload=obj.get("payload", {}),
            error=obj.get("error", ""),
            algorithm_version=obj.get("algorithm_version", ""),
        )


# Canonical result payload keys per computation_type. Averyn stores these as
# value_json in imported_health_samples and never interprets them beyond
# display.
HRV_TRENDS_KEYS = {"rmssd_ms", "sdnn_ms", "mean_rr_ms", "hr_mean_bpm", "window_s"}
STRESS_DAILY_KEYS = {"stress_level", "stress_prob", "baseline_prob", "artifact_prob", "window_s"}
SQI_REPORT_KEYS = {"sqi", "map", "ecg_quality", "window_s"}
SLEEP_STAGING_KEYS = {"sleep_stage", "confidence", "epoch_s"}

__all__ = [
    "Job",
    "JobResult",
    "JobType",
    "HRV_TRENDS_KEYS",
    "STRESS_DAILY_KEYS",
    "SQI_REPORT_KEYS",
    "SLEEP_STAGING_KEYS",
]
