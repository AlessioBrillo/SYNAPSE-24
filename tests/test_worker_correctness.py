"""Worker / export data-correctness fixes (MVP-1.3 PR-E1).

Each test pins a defect the audit found and that the existing golden tests did not see:

* ``stress_daily`` counted "unknown" (the triage did not run) as a real label;
* ``window_start`` was FIT-epoch seconds for a FIT input and Unix/LSL seconds for an XDF one;
* the export and the worker counted HRV windows differently (``ceil`` vs ``floor``);
* the XDF HRV truncated the sample rate and ran the ECG R-peak detector on a PPG trace;
* the sleep-stage enum had three meanings, and a classifier's "unscored" (9) overflowed the schema;
* ``sqi_report`` / ``sleep_staging`` returned nulls that the schema rejected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

import check_interfaces as ci  # noqa: E402

from synapse24.sessions.catalog import _FIT_EPOCH_OFFSET_S  # noqa: E402
from synapse24.signal_quality.ecg import compute_hrv_metrics  # noqa: E402
from synapse24.signal_quality.ppg import detect_ppg_peaks  # noqa: E402
from synapse24.utils.windows import window_bounds  # noqa: E402
from tests.test_worker_jobs import _make_job, write_dev_data_fit  # noqa: E402
from worker import hrv_worker  # noqa: E402
from worker.hrv_worker import (  # noqa: E402
    SLEEP_STAGE_NA,
    _fit_to_unix,
    _stage_code,
    _validate_payload,
    run_hrv_trends,
    run_sqi_report,
    run_stress_daily,
)
from worker.job_contract import JobType  # noqa: E402

FIELD_STRESS, FIELD_SQI, FIELD_MAP = 0, 1, 2


def _fit(tmp_path: Path, name: str, rows: list[dict[int, int]], base: int = 1000) -> Path:
    path = tmp_path / name
    write_dev_data_fit(path, [{"timestamp": base + i, "dev": dev} for i, dev in enumerate(rows)])
    return path


# --- stress_daily ---------------------------------------------------------------------


def test_a_file_whose_windows_are_all_unknown_is_not_reported_as_baseline(tmp_path: Path) -> None:
    fit = _fit(tmp_path, "unknown.fit", [{FIELD_STRESS: 3}] * 5)
    result = run_stress_daily(_make_job(JobType.STRESS_DAILY, str(fit)), fit)
    assert result.ok is False
    assert result.error is not None
    assert "insufficient_data" in result.error


def test_unknown_windows_are_excluded_from_the_probabilities(tmp_path: Path) -> None:
    rows = [{FIELD_STRESS: v} for v in (0, 3, 1, 3, 3, 1)]
    fit = _fit(tmp_path, "mixed.fit", rows)
    result = run_stress_daily(_make_job(JobType.STRESS_DAILY, str(fit)), fit)
    assert result.ok is True
    p = result.payload
    assert p["n_records"] == 3  # only the labelled windows
    assert p["stress_prob"] == pytest.approx(2 / 3, abs=1e-4)
    assert p["baseline_prob"] == pytest.approx(1 / 3, abs=1e-4)
    assert _validate_payload(JobType.STRESS_DAILY, p) == []


def test_an_unknown_window_breaks_a_run_instead_of_bridging_it(tmp_path: Path) -> None:
    # baseline, baseline, UNKNOWN, baseline: three baselines, but not three in a row
    fit = _fit(tmp_path, "gap.fit", [{FIELD_STRESS: v} for v in (0, 0, 3, 0)])
    result = run_stress_daily(_make_job(JobType.STRESS_DAILY, str(fit)), fit)
    assert result.ok is True
    assert result.payload["recovery_periods"] == 0


# --- one clock for every window_start --------------------------------------------------------


def test_the_fit_epoch_offset_is_the_one_the_catalog_uses() -> None:
    assert hrv_worker.FIT_TO_UNIX_OFFSET_S == _FIT_EPOCH_OFFSET_S
    assert _fit_to_unix(10) == 10 + _FIT_EPOCH_OFFSET_S
    assert _fit_to_unix(None) is None


def test_window_start_is_unix_seconds_for_a_fit_input(tmp_path: Path) -> None:
    base = 1_100_000_000  # a FIT timestamp in 2024: seconds since 1989-12-31
    fit = _fit(tmp_path, "t.fit", [{FIELD_STRESS: 0, FIELD_SQI: 200, FIELD_MAP: 10}] * 2, base)
    for runner in (run_hrv_trends, run_sqi_report):
        job_type = JobType.HRV_TRENDS if runner is run_hrv_trends else JobType.SQI_REPORT
        result = runner(_make_job(job_type, str(fit)), fit)
        key = "windows" if runner is run_hrv_trends else "per_channel"
        starts = [w["window_start"] for w in result.payload[key]]
        assert starts == [base + _FIT_EPOCH_OFFSET_S, base + 1 + _FIT_EPOCH_OFFSET_S]
        assert all(s > 1.7e9 for s in starts)  # 2024 in Unix seconds, not seconds-since-1989


# --- the payloads the worker produces are schema-valid ------------------------------------------------


def test_an_sqi_report_with_no_valid_window_validates_with_nulls(tmp_path: Path) -> None:
    fit = _fit(tmp_path, "invalid.fit", [{}] * 3)  # every field at its invalid sentinel
    result = run_sqi_report(_make_job(JobType.SQI_REPORT, str(fit)), fit)
    assert result.ok is True
    assert result.payload["sqi"] is None
    assert result.payload["map"] is None
    assert _validate_payload(JobType.SQI_REPORT, result.payload) == []


def test_a_sleep_staging_payload_with_an_unscored_epoch_validates() -> None:
    payload = {
        "sleep_stage": SLEEP_STAGE_NA,
        "confidence": None,
        "epoch_s": 30.0,
        "hypnogram": [0, 1, 4, SLEEP_STAGE_NA],
        "input_modality": "eeg",
        "source": "xdf-file",
    }
    assert _validate_payload(JobType.SLEEP_STAGING, payload) == []
    payload["hypnogram"] = [0, 9]  # 9 was the classifier's "unscored" code: not a contract value
    assert _validate_payload(JobType.SLEEP_STAGING, payload) != []


@pytest.mark.parametrize(
    ("raw", "expected"), [(0, 0), (1, 1), (2, 2), (3, 3), (4, 4), (9, 255), (-1, 255)]
)
def test_a_classifier_stage_code_maps_to_the_contract(raw: int, expected: int) -> None:
    assert _stage_code(raw) == expected


# --- the sleep-stage meaning is one thing ---------------------------------------------------------------


def _descriptor() -> dict:
    import json

    return json.loads(
        (ROOT / "contracts" / "fit_developer_fields.json").read_text(encoding="utf-8")
    )


def test_the_committed_sleep_stage_contract_is_consistent() -> None:
    report = ci.Report()
    ci.check_sleep_stage_semantics(report, _descriptor(), ci.FIRMWARE_FIT_HEADER, ci.SDK_FIT_MODULE)
    assert not report.failures, report.failures


def test_the_old_wake_light_deep_enum_is_rejected() -> None:
    descriptor = _descriptor()
    field = next(f for f in descriptor["fields"] if f["name"] == "synapse_sleep_stage")
    field["enum"] = {"0": "wake", "1": "light", "2": "deep", "3": "rem", "4": "unknown"}
    report = ci.Report()
    ci.check_sleep_stage_semantics(report, descriptor, ci.FIRMWARE_FIT_HEADER, ci.SDK_FIT_MODULE)
    assert any("ADR-0014" in f for f in report.failures), report.failures


def test_a_sdk_that_states_another_meaning_is_rejected(tmp_path: Path) -> None:
    stale = tmp_path / "fit.py"
    stale.write_text(
        ci.SDK_FIT_MODULE.read_text(encoding="utf-8").replace(
            "0=W 1=N1 2=N2 3=N3 4=REM", "0=wake 1=light 2=deep 3=rem 4=unknown"
        ),
        encoding="utf-8",
    )
    report = ci.Report()
    ci.check_sleep_stage_semantics(report, _descriptor(), ci.FIRMWARE_FIT_HEADER, stale)
    assert any("sdk fit.py" in f for f in report.failures), report.failures


# --- windows --------------------------------------------------------------------------------------------------


@given(
    t0=st.floats(0, 1e6),
    duration=st.floats(0, 5000),
    window=st.floats(1, 600),
)
def test_windows_tile_the_recording_without_gaps_or_overlap(
    t0: float, duration: float, window: float
) -> None:
    t1 = t0 + duration
    bounds = window_bounds(t0, t1, window)
    assert bounds[0][0] == t0
    assert bounds[-1][1] == pytest.approx(t1)
    for (_, end), (start, _) in zip(bounds, bounds[1:], strict=False):
        assert start == pytest.approx(end)
    assert all(a <= b for a, b in bounds)


def test_an_exact_multiple_does_not_gain_a_window_to_rounding_noise() -> None:
    assert len(window_bounds(0.0, 600.0000000001, 300.0)) == 2
    assert len(window_bounds(0.0, 600.0, 300.0)) == 2
    assert len(window_bounds(0.0, 601.0, 300.0)) == 3  # a real tail is still a window
    assert len(window_bounds(0.0, 10.0, 300.0)) == 1  # shorter than one window: one clipped window


def test_the_window_rule_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="positive"):
        window_bounds(0.0, 10.0, 0.0)
    with pytest.raises(ValueError, match="before"):
        window_bounds(10.0, 0.0, 30.0)


def test_the_export_and_the_worker_share_the_window_rule() -> None:
    for path in (
        ROOT / "src" / "synapse24" / "export" / "__init__.py",
        ROOT / "worker" / "hrv_worker.py",
    ):
        assert "window_bounds(" in path.read_text(encoding="utf-8"), path.name


# --- PPG: its own detector ---------------------------------------------------------------------------------------


def _ppg(fs: int, seconds: int, hr_bpm: float, seed: int = 3) -> np.ndarray:
    t = np.arange(fs * seconds) / fs
    f = hr_bpm / 60.0
    pulse = np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t + 0.6)
    noise = np.random.default_rng(seed).normal(0, 0.03, t.size)
    return 5000.0 + 800.0 * pulse + noise


def test_the_ppg_detector_finds_one_peak_per_beat() -> None:
    fs, seconds, hr = 50, 60, 72.0
    peaks = detect_ppg_peaks(_ppg(fs, seconds, hr), fs)
    assert abs(len(peaks) - hr * seconds / 60.0) <= 2
    rr_ms = np.diff(peaks) / fs * 1000.0
    assert np.median(rr_ms) == pytest.approx(60000.0 / hr, rel=0.05)


def test_hrv_from_ppg_peaks_is_physiological() -> None:
    fs = 50
    peaks = detect_ppg_peaks(_ppg(fs, 120, 60.0), fs)
    metrics = compute_hrv_metrics(peaks, fs, window_duration_s=120.0)
    assert metrics["hr_mean_bpm"] == pytest.approx(60.0, abs=3.0)


@pytest.mark.parametrize("signal", [np.zeros(500), np.array([1.0]), np.full(500, 7.0)])
def test_the_ppg_detector_returns_nothing_for_a_flat_or_tiny_trace(signal: np.ndarray) -> None:
    assert len(detect_ppg_peaks(signal, 50)) == 0
