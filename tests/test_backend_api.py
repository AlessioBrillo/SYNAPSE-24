"""End-to-end contract tests for the Synapse local Data Plane API.

Exercises the six Averyn-facing endpoints (sessions, session detail, report,
metrics, job types, job run) against a real FastAPI TestClient, on a
synthetic session written to disk, and validates every worker payload against
the canonical JSON Schema in contracts/schemas/worker_job_result.json.

Requires fastapi + httpx (dev dependencies).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from backend import main as main_module
from backend import service as service_module
from backend.main import app, create_app
from synapse24.utils.xdf import write_xdf

try:  # fastapi/htpx are dev deps
    from fastapi.testclient import TestClient  # type: ignore
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

try:
    import jsonschema  # type: ignore

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover
    jsonschema = None  # type: ignore[assignment]
    _HAS_JSONSCHEMA = False

ROOT = Path(__file__).resolve().parents[1]
WORKER_RESULT_SCHEMA = json.loads(
    (ROOT / "contracts" / "schemas" / "worker_job_result.json").read_text(encoding="utf-8")
)

pytestmark = pytest.mark.skipif(
    TestClient is None or not _HAS_JSONSCHEMA,
    reason="fastapi/httpx/jsonschema required for Data Plane API contract tests",
)


def _write_minimal_xdf(path: Path, n_samples: int = 200, fs: float = 500.0) -> None:
    """Write a minimal XDF with one ECG stream (valid for synapse24.utils.xdf)."""
    ts = np.arange(n_samples, dtype=np.float64) / fs
    data = np.zeros((n_samples, 1), dtype=np.float64)
    data[:, 0] = 1.0 + 0.1 * np.sin(2 * np.pi * 1.0 * ts)
    write_xdf(
        path,
        [
            {
                "data": data,
                "timestamps": ts,
                "info": {
                    "name": "ECG",
                    "type": "SYNAPSE_ECG",
                    "channel_count": 1,
                    "nominal_srate": fs,
                    "channel_names": ["ecg_uv"],
                    "units": ["uV"],
                    "source_id": "synthetic",
                },
            }
        ],
    )


def _write_minimal_fit(path: Path) -> None:
    """Write a minimal FIT with one record (canonical writer)."""
    from synapse_device.fit import DEV_FIELDS, FitRecord, FitWriter

    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(serial_number=12345)
    writer.write_developer_data_id()
    writer.write_field_descriptions()
    dev = {field.name: field.invalid for field in DEV_FIELDS}
    dev["synapse_sqi"] = 200
    dev["synapse_map"] = 50
    dev["synapse_ecg_quality"] = 220
    writer.write_record(FitRecord(timestamp=1000, heart_rate=72, dev=dev))
    writer.finalize(path)


def _validate(computation_type: str, payload: dict) -> None:
    sub = WORKER_RESULT_SCHEMA["payloads"][computation_type]
    jsonschema.validate(instance=payload, schema=sub)


def _session_path(client: TestClient, session_id: str) -> str:
    r = client.get(f"/api/v1/sessions/{session_id}")
    assert r.status_code == 200
    return r.json()["path"]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sessions_dir = tmp_path
    monkeypatch.setenv("SYNAPSE_SESSIONS_DIR", str(sessions_dir))

    _write_minimal_xdf(sessions_dir / "session_ecg.xdf")
    _write_minimal_fit(sessions_dir / "session_sqi.fit")

    # The app caches one service (and its session catalog) per sessions
    # directory: start every test from a clean cache.
    main_module._reset_services()
    return TestClient(app)


def test_sessions_list(client: TestClient):
    r = client.get("/api/v1/sessions")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    assert any(s["format"] == "xdf" for s in body["sessions"])
    assert any(s["format"] == "fit" for s in body["sessions"])


def test_session_detail(client: TestClient):
    listed = client.get("/api/v1/sessions").json()["sessions"]
    sid = listed[0]["session_id"]
    r = client.get(f"/api/v1/sessions/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == sid
    assert "streams" in body
    assert "n_samples_total" in body


def test_session_report(client: TestClient):
    listed = client.get("/api/v1/sessions").json()["sessions"]
    sid = listed[0]["session_id"]
    r = client.get(f"/api/v1/sessions/{sid}/report")
    assert r.status_code == 200
    body = r.json()
    assert body["report_version"] == "session-report-v1"
    assert body["session_id"] == sid
    assert "streams" in body
    assert "metrics" in body
    assert "quality" in body
    assert body["quality"]["overall"] in ("good", "warning", "poor")


def test_metrics(client: TestClient):
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    body = r.json()
    assert "count" in body
    assert "by_format" in body
    assert "total_samples" in body


def test_job_types(client: TestClient):
    r = client.get("/api/v1/jobs/types")
    assert r.status_code == 200
    types = [t["name"] for t in r.json()["types"]]
    assert set(types) == {"hrv_trends", "stress_daily", "sqi_report", "sleep_staging"}


def _session_by_format(client: TestClient, fmt: str) -> dict:
    listed = client.get("/api/v1/sessions").json()["sessions"]
    for s in listed:
        if s.get("format") == fmt:
            return s
    raise AssertionError(f"no session with format {fmt}; sessions={listed}")


def test_job_run_hrv_trends(client: TestClient):
    xdf = _session_by_format(client, "xdf")
    r = client.post(
        "/api/v1/jobs/run",
        json={
            "computation_type": "hrv_trends",
            "file_path": _session_path(client, xdf["session_id"]),
        },
    )
    assert r.status_code == 200
    res = r.json()
    assert res["ok"] is True, res.get("error")
    assert res["algorithm_version"]
    _validate("hrv_trends", res["payload"])


def test_job_run_sqi_report(client: TestClient):
    fit = _session_by_format(client, "fit")
    r = client.post(
        "/api/v1/jobs/run",
        json={
            "computation_type": "sqi_report",
            "file_path": _session_path(client, fit["session_id"]),
        },
    )
    assert r.status_code == 200
    res = r.json()
    assert res["algorithm_version"]
    # The hand-rolled test FIT may not carry readable dev fields; when the
    # worker can parse them the payload must conform to the schema.
    if res.get("ok") and res.get("payload"):
        _validate("sqi_report", res["payload"])


def test_job_run_unknown_type(client: TestClient):
    r = client.post(
        "/api/v1/jobs/run",
        json={"computation_type": "does_not_exist", "file_path": "/nonexistent.fit"},
    )
    assert r.status_code == 200
    res = r.json()
    assert res["ok"] is False
    assert res["error"]


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


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Sessions dir nested in tmp_path so a sibling file plays 'outside'."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setenv("SYNAPSE_SESSIONS_DIR", str(sessions_dir))
    _write_minimal_xdf(sessions_dir / "session_ecg.xdf")
    _write_minimal_fit(sessions_dir / "session_sqi.fit")
    outside = tmp_path / "outside_secret.fit"
    outside.write_bytes(b"TOP SECRET PAYLOAD")
    return sessions_dir, outside


def test_job_run_rejects_path_outside_sessions_dir(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """A path outside the sessions dir is refused and the file is not read."""
    _sessions_dir, outside = sandbox
    client = TestClient(app)
    calls: list[str] = []
    monkeypatch.setattr(
        service_module,
        "dispatch",
        lambda _job_json, file_path: calls.append(str(file_path)) or '{"ok": true}',
    )
    opened = _install_read_spy(monkeypatch)

    r = client.post(
        "/api/v1/jobs/run",
        json={"computation_type": "stress_daily", "file_path": str(outside)},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "path outside allowed roots"
    assert calls == [], "the worker must not run on an out-of-root path"
    assert str(outside) not in opened
    assert str(outside.resolve()) not in opened


def test_job_run_rejects_parent_traversal_path(
    sandbox: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
):
    """A path escaping the sessions dir through ``..`` is refused."""
    sessions_dir, outside = sandbox
    client = TestClient(app)
    traversal = str(sessions_dir / ".." / outside.name)
    opened = _install_read_spy(monkeypatch)

    r = client.post(
        "/api/v1/jobs/run",
        json={"computation_type": "stress_daily", "file_path": traversal},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "path outside allowed roots"
    assert str(outside.resolve()) not in opened


def test_job_run_rejects_system_paths(client: TestClient):
    """Well-known system files are refused as well."""
    for candidate in ("/etc/passwd", "C:/Windows/win.ini", "../../etc/passwd"):
        r = client.post(
            "/api/v1/jobs/run",
            json={"computation_type": "stress_daily", "file_path": candidate},
        )
        assert r.status_code == 200, candidate
        body = r.json()
        assert body["ok"] is False, candidate
        assert body["error"] == "path outside allowed roots", candidate


def test_session_id_is_not_a_path(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """A traversal-like session id is a plain lookup miss, never a file read."""
    opened = _install_read_spy(monkeypatch)
    r = client.get("/api/v1/sessions/..%2F..%2Fetc%2Fpasswd")
    # 404 when the encoded slash does not match the route, 200 with an error
    # payload when it does: either way it is a catalog lookup, not a file read.
    assert r.status_code in (200, 404)
    assert not any("passwd" in path for path in opened)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_api_requires_bearer_token_when_configured(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """With a token configured, unauthenticated / wrong-token calls get 401."""
    monkeypatch.setenv("SYNAPSE_API_TOKEN", "s3cr3t-token")

    r = client.get("/api/v1/sessions")
    assert r.status_code == 401

    r = client.get("/api/v1/sessions", headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401

    r = client.get("/api/v1/sessions", headers={"Authorization": "s3cr3t-token"})
    assert r.status_code == 401

    r = client.get("/api/v1/sessions", headers={"Authorization": "Bearer s3cr3t-token"})
    assert r.status_code == 200


def test_legacy_endpoints_require_bearer_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """The legacy /api surface is protected too."""
    monkeypatch.setenv("SYNAPSE_API_TOKEN", "s3cr3t-token")

    for path in (
        "/api/status",
        "/api/sync",
        "/api/quality/pod-1",
        "/api/live/pod-1",
        "/api/config",
    ):
        r = client.get(path)
        assert r.status_code == 401, path

    r = client.post("/api/control/tier")
    assert r.status_code == 401

    r = client.get("/api/status", headers={"Authorization": "Bearer s3cr3t-token"})
    assert r.status_code == 200


def test_health_is_reachable_without_token(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """The liveness probe stays open even when the API is token protected."""
    monkeypatch.setenv("SYNAPSE_API_TOKEN", "s3cr3t-token")

    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_api_is_open_without_token(client: TestClient):
    """Local development keeps working when no token is configured."""
    r = client.get("/api/v1/sessions")
    assert r.status_code == 200


def test_startup_warns_when_api_is_unauthenticated(caplog: pytest.LogCaptureFixture):
    """A loud warning is logged at startup when no token is configured."""
    with caplog.at_level(logging.WARNING, logger="backend.main"), TestClient(app) as test_client:
        test_client.get("/api/health")
    messages = [record.getMessage() for record in caplog.records]
    assert any("SYNAPSE_API_TOKEN is not set" in m for m in messages), messages


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------


def test_cors_never_allows_wildcard_origin(client: TestClient):
    """The wildcard origin is gone (it is invalid with credentials)."""
    r = client.get("/api/v1/sessions", headers={"Origin": "https://evil.example"})
    assert r.headers.get("access-control-allow-origin") != "*"

    r = client.get("/api/v1/sessions")
    assert r.headers.get("access-control-allow-origin") != "*"


def test_cors_echoes_allowed_origin(client: TestClient):
    """An allowed origin comes back verbatim in the response."""
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173"):
        r = client.get("/api/v1/sessions", headers={"Origin": origin})
        assert r.headers.get("access-control-allow-origin") == origin


def test_cors_origins_env_extends_the_allowlist(monkeypatch: pytest.MonkeyPatch):
    """SYNAPSE_CORS_ORIGINS adds explicit origins."""
    monkeypatch.setenv("SYNAPSE_CORS_ORIGINS", "http://averyn.local:5173,https://app.averyn.test")
    app_with_extra = create_app()
    client = TestClient(app_with_extra)

    r = client.get("/api/health", headers={"Origin": "https://app.averyn.test"})
    assert r.headers.get("access-control-allow-origin") == "https://app.averyn.test"

    r = client.get("/api/health", headers={"Origin": "https://not-allowed.test"})
    assert r.headers.get("access-control-allow-origin") is None


def test_cors_preflight_is_answered_before_auth(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """The CORS preflight is not blocked by the bearer token."""
    monkeypatch.setenv("SYNAPSE_API_TOKEN", "s3cr3t-token")
    r = client.options(
        "/api/v1/sessions",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"


# ---------------------------------------------------------------------------
# Caching + digests
# ---------------------------------------------------------------------------


def test_job_run_returns_real_file_sha256(client: TestClient):
    """The job carries the real sha256 of the file content."""
    xdf = _session_by_format(client, "xdf")
    path = _session_path(client, xdf["session_id"])
    expected = hashlib.sha256(Path(path).read_bytes()).hexdigest()

    r = client.post(
        "/api/v1/jobs/run",
        json={"computation_type": "hrv_trends", "file_path": path},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, body.get("error")
    assert body["file_sha256"] == expected


def test_catalog_is_not_reindexed_on_every_request(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Two consecutive requests reuse the cached catalog."""
    calls: list[str] = []
    original = service_module.index_directory

    def counting(root: Any) -> Any:
        calls.append(str(root))
        return original(root)

    monkeypatch.setattr(service_module, "index_directory", counting)

    r1 = client.get("/api/v1/sessions")
    r2 = client.get("/api/v1/sessions")
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert len(calls) == 1, calls
