"""Tests for the Synapse stateless inference worker (worker/hrv_worker.py).

Deterministic synthetic FIT / XDF fixtures — no hardware, no dataset
downloads. Payload shapes match worker/job_contract.py and
contracts/schemas/worker_job_result.json.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

# Ensure the repo src is importable (worker/hrv_worker.py does the same).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from synapse24.utils import write_xdf  # noqa: E402
from worker.hrv_worker import (  # noqa: E402
    dispatch,
    run_sleep_staging,
    run_sqi_report,
    run_stress_daily,
)
from worker.job_contract import (  # noqa: E402
    Job,
    JobType,
)

# ---- FIT test fixture writer (canonical writer: synapse_device.fit) --------


def write_dev_data_fit(path: Path, records: list[dict]) -> None:
    """Write a FIT file with Synapse developer data, one record per entry.

    Each record: {"timestamp": int, "dev": {field_id: value}}. The bytes come
    from the canonical writer (:mod:`synapse_device.fit`), so the fixture is
    the same format the product writes — and the worker's reader is the
    canonical reader, not a second parser.
    """
    from synapse_device.fit import DEV_FIELDS, FitRecord, FitWriter

    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(serial_number=1)
    writer.write_developer_data_id()
    writer.write_field_descriptions()
    for rec in records:
        dev = {}
        for field in DEV_FIELDS:
            value = rec["dev"].get(field.field_id)
            dev[field.name] = int(value) if value is not None else field.invalid
        writer.write_record(FitRecord(timestamp=int(rec["timestamp"]), heart_rate=0, dev=dev))
    writer.finalize(path)


# ---- XDF test fixture writer ---------------------------------------------


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
                "info": {
                    "name": "EEG",
                    "type": "EEG",
                    "nominal_srate": fs,
                    "channel_count": 1,
                    "channel_format": "double64",
                    "source_id": "syn-test",
                },
            }
        ],
    )


# ---- Job helper -----------------------------------------------------------


def _make_job(computation_type: JobType, file_ref: str) -> Job:
    digest = hashlib.sha256(f"{file_ref}|{computation_type.value}|test-v1".encode()).hexdigest()[
        :16
    ]
    return Job(
        job_id=f"syn-{computation_type.value}-{digest}",
        computation_type=computation_type,
        algorithm_version="test-v1",
        file_ref=file_ref,
        file_sha256="placeholder",
    )


# ---- stress_daily ---------------------------------------------------------


def test_stress_daily_golden(tmp_path: Path) -> None:
    """Labels [0,0,1,1,1,0,0,0] → daily_average 37.5, one recovery period."""
    records = [{"timestamp": i, "dev": {0: v}} for i, v in enumerate([0, 0, 1, 1, 1, 0, 0, 0])]
    fit = tmp_path / "stress.fit"
    write_dev_data_fit(fit, records)

    result = run_stress_daily(_make_job(JobType.STRESS_DAILY, str(fit)), fit)
    assert result.ok is True
    p = result.payload
    assert p["n_records"] == 8
    assert p["daily_average"] == 37.5
    assert p["stress_prob"] == 0.375
    assert p["baseline_prob"] == 0.625
    assert p["artifact_prob"] == 0.0
    assert p["peak_stress"] == 1.0
    assert p["recovery_periods"] == 1
    assert p["stress_episodes"] == [{"start": 2, "end": 4, "len": 3}]
    assert result.algorithm_version == "stress-daily-py-v1"


def test_stress_daily_rejects_non_fit(tmp_path: Path) -> None:
    xdf = tmp_path / "sleep.xdf"
    write_eeg_xdf(xdf, n_seconds=60, fs=250.0)
    result = run_stress_daily(_make_job(JobType.STRESS_DAILY, str(xdf)), xdf)
    assert result.ok is False
    assert "requires a FIT file" in result.error


# ---- sqi_report -----------------------------------------------------------


def test_sqi_report_golden(tmp_path: Path) -> None:
    records = [{"timestamp": i, "dev": {1: 200, 2: 50, 3: 220}} for i in range(4)]
    fit = tmp_path / "sqi.fit"
    write_dev_data_fit(fit, records)

    result = run_sqi_report(_make_job(JobType.SQI_REPORT, str(fit)), fit)
    assert result.ok is True
    p = result.payload
    assert p["window_s"] == 3.0
    assert p["per_channel"] == [
        {"window_start": i, "sqi": 200, "map": 50, "ecg_quality": 220} for i in range(4)
    ]
    assert result.algorithm_version == "sqi-report-py-v1"


def test_sqi_report_rejects_non_fit(tmp_path: Path) -> None:
    xdf = tmp_path / "sleep.xdf"
    write_eeg_xdf(xdf, n_seconds=60, fs=250.0)
    result = run_sqi_report(_make_job(JobType.SQI_REPORT, str(xdf)), xdf)
    assert result.ok is False
    assert "requires a FIT file" in result.error


# ---- sleep_staging --------------------------------------------------------


def test_sleep_staging_eeg_only(tmp_path: Path) -> None:
    xdf = tmp_path / "sleep.xdf"
    write_eeg_xdf(xdf, n_seconds=300, fs=250.0)
    result = run_sleep_staging(_make_job(JobType.SLEEP_STAGING, str(xdf)), xdf)
    assert result.ok is True, result.error
    p = result.payload
    assert p["input_modality"] == "eeg"
    assert p["n_epochs"] > 0
    assert len(p["hypnogram"]) == p["n_epochs"]
    assert all(0 <= s <= 4 for s in p["hypnogram"])
    assert 0.0 <= p["confidence"] <= 1.0
    assert result.algorithm_version == "sleep-staging-yasa-py-v1"


def test_sleep_staging_rejects_no_eeg(tmp_path: Path) -> None:
    xdf = tmp_path / "ppg.xdf"
    t = np.arange(100) / 64.0
    write_xdf(
        xdf,
        streams=[
            {
                "data": np.random.randn(100, 1).astype(np.float64),
                "timestamps": t,
                "info": {"name": "PPG", "type": "PPG", "nominal_srate": 64.0},
            }
        ],
    )
    result = run_sleep_staging(_make_job(JobType.SLEEP_STAGING, str(xdf)), xdf)
    assert result.ok is False
    assert "no EEG stream" in result.error


def test_sleep_staging_rejects_non_xdf(tmp_path: Path) -> None:
    fit = tmp_path / "session.fit"
    write_dev_data_fit(fit, [{"timestamp": 0, "dev": {}}])
    result = run_sleep_staging(_make_job(JobType.SLEEP_STAGING, str(fit)), fit)
    assert result.ok is False
    assert "requires an XDF file" in result.error


# ---- dispatch -------------------------------------------------------------


def test_dispatch_routes_all_jobs(tmp_path: Path) -> None:
    """dispatch() returns ok JobResults for all four computation types."""
    fit = tmp_path / "session.fit"
    write_dev_data_fit(
        fit,
        [{"timestamp": i, "dev": {0: 1, 1: 200, 2: 50, 3: 220}} for i in range(3)],
    )
    xdf = tmp_path / "sleep.xdf"
    write_eeg_xdf(xdf, n_seconds=120, fs=250.0)

    cases = [
        (JobType.HRV_TRENDS, fit),
        (JobType.STRESS_DAILY, fit),
        (JobType.SQI_REPORT, fit),
        (JobType.SLEEP_STAGING, xdf),
    ]
    for ctype, fpath in cases:
        job = _make_job(ctype, str(fpath))
        out = dispatch(job.to_json(), fpath)
        res = json.loads(out)
        assert res["ok"] is True, res.get("error")
        assert "payload" in res
        assert res["algorithm_version"]

    # Unknown type → ok=False, never crashes.
    raw = json.dumps(
        {
            "job_id": "bogus",
            "computation_type": "does_not_exist",
            "algorithm_version": "v",
            "file_ref": str(fit),
            "file_sha256": "x",
        }
    )
    res = json.loads(dispatch(raw, fit))
    assert res["ok"] is False
    assert "invalid job payload" in res["error"]
