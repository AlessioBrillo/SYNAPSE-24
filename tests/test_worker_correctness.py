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
from datetime import UTC, datetime, timedelta
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


# --- review follow-ups (PR #59) -------------------------------------------------------------------


def test_a_trailing_stress_episode_ends_at_the_last_window_not_the_last_label(
    tmp_path: Path,
) -> None:
    # unknown, stress, stress: two labelled windows, but the episode ends at window index 2
    fit = _fit(tmp_path, "tail.fit", [{FIELD_STRESS: v} for v in (3, 1, 1)])
    result = run_stress_daily(_make_job(JobType.STRESS_DAILY, str(fit)), fit)
    assert result.ok is True
    assert result.payload["stress_episodes"] == [{"start": 1, "end": 2, "len": 2}]


def test_a_value_outside_the_stress_enum_is_not_a_label(tmp_path: Path) -> None:
    fit = _fit(tmp_path, "future.fit", [{FIELD_STRESS: v} for v in (0, 4, 4, 9)])
    result = run_stress_daily(_make_job(JobType.STRESS_DAILY, str(fit)), fit)
    assert result.ok is True
    p = result.payload
    assert p["n_records"] == 1
    assert p["baseline_prob"] + p["stress_prob"] + p["artifact_prob"] == pytest.approx(1.0)
    only_unexpected = _fit(tmp_path, "only.fit", [{FIELD_STRESS: 4}] * 3)
    result = run_stress_daily(
        _make_job(JobType.STRESS_DAILY, str(only_unexpected)), only_unexpected
    )
    assert result.ok is False
    assert result.error is not None
    assert "insufficient_data" in result.error


def test_the_fit_offset_comes_from_the_sdk_constant() -> None:
    from synapse_device.fit import FIT_EPOCH

    assert (
        datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=hrv_worker.FIT_TO_UNIX_OFFSET_S)
        == FIT_EPOCH
    )


def test_a_record_without_a_timestamp_is_left_out_of_the_timed_payloads() -> None:
    kept = hrv_worker._timed([{"timestamp": 10, "dev": {}}, {"timestamp": None, "dev": {}}])
    assert [r["timestamp"] for r in kept] == [10]


def test_an_unscored_epoch_carries_no_confidence() -> None:
    class _Staging:
        hypnogram = np.array([2, 9])
        confidence = np.array([0.9, 0.8])

    # mirrors the payload expression in run_sleep_staging
    last = _Staging.hypnogram[-1]
    confidence = float(_Staging.confidence[-1]) if _stage_code(last) != SLEEP_STAGE_NA else None
    assert confidence is None
    source = (ROOT / "worker" / "hrv_worker.py").read_text(encoding="utf-8")
    assert "_stage_code(hypnogram[-1]) != SLEEP_STAGE_NA" in source


def test_a_clipped_window_is_analysed_over_its_real_duration() -> None:
    for path in (
        ROOT / "worker" / "hrv_worker.py",
        ROOT / "src" / "synapse24" / "export" / "__init__.py",
    ):
        source = path.read_text(encoding="utf-8")
        assert "window_duration_s=window_s" not in source, path.name
    # the worker no longer asks the LF/HF-aware helper: it works from the real beat instants
    # of the window (see test_hrv_windows below)
    assert "_window_hrv(win_times)" in (ROOT / "worker" / "hrv_worker.py").read_text(
        encoding="utf-8"
    )
    assert "window_duration_s=win_t1 - win_t0" in (
        ROOT / "src" / "synapse24" / "export" / "__init__.py"
    ).read_text(encoding="utf-8")


def test_an_unusable_sampling_rate_fails_the_job_instead_of_returning_nothing(
    tmp_path: Path,
) -> None:
    from synapse24.utils import write_xdf

    n = 200
    t = np.arange(n) / 4.0  # a 4 Hz stream: below any cardiac-band filter
    xdf = tmp_path / "slow.xdf"
    write_xdf(
        xdf,
        streams=[
            {
                "data": np.sin(t).reshape(-1, 1),
                "timestamps": t,
                "info": {
                    "name": "PPG",
                    "type": "PPG",
                    "nominal_srate": 4.0,
                    "channel_count": 1,
                    "channel_format": "double64",
                    "source_id": "syn-test",
                },
            }
        ],
    )
    result = run_hrv_trends(_make_job(JobType.HRV_TRENDS, str(xdf)), xdf)
    assert result.ok is False
    assert result.error is not None
    assert "too low" in result.error


