"""The §4a bench numbers must recover a gain and a noise level that we put in ourselves."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import g1_measure as g1  # noqa: E402
import real_signal_replay as rsr  # noqa: E402

FS = 500.0


def _bench_sine(
    counts_per_mv: float, seconds: float = 20.0, noise_counts: float = 2.0
) -> np.ndarray:
    """1 mVpp, 10 Hz through the same ADC model the replay uses, plus electrode noise."""
    t = np.arange(int(seconds * FS)) / FS
    mv = 0.5 * np.sin(2 * np.pi * 10.0 * t)
    counts, _ = rsr.ecg_to_adc_counts(mv, int(FS), counts_per_mv=counts_per_mv)
    rng = np.random.RandomState(3)
    return counts + np.rint(rng.normal(0, noise_counts, counts.size))


@pytest.mark.parametrize("true_gain", [40.0, 132.0, 400.0])
def test_the_gain_we_inject_is_the_gain_we_measure(true_gain: float) -> None:
    got = g1.gain_from_sine(_bench_sine(true_gain), FS, 10.0, 1.0)
    assert got["counts_per_mv"] == pytest.approx(true_gain, rel=0.02)
    assert got["offset_counts"] == pytest.approx(rsr.ADC_MIDSCALE, abs=2)


def test_gain_measured_needs_the_adc_scale_and_is_never_guessed() -> None:
    sine = _bench_sine(132.0)
    assert "gain_measured" not in g1.gain_from_sine(sine, FS, 10.0, 1.0)
    got = g1.gain_from_sine(sine, FS, 10.0, 1.0, adc_mv_per_count=0.5)
    assert got["gain_measured"] == pytest.approx(132.0 * 0.5, rel=0.02)


def test_a_capture_without_the_sine_is_refused_not_scored() -> None:
    rng = np.random.RandomState(0)
    shorted = 2048 + np.rint(rng.normal(0, 3, 10_000))
    with pytest.raises(ValueError, match="no 10.0 Hz sine"):
        g1.gain_from_sine(shorted, FS, 10.0, 1.0)


def test_the_noise_floor_is_the_std_in_counts() -> None:
    rng = np.random.RandomState(1)
    shorted = 2048 + rng.normal(0, 3.0, 30_000)
    assert g1.noise_floor_counts(shorted)["std_counts"] == pytest.approx(3.0, rel=0.03)
