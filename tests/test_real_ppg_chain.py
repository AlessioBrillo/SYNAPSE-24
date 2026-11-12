"""MVP-1.8: a real finger PPG through the firmware's beat-to-beat detector (``synapse_rr.c``).

``tests/fixtures/bidmc_ppg_150s.npz`` holds 150 s of BIDMC records 01, 02 and 03 (PhysioNet, Open Data
Commons Attribution licence; PLETH and ECG lead II at 125 Hz, cut without processing). The ECG gives the
reference beats, the PLETH is turned into the reflective 50 Hz IR counts the band would see.

What this shows: the detector, written and tuned on synthetic pulses, finds the right heart rate on real
pulses. What it does not show: a MAX30102 on a wrist (BIDMC is a transmissive oximeter on bed-ridden ICU
patients). Record 02 is kept as a *characterisation*: the pulse-to-pulse RMSSD of the PPG there is about
5x the ECG's. It is not the firmware's filters: an offline zero-phase detector (peak or max-upslope, at 50
or 125 Hz) gives ~40 ms as well, so the PRV of that pulse really differs from the ECG HRV (cause not
established). That is why ``CONFIG_SYNAPSE_RR_ENABLE`` stays off until a wrist bench run against a
reference. A skip here is not a pass.
"""

from __future__ import annotations

import ctypes
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import real_ppg_replay as rp  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "bidmc_ppg_150s.npz"
RECORDS = ["01", "02", "03"]


@pytest.fixture(scope="module")
def lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    d = rp.load_rr(tmp_path_factory.mktemp("realppg"))
    if d is None:
        pytest.skip("OS policy blocked loading the freshly built library; rerun in the Linux gate")
    return d


@pytest.fixture(scope="module")
def results(lib: ctypes.CDLL) -> dict[str, dict]:
    z = np.load(FIXTURE)
    return {
        r: rp.replay_ppg(z[f"pleth_{r}"].astype(np.float64), z[f"ecg_{r}"].astype(np.float64), lib)
        for r in RECORDS
    }


@pytest.mark.parametrize("record", RECORDS)
def test_the_heart_rate_is_right_on_a_real_pulse(results, record) -> None:
    r = results[record]
    assert r["windows_compared"] >= 4
    assert r["windows_band_silent"] == 0
    assert r["hr_within_5bpm"] == 1.0, r
    assert r["hr_median_abs_error_bpm"] < 2.0, r


@pytest.mark.parametrize("record", RECORDS)
def test_most_beats_give_an_interval(results, record) -> None:
    r = results[record]
    assert r["band_intervals"] >= 0.8 * r["ecg_beats"], r


@pytest.mark.parametrize("record", ["01", "03"])
def test_rmssd_agrees_with_the_ecg_where_the_pulse_is_clean(results, record) -> None:
    r = results[record]
    assert abs(r["rmssd_band_ms_median"] - r["rmssd_ecg_ms_median"]) <= 5.0, r


def test_known_limit_ppg_rmssd_differs_from_ecg_on_record_02(results) -> None:
    """Characterisation, not a requirement: the PPG's RMSSD here is about 5x the ECG's."""
    r = results["02"]
    assert r["rmssd_band_ms_median"] > 3 * r["rmssd_ecg_ms_median"], r


def test_the_pulse_is_inverted_like_a_reflective_sensor() -> None:
    def pulse(fs: int) -> np.ndarray:
        return 0.5 + 0.4 * np.sin(2 * np.pi * 1.2 * np.arange(0, 10, 1 / fs))  # up on systole

    ir = rp.pleth_to_ir_counts(pulse(rp.BIDMC_FS))
    assert np.corrcoef(pulse(rp.PPG_FS), ir)[0, 1] < -0.99


def test_rmssd_uses_adjacent_clean_intervals_only() -> None:
    rr = np.array([800.0, 820.0, 800.0, 820.0, 800.0])
    t = np.cumsum(rr) / 1000.0
    assert rp._rmssd(t, rr) == pytest.approx(20.0)
    gap = t.copy()
    gap[2:] += 5.0  # a missed beat after the second interval: that pair is not adjacent
    assert rp._rmssd(gap, rr) == pytest.approx(20.0)  # the other three pairs still count
    assert np.isnan(rp._rmssd(t[:2], rr[:2]))


def test_a_window_with_no_usable_rmssd_gives_valid_json_not_nan() -> None:
    """Intervals 5 s apart are never adjacent: no RMSSD, which must come out as null, not NaN."""
    band = [(1.0 + 5.0 * k, 800.0) for k in range(6)]
    ecg = np.arange(0.0, 31.0, 0.8)
    out = rp.compare_windows(band, ecg, 30.0)
    assert out["windows_compared"] == 1
    assert out["rmssd_band_ms_median"] is None
    json.dumps(out, allow_nan=False)
