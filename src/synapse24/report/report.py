"""SessionReport v1 — representation of a single recorded session for Averyn.

Builds a JSON report from a :class:`synapse24.sessions.SessionRecord` plus
optional worker job results (keyed by computation type). Raw waveforms stay in
the source FIT/XDF files; this report carries metadata, decimation policy,
windowed metrics, detected events and a quality summary — exactly what the
local reference service exposes to Averyn.

No hardware dependency; fully testable on synthetic fixtures.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from synapse24.sessions import SessionRecord, StreamInfo

REPORT_VERSION = "session-report-v1"

# Stream type -> physical unit (best effort; raw files are authoritative).
_TYPE_TO_UNIT: dict[str, str] = {
    "EEG": "µV",
    "ECG": "mV",
    "PPG": "AU",
    "EDA": "µS",
    "ACC": "m/s²",
    "GYRO": "°/s",
    "MAG": "µT",
    "TEMP": "°C",
    "MARKERS": "label",
    "UNKNOWN": "AU",
}


def _unit_for(stream_type: str) -> str:
    return _TYPE_TO_UNIT.get(stream_type.upper(), "AU")


def _quality_overall(record: SessionRecord) -> str:
    if record.format == "xdf" and record.xdf_valid is False:
        return "poor"
    if record.format == "fit" and record.fit_valid is False:
        return "poor"
    if record.xdf_valid is None and record.fit_valid is None:
        return "warning"
    return "good"


def _events_from_jobs(job_results: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not job_results:
        return events
    stress = job_results.get("stress_daily", {}) or {}
    for ep in stress.get("stress_episodes", []) or []:
        events.append(
            {
                "kind": "stress_episode",
                "start": ep.get("start"),
                "end": ep.get("end"),
                "len": ep.get("len"),
                "source": "stress_daily",
            }
        )
    sleep = job_results.get("sleep_staging", {}) or {}
    hypno = sleep.get("hypnogram")
    if hypno:
        # Sleep stage transitions as events.
        prev = None
        for i, stage in enumerate(hypno):
            if prev is not None and stage != prev:
                events.append(
                    {
                        "kind": "sleep_stage_transition",
                        "epoch": i,
                        "from": prev,
                        "to": stage,
                        "source": "sleep_staging",
                    }
                )
            prev = stage
    return events


def build_session_report(
    record: SessionRecord,
    job_results: dict[str, dict[str, Any]] | None = None,
    *,
    decimation_factor: int = 10,
) -> dict[str, Any]:
    """Build a SessionReport v1 dict from a session record + optional job results."""
    streams_out = [
        {
            "name": s.name,
            "type": s.type,
            "sampling_rate_hz": s.sampling_rate_hz,
            "n_samples": s.n_samples,
            "unit": _unit_for(s.type),
        }
        for s in record.streams
    ]

    # v1: raw waveforms are not embedded; the preview series is empty and the
    # decimation policy is declared so consumers know how to decimate on demand.
    series = {"timestamps": [], "min": [], "max": []}

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "session_id": record.session_id,
        "device_serial": record.device_serial,
        "format": record.format,
        "window": {
            "started_at": record.started_at,
            "ended_at": record.ended_at,
            "duration_s": record.duration_s,
        },
        "streams": streams_out,
        "n_samples_total": record.n_samples_total,
        "decimation": {
            "factor": int(decimation_factor),
            "policy": "min_max_envelope",
            "applied_to": [],
        },
        "series": series,
        "metrics": dict(job_results or {}),
        "events": _events_from_jobs(job_results),
        "quality": {
            "overall": _quality_overall(record),
            "xdf_valid": record.xdf_valid,
            "fit_valid": record.fit_valid,
            "notes": [],
        },
        "raw_ref": {
            "path": record.path,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
        },
        "generated_at": record.indexed_at,
    }
    return report


def report_quality_note(report: dict[str, Any], note: str) -> None:
    """Append a quality note (mutates in place)."""
    report.setdefault("quality", {}).setdefault("notes", []).append(note)
