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
from typing import Any

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import backend.service as service_module  # noqa: E402
from backend.service import (  # noqa: E402
    PATH_OUTSIDE_ALLOWED_ROOTS,
    DataPlaneService,
    JobRunRequest,
    JobRunResponse,
)
from synapse24.utils import write_xdf  # noqa: E402


def write_dev_fit(path: Path, records: list[dict]) -> None:
    """Write a FIT file with Synapse developer data (canonical writer).

    Each record is ``{"timestamp": int, "dev": {field_id: value}}``. The bytes
    come from the canonical writer (:mod:`synapse_device.fit`), so the fixture
    is the same format the product writes and the worker reads.
    """
    from synapse_device.fit import DEV_FIELDS, FitRecord, FitWriter

    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(serial_number=1)
    writer.write_developer_data_id()
    writer.write_field_descriptions()
    for rec in records:
        dev: dict[str, int] = {}
        for field in DEV_FIELDS:
            value = rec["dev"].get(field.field_id)
            dev[field.name] = int(value) if value is not None else field.invalid
        writer.write_record(FitRecord(timestamp=int(rec["timestamp"]), heart_rate=0, dev=dev))
    writer.finalize(path)


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


# ---------------------------------------------------------------------------
# Path containment (arbitrary file read hardening)
# ---------------------------------------------------------------------------


def _install_read_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Install a pathlib read spy; returns the list of paths that get read."""
    seen: list[str] = []
    original_open = Path.open
    original_read_bytes = Path.read_bytes

    def spy_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        seen.append(str(self))
        return original_open(self, *args, **kwargs)

    def spy_read_bytes(self: Path) -> bytes:
        seen.append(str(self))
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "open", spy_open)
    monkeypatch.setattr(Path, "read_bytes", spy_read_bytes)
    return seen


def _dispatch_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every worker dispatch (the worker is what opens the file)."""
    calls: list[str] = []
    monkeypatch.setattr(
        service_module,
        "dispatch",
        lambda _job_json, file_path: calls.append(str(file_path)) or '{"ok": true}',
    )
    return calls


def test_run_job_rejects_path_outside_allowed_roots(
    svc: DataPlaneService,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client path outside the sessions dir is refused and never read."""
    outside_dir = tmp_path_factory.mktemp("outside")
    secret = outside_dir / "secret.fit"
    secret.write_bytes(b"TOP SECRET PAYLOAD")
    calls = _dispatch_spy(monkeypatch)
    opened_paths = _install_read_spy(monkeypatch)

    result = svc.run_job("stress_daily", str(secret))

    assert result["ok"] is False
    assert result["error"] == PATH_OUTSIDE_ALLOWED_ROOTS
    assert calls == [], "the worker must not be invoked for an out-of-root path"
    assert str(secret) not in opened_paths, "the out-of-root file must not be read"
    assert str(secret.resolve()) not in opened_paths


def test_run_job_rejects_parent_traversal(
    svc: DataPlaneService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path escaping the sessions dir through ``..`` is refused."""
    outside = tmp_path.parent / "traversal_secret.fit"
    outside.write_bytes(b"TOP SECRET PAYLOAD")
    traversal = str(tmp_path / ".." / outside.name)
    calls = _dispatch_spy(monkeypatch)
    opened_paths = _install_read_spy(monkeypatch)

    result = svc.run_job("stress_daily", traversal)

    assert result["ok"] is False
    assert result["error"] == PATH_OUTSIDE_ALLOWED_ROOTS
    assert calls == []
    assert str(outside) not in opened_paths
    assert str(outside.resolve()) not in opened_paths


def test_run_job_rejects_absolute_path_outside_sessions_dir(
    svc: DataPlaneService,
) -> None:
    """Well-known system paths are refused as well."""
    for candidate in ("/etc/passwd", "C:/Windows/win.ini", "../../../etc/passwd"):
        result = svc.run_job("stress_daily", candidate)
        assert result["ok"] is False, candidate
        assert result["error"] == PATH_OUTSIDE_ALLOWED_ROOTS, candidate


def test_run_job_accepts_path_in_extra_data_root(
    svc: DataPlaneService,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SYNAPSE_DATA_ROOTS`` adds an allowed root for client-supplied paths."""
    extra_root = tmp_path_factory.mktemp("data_root")
    fit = extra_root / "stress.fit"
    write_dev_fit(
        fit,
        [{"timestamp": i, "dev": {0: v}} for i, v in enumerate([0, 0, 1, 1, 1, 0, 0, 0])],
    )
    monkeypatch.setenv("SYNAPSE_DATA_ROOTS", str(extra_root))

    assert extra_root.resolve() in svc.allowed_roots()
    result = svc.run_job("stress_daily", str(fit))
    assert result["error"] != PATH_OUTSIDE_ALLOWED_ROOTS
    assert result["ok"] is True, result.get("error")


def test_allowed_roots_defaults_to_sessions_dir(svc: DataPlaneService) -> None:
    roots = svc.allowed_roots()
    assert svc.sessions_dir.resolve() in roots


def test_run_job_reports_real_file_sha256(svc: DataPlaneService, tmp_path: Path) -> None:
    """The job carries the real sha256 of the file, not an empty string."""
    xdf = tmp_path / "session_001.xdf"
    expected = hashlib.sha256(xdf.read_bytes()).hexdigest()

    result = svc.run_job("hrv_trends", str(xdf))

    assert result["ok"] is True, result.get("error")
    assert result["file_sha256"] == expected


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


def test_catalog_is_not_reindexed_when_tree_is_unchanged(
    svc: DataPlaneService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog is indexed once and reused until the tree changes."""
    calls: list[str] = []
    original = service_module.index_directory

    def counting(root: Any) -> Any:
        calls.append(str(root))
        return original(root)

    monkeypatch.setattr(service_module, "index_directory", counting)

    svc.catalog()
    svc.catalog()
    svc.session(next(iter(svc.catalog().sessions)))

    assert len(calls) == 1


def test_catalog_is_reindexed_after_tree_change(
    svc: DataPlaneService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new file in the sessions dir invalidates the cached catalog."""
    calls: list[str] = []
    original = service_module.index_directory

    def counting(root: Any) -> Any:
        calls.append(str(root))
        return original(root)

    monkeypatch.setattr(service_module, "index_directory", counting)

    svc.catalog()
    assert len(calls) == 1

    write_eeg_xdf(svc.sessions_dir / "session_002.xdf", n_seconds=2, fs=50.0)
    cat = svc.catalog()

    assert len(calls) == 2
    assert len(cat.sessions) == 2


def test_file_sha256_is_cached(svc: DataPlaneService, tmp_path: Path) -> None:
    """Repeated digest computations reuse the cached value."""
    xdf = tmp_path / "session_001.xdf"
    first = svc.file_sha256(xdf)
    second = svc.file_sha256(xdf)
    assert first == second
    assert first == hashlib.sha256(xdf.read_bytes()).hexdigest()
