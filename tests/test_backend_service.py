"""Tests for the SYNAPSE-24 Data Plane service (backend/service.py).

No fastapi/httpx required: exercises DataPlaneService directly. The FastAPI
app wiring is checked separately (importorskip fastapi).
"""

from __future__ import annotations

import hashlib
import json
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backend.service import DataPlaneService, JobRunRequest, JobRunResponse  # noqa: E402
from synapse24.utils import write_xdf  # noqa: E402
def _crc16(data: bytes, crc: int = 0) -> int:
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def write_dev_fit(path: Path, records: list[dict]) -> None:
    """Minimal FIT with developer-data message 209 (mirrors export script)."""
    out = bytearray()
    hdr = bytearray(14)
    hdr[0] = 14
    hdr[8:12] = b".FIT"
    out += hdr
    field_sizes = [4, 1, 1, 1, 1, 1, 1, 2, 2, 2]
    field_types = [134, 2, 2, 2, 2, 2, 2, 132, 132, 132]
    defn = bytearray()
    defn.append(0x80 | 0)
    defn.append(0)
    defn.append(0)
    defn += (209).to_bytes(2, "little")
    defn.append(len(field_sizes))
    for fs, ft in zip(field_sizes, field_types):
        defn.append(0)
        defn.append(fs)
        defn.append(ft)
    out += defn
    out += (_crc16(bytes(defn))).to_bytes(2, "little")
    for rec in records:
        msg = bytearray()
        msg.append(0)
        msg += int(rec["timestamp"]).to_bytes(4, "little")
        msg.append(0)
        dev = rec["dev"]
        for fid in range(8):
            v = int(dev.get(fid, 0))
            if fid in (5, 6, 7):
                msg += (v & 0xFFFF).to_bytes(2, "little")
            else:
                msg.append(v & 0xFF)
        out += msg
        out += (_crc16(bytes(msg))).to_bytes(2, "little")
    path.write_bytes(bytes(out))


def write_eeg_xdf(path: Path, n_seconds: int = 300, fs: float = 250.0) -> None:
    n = int(n_seconds * fs)
    t = np.arange(n) / fs
    rng = np.random.default_rng(7)
    signal = 30 * np.sin(2 * np.pi * 10 * t) + rng.standard_normal(n)
    write_xdf(
        path,
        streams=[
            {
                "data": signal.reshape(-1, 1).astype(np.float64),
                "timestamps": t,
                "info": {"name": "EEG", "type": "EEG", "nominal_srate": fs},
            }
        ],
    )


@pytest.fixture
def svc(tmp_path: Path) -> DataPlaneService:
    write_eeg_xdf(tmp_path / "session_001.xdf", n_seconds=300, fs=250.0)
    return DataPlaneService(tmp_path)


def test_catalog_and_session(svc: DataPlaneService, tmp_path: Path) -> None:
    cat = svc.catalog()
    assert cat is not None
    assert len(cat.sessions) == 1
    sid = next(iter(cat.sessions))
    rec = svc.session(sid)
    assert rec is not None
    assert rec["session_id"] == sid
    assert rec["format"] == "xdf"
    assert rec["n_samples_total"] > 0
    assert len(rec["streams"]) == 1
    assert rec["streams"][0]["type"] == "EEG"


def test_report(svc: DataPlaneService) -> None:
    sid = next(iter(svc.catalog().sessions))
    rep = svc.report(sid)
    assert rep is not None
    assert rep["report_version"] == "session-report-v1"
    assert rep["session_id"] == sid
    assert rep["quality"]["overall"] == "good"


def test_metrics(svc: DataPlaneService) -> None:
    cat = svc.catalog()
    by_format: dict[str, int] = {}
    total = 0
    for rec in cat.sessions.values():
        by_format[rec.format] = by_format.get(rec.format, 0) + 1
        total += rec.n_samples_total
    assert by_format == {"xdf": 1}
    assert total > 0


def test_run_job_hrv_trends_on_xdf(svc: DataPlaneService, tmp_path: Path) -> None:
    xdf = tmp_path / "session_001.xdf"
    result = svc.run_job("hrv_trends", str(xdf))
    assert result["ok"] is True, result.get("error")
    assert "payload" in result
    assert result["algorithm_version"]


def test_run_job_stress_daily_on_fit(svc: DataPlaneService, tmp_path: Path) -> None:
    fit = tmp_path / "stress.fit"
    write_dev_fit(
        fit,
        [{"timestamp": i, "dev": {0: v}} for i, v in enumerate([0, 0, 1, 1, 1, 0, 0, 0])],
    )
    result = svc.run_job("stress_daily", str(fit))
    assert result["ok"] is True, result.get("error")
    assert result["payload"]["daily_average"] == 37.5


def test_run_job_unknown_type(svc: DataPlaneService, tmp_path: Path) -> None:
    xdf = tmp_path / "session_001.xdf"
    result = svc.run_job("does_not_exist", str(xdf))
    assert result["ok"] is False
    assert "unsupported computation type" in result["error"]


def test_job_types(svc: DataPlaneService) -> None:
    types = svc.job_types()
    names = {t["name"] for t in types}
    assert names == {"hrv_trends", "stress_daily", "sqi_report", "sleep_staging"}


def test_job_run_request_response_models() -> None:
    req = JobRunRequest(computation_type="hrv_trends", file_path="/tmp/x.xdf")
    assert req.computation_type == "hrv_trends"
    resp = JobRunResponse(ok=True, payload={"a": 1}, algorithm_version="v1", job_id="j1")
    assert resp.ok is True


def test_app_routes_registered() -> None:
    """FastAPI app exposes the data plane routes (skipped without fastapi)."""
    fastapi = pytest.importorskip("fastapi")
    del fastapi  # only checking importability
    from backend.main import create_app  # noqa: PLC0415

    app = create_app()
    paths = {r.path for r in app.routes}
    for expected in (
        "/api/health",
        "/api/v1/sessions",
        "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/report",
        "/api/v1/metrics",
        "/api/v1/jobs/types",
        "/api/v1/jobs/run",
        "/ws/live",
    ):
        assert expected in paths, f"missing route {expected}"
