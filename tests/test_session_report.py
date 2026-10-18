"""Tests for SessionReport v1 (synapse24.report).

Conformance to contracts/schemas/session_report.json plus CLI behaviour,
built on synthetic catalog records — no hardware, no dataset downloads.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from synapse24.report import build_session_report, report_quality_note  # noqa: E402
from synapse24.sessions import (  # noqa: E402
    SessionCatalog,
    SessionRecord,
    StreamInfo,
    index_directory,
    load_catalog,
    save_catalog,
)
from synapse24.utils import write_xdf  # noqa: E402


def _make_record(tmp_path: Path, fmt: str = "xdf", valid: bool = True) -> SessionRecord:
    if fmt == "xdf":
        xdf = tmp_path / "s.xdf"
        t = np.arange(100) / 50.0
        write_xdf(
            xdf,
            streams=[
                {
                    "data": np.random.randn(100, 1).astype(np.float64),
                    "timestamps": t,
                    "info": {"name": "ECG", "type": "ECG", "nominal_srate": 50.0},
                }
            ],
        )
        from synapse24.sessions import index_xdf  # noqa: PLC0415

        return index_xdf(xdf)
    fit = tmp_path / "s.fit"
    fit_hdr = bytearray(14)
    fit_hdr[0] = 14
    fit_hdr[8:12] = b".FIT"
    fit_bytes = bytes(fit_hdr) + b"\x00\x00"
    if not valid:
        fit_bytes = b"NOT-A-FIT-FILE"
    fit.write_bytes(fit_bytes)
    from synapse24.sessions import index_fit  # noqa: PLC0415

    return index_fit(fit)


def _make_jobs() -> dict[str, dict]:
    return {
        "stress_daily": {
            "stress_level": 1,
            "stress_prob": 0.4,
            "baseline_prob": 0.5,
            "artifact_prob": 0.1,
            "window_s": 8.0,
            "daily_average": 40.0,
            "peak_stress": 1.0,
            "recovery_periods": 2,
            "stress_episodes": [
                {"start": 2, "end": 4, "len": 3},
                {"start": 7, "end": 7, "len": 1},
            ],
            "source": "fit-file",
        },
        "sleep_staging": {
            "sleep_stage": 2,
            "confidence": 0.8,
            "epoch_s": 30.0,
            "hypnogram": [0, 0, 1, 2, 2, 1, 0],
            "input_modality": "eeg",
            "aux_channels_used": [],
            "source": "xdf-file",
            "n_epochs": 7,
        },
    }


def _assert_report_conforms(report: dict) -> None:
    """Lightweight conformance to contracts/schemas/session_report.json."""
    assert report["report_version"] == "session-report-v1"
    assert isinstance(report["session_id"], str) and report["session_id"]
    assert isinstance(report["device_serial"], str)
    assert report["format"] in ("xdf", "fit")
    assert set(report["window"].keys()) >= {"started_at", "ended_at", "duration_s"}
    assert isinstance(report["streams"], list)
    for s in report["streams"]:
        assert set(s.keys()) >= {"name", "type", "sampling_rate_hz", "n_samples", "unit"}
    assert isinstance(report["n_samples_total"], int)
    dec = report["decimation"]
    assert set(dec.keys()) >= {"factor", "policy", "applied_to"}
    assert dec["policy"] == "min_max_envelope"
    assert isinstance(report["metrics"], dict)
    assert isinstance(report["events"], list)
    q = report["quality"]
    assert q["overall"] in ("good", "warning", "poor")
    assert "raw_ref" in report and "sha256" in report["raw_ref"]
    assert isinstance(report["generated_at"], str)


def test_build_session_report_structure(tmp_path: Path) -> None:
    record = _make_record(tmp_path, "xdf", valid=True)
    report = build_session_report(record, _make_jobs())
    _assert_report_conforms(report)
    assert report["device_serial"] == record.device_serial
    assert report["format"] == "xdf"
    assert report["n_samples_total"] == record.n_samples_total
    assert report["metrics"]["stress_daily"]["daily_average"] == 40.0
    assert report["metrics"]["sleep_staging"]["n_epochs"] == 7


def test_quality_good_when_valid(tmp_path: Path) -> None:
    record = _make_record(tmp_path, "xdf", valid=True)
    report = build_session_report(record)
    assert report["quality"]["overall"] == "good"


def test_quality_poor_when_invalid_fit(tmp_path: Path) -> None:
    record = _make_record(tmp_path, "fit", valid=False)
    report = build_session_report(record)
    assert report["quality"]["overall"] == "poor"


def test_events_from_jobs(tmp_path: Path) -> None:
    record = _make_record(tmp_path, "xdf", valid=True)
    report = build_session_report(record, _make_jobs())
    kinds = [e["kind"] for e in report["events"]]
    assert "stress_episode" in kinds
    assert "sleep_stage_transition" in kinds
    # stress_episodes carried through
    stress_events = [e for e in report["events"] if e["kind"] == "stress_episode"]
    assert len(stress_events) == 2


def test_report_quality_note_mutates() -> None:
    report = {"quality": {"notes": []}}
    report_quality_note(report, "low SQI on PPG")
    assert "low SQI on PPG" in report["quality"]["notes"]


def test_cli_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_record(tmp_path, "xdf", valid=True)
    catalog_file = tmp_path / "catalog.json"
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps(_make_jobs()), encoding="utf-8")

    save_catalog(index_directory(tmp_path), catalog_file)
    record = next(iter(load_catalog(catalog_file).sessions.values()))

    from synapse24.report.__main__ import main  # noqa: E402,PLC0415

    old_argv = sys.argv
    sys.argv = [
        "synapse-report",
        str(catalog_file),
        record.session_id,
        "--jobs",
        str(jobs_file),
    ]
    try:
        rc = main()
    finally:
        sys.argv = old_argv
    assert rc == 0

    # --out writes a file
    out_file = tmp_path / "report.json"
    sys.argv = [
        "synapse-report",
        str(catalog_file),
        record.session_id,
        "--jobs",
        str(jobs_file),
        "--out",
        str(out_file),
    ]
    try:
        rc = main()
    finally:
        sys.argv = old_argv
    assert rc == 0
    written = json.loads(out_file.read_text(encoding="utf-8"))
    _assert_report_conforms(written)
    assert written["report_version"] == "session-report-v1"

    # unknown session -> non-zero
    sys.argv = ["synapse-report", str(catalog_file), "syn-nonexistent"]
    try:
        rc = main()
    finally:
        sys.argv = old_argv
    assert rc == 1