def test_the_report_does_not_invent_a_transition_to_not_available() -> None:
    from synapse24.report.report import _events_from_jobs

    events = _events_from_jobs({"sleep_staging": {"hypnogram": [0, 255, 0, 2, 255, 2]}})
    transitions = [(e["from"], e["to"]) for e in events if e["kind"] == "sleep_stage_transition"]
    assert transitions == [(0, 2)]  # W -> N2 only: the unscored epochs are neither stage nor change


def test_a_sleep_stage_line_that_still_says_wake_light_deep_is_rejected(tmp_path: Path) -> None:
    stale = tmp_path / "fit.py"
    text = ci.SDK_FIT_MODULE.read_text(encoding="utf-8")
    stale.write_text(
        text + "\n# synapse_sleep_stage: wake/light/deep (old meaning)\n", encoding="utf-8"
    )
    report = ci.Report()
    ci.check_sleep_stage_semantics(report, _descriptor(), ci.FIRMWARE_FIT_HEADER, stale)
    assert any("wake/light/deep" in f for f in report.failures), report.failures


def test_the_mapping_in_another_fields_comment_does_not_satisfy_the_check(tmp_path: Path) -> None:
    decoy = tmp_path / "fit_writer.h"
    decoy.write_text("// some_other_field: 0=W, 1=N1, 2=N2, 3=N3, 4=REM\n", encoding="utf-8")
    report = ci.Report()
    ci.check_sleep_stage_semantics(report, _descriptor(), decoy, ci.SDK_FIT_MODULE)
    assert any("firmware fit_writer.h" in f for f in report.failures), report.failures


# --- MVP-1.4 S9: HRV from the real instants of the beats ---------------------------------------------


def _beats(rr_ms: list[float], start: float = 100.0) -> np.ndarray:
    return start + np.concatenate([[0.0], np.cumsum(rr_ms)]) / 1000.0


def test_two_beats_give_no_hrv_not_a_nan() -> None:
    assert hrv_worker._window_hrv(_beats([800.0])) == {}  # one interval: no SDNN, no RMSSD
    assert hrv_worker._window_hrv(np.array([])) == {}


def test_a_steady_heart_gives_its_numbers() -> None:
    hrv = hrv_worker._window_hrv(_beats([1000.0, 1010.0, 990.0, 1000.0, 1020.0, 980.0]))
    assert hrv["mean_rr_ms"] == pytest.approx(1000.0)
    assert hrv["hr_mean_bpm"] == pytest.approx(60.0)
    assert 10.0 < hrv["sdnn_ms"] < 25.0
    assert 15.0 < hrv["rmssd_ms"] < 40.0
    assert all(np.isfinite(v) for v in hrv.values())


def test_an_ectopic_beat_does_not_inflate_the_variability() -> None:
    steady = [1000.0] * 9
    with_ectopic = [1000.0, 1000.0, 1000.0, 520.0, 1480.0, 1000.0, 1000.0, 1000.0, 1000.0, 1000.0]
    clean = hrv_worker._window_hrv(_beats(steady))
    dirty = hrv_worker._window_hrv(_beats(with_ectopic))
    assert dirty["sdnn_ms"] == pytest.approx(clean["sdnn_ms"], abs=1.0)
    assert dirty["rmssd_ms"] == pytest.approx(clean["rmssd_ms"], abs=1.0)


def test_a_window_that_is_mostly_artefact_is_not_analysed() -> None:
    assert hrv_worker._window_hrv(_beats([300.0, 2400.0, 400.0, 2300.0, 350.0, 2450.0])) == {}


