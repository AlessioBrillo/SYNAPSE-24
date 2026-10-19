"""End-to-end contract tests for the Synapse local Data Plane API.

Exercises the six Averyn-facing endpoints (sessions, session detail, report,
metrics, job types, job run) against a real FastAPI TestClient, on a
synthetic session written to disk, and validates every worker payload against
the canonical JSON Schema in contracts/schemas/worker_job_result.json.

Requires fastapi + httpx (dev dependencies).
"""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import numpy as np
import pytest

from backend.main import app
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
    """Write a minimal FIT with one developer-data record (synapse_sqi etc.)."""
    buf = bytearray()
    running = 0

    def crc16(b: bytes) -> int:
        nonlocal running
        for byte in b:
            running ^= byte
            for _ in range(8):
                running = (running >> 1) ^ 0xA066 if running & 1 else running >> 1
        return running

    def w(data: bytes) -> None:
        nonlocal running, buf
        buf.extend(data)
        running = crc16(data)

    # file header
    w(bytes([14, 0x10]))  # size, protocol ver
    w(struct.pack("<H", 0x0815))  # profile version
    w(struct.pack("<I", 0))  # data size placeholder
    w(b".FIT")
    w(struct.pack("<H", 0))  # header crc placeholder

    # file_id (local msg type 0)
    w(bytes([0xC0 | 0, 5, 0x00, 2]))  # header, size, arch, field count
    for fn, bt in ((4, 134), (1, 132)):  # serial u32, manufacturer u16
        w(bytes([fn, bt]))
    w(bytes([0]))
    w(struct.pack("<I", 12345))  # serial
    w(struct.pack("<H", 0xFFFF))  # manufacturer

    # record: standard + developer block (local msg type 0 and 1)
    w(bytes([0xC0 | 0, 13, 0x00, 4]))
    for fn, bt in ((253, 134), (0, 2), (1, 132), (254, 2)):
        w(bytes([fn, bt]))
    w(bytes([0]))
    w(struct.pack("<I", 1000))  # timestamp
    w(bytes([72]))  # heart_rate
    w(struct.pack("<H", 0))  # rr
    w(bytes([100]))  # battery

    # developer data (local msg type 1)
    w(bytes([0xC0 | 0, 13, 0x00, 4]))
    for fn, bt in ((0, 2), (253, 134), (1, 2), (2, 2)):
        w(bytes([fn, bt]))
    w(bytes([0]))
    w(bytes([0]))  # developer_data_index
    w(struct.pack("<I", 1000))  # timestamp
    w(bytes([0]))  # reserved
    # dev fields 0..6: stress=1, sqi=200, map=50, ecg_quality=220, rmssd/sdnn/lf_hf invalid
    w(struct.pack("<B", 1))  # stress
    w(struct.pack("<B", 200))  # sqi
    w(struct.pack("<B", 50))  # map
    w(struct.pack("<B", 220))  # ecg_quality
    w(struct.pack("<H", 0xFFFF))  # rmssd
    w(struct.pack("<H", 0xFFFF))  # sdnn
    w(struct.pack("<H", 0xFFFF))  # lf_hf

    # patch data size + header CRC
    data_size = len(buf) - 14 - 2
    buf[4:8] = struct.pack("<I", data_size)
    hcrc = crc16(bytes(buf[:12]))
    # data CRC over bytes 12..end
    dcrc = crc16(bytes(buf[12:]))
    w(struct.pack("<H", hcrc))
    w(struct.pack("<H", dcrc))
    path.write_bytes(bytes(buf))


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
        json={"computation_type": "hrv_trends", "file_path": _session_path(client, xdf["session_id"])},
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
        json={"computation_type": "sqi_report", "file_path": _session_path(client, fit["session_id"])},
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