def test_a_gap_in_the_recording_is_a_long_interval_not_a_quiet_stretch() -> None:
    # 4 clean beats, 8 s with no data, 4 more: the interval across the gap is 8000 ms
    times = np.concatenate([_beats([1000.0] * 3), _beats([1000.0] * 3, start=111.0)])
    hrv = hrv_worker._window_hrv(times)
    assert hrv["mean_rr_ms"] == pytest.approx(1000.0)  # the gap interval was rejected, not averaged


def test_the_numbers_are_json_safe() -> None:
    import json

    for rr in ([1000.0, 1000.0], [800.0, 900.0, 1000.0], [1000.0] * 12):
        json.dumps(hrv_worker._window_hrv(_beats(rr)), allow_nan=False)


def _xdf_with(tmp_path: Path, name: str, columns: list[np.ndarray], t: np.ndarray) -> Path:
    from synapse24.utils import write_xdf

    path = tmp_path / f"{name}.xdf"
    data = np.stack(columns, axis=1)
    write_xdf(
        path,
        streams=[
            {
                "data": data,
                "timestamps": t,
                "info": {
                    "name": name,
                    "type": name,
                    "nominal_srate": 50.0,
                    "channel_count": data.shape[1],
                    "channel_format": "float32",
                    "source_id": "test",
                },
            }
        ],
    )
    return path


def test_the_ppg_analysis_uses_the_infrared_channel(tmp_path: Path) -> None:
    fs, seconds = 50.0, 90
    t = np.arange(int(fs * seconds)) / fs
    pulse = 0.5 * (1.0 - np.cos(2.0 * np.pi * t * 1.2))  # 72 bpm
    noise = np.random.RandomState(3).normal(0.0, 1.0, t.size)
    red = 100_000 + 2.0 * noise  # no pulse at all in this channel
    ir = 120_000 - 6_000 * pulse  # reflective PPG: the count falls on the pulse
    path = _xdf_with(tmp_path, "PPG", [red, ir], t)
    windows = hrv_worker.read_xdf_hrv(path, window_s=30.0)
    rates = [w["hr_mean_bpm"] for w in windows if w["hr_mean_bpm"] is not None]
    assert rates, "no window was analysed: the red channel was used"
    assert np.mean(rates) == pytest.approx(72.0, abs=4.0)


def test_beat_times_follow_the_timestamps_across_a_gap(tmp_path: Path) -> None:
    """Samples missing from the recording must not shift the later beats earlier in time."""
    fs = 50.0
    t = np.arange(int(fs * 120)) / fs
    keep = ~((t >= 40.0) & (t < 50.0))  # a 10 s hole
    t, pulse_t = t[keep], t[keep]
    ir = 120_000 - 6_000 * 0.5 * (1.0 - np.cos(2.0 * np.pi * pulse_t))  # 60 bpm
    path = _xdf_with(tmp_path, "PPG", [ir, ir], t)
    windows = hrv_worker.read_xdf_hrv(path, window_s=30.0)
    by_start = {round(w["window_start"] - t[0]): w for w in windows}
    gap_window = by_start[30]  # covers 30-60 s: 10 s of it is empty
    # 20 s of beats at 60 bpm, then the hole: the heart rate is still 60, not distorted by the gap
    if gap_window["hr_mean_bpm"] is not None:
        assert gap_window["hr_mean_bpm"] == pytest.approx(60.0, abs=3.0)
    assert by_start[60]["hr_mean_bpm"] == pytest.approx(60.0, abs=3.0)


def test_the_worker_image_carries_the_schemas_it_validates_against() -> None:
    dockerfile = (ROOT / "worker" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY contracts ./contracts" in dockerfile


def test_a_missing_schema_is_reported_not_skipped_in_silence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    monkeypatch.setattr(hrv_worker, "_WORKER_SCHEMA_PATH", tmp_path / "absent.json")
    hrv_worker._load_worker_schema.cache_clear()
    try:
        with caplog.at_level("WARNING"):
            errors = hrv_worker._validate_payload(JobType.SQI_REPORT, {"anything": 1})
    finally:
        hrv_worker._load_worker_schema.cache_clear()
    assert errors == []
    assert "NOT validated" in caplog.text
