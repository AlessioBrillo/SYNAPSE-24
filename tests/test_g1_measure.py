"""The §4a bench numbers must recover a gain and a noise level that we put in ourselves.

The "bench" here is ``ecg_to_wire_uv``: the same model of the front end, the ADC and the firmware's
``(adc - midrail) / gain`` that the replay uses, so what is measured is what the band would send.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "sdk" / "python"))

import g1_measure as g1  # noqa: E402
import real_signal_replay as rsr  # noqa: E402

FS = 500.0


def _bench_sine(
    true_gain: float,
    configured_gain: float = 1100.0,
    *,
    applied_mvpp: float = 1.0,
    seconds: float = 20.0,
    noise_mv: float = 0.002,
) -> np.ndarray:
    """A 10 Hz sine of ``applied_mvpp`` plus electrode noise, as the host receives it."""
    t = np.arange(int(seconds * FS)) / FS
    rng = np.random.RandomState(3)
    mv = 0.5 * applied_mvpp * np.sin(2 * np.pi * 10.0 * t) + rng.normal(0, noise_mv, t.size)
    wire, saturated = rsr.ecg_to_wire_uv(
        mv, int(FS), true_gain=true_gain, configured_gain=configured_gain
    )
    assert saturated == 0.0
    return wire.astype(np.float64)


@pytest.mark.parametrize("true_gain", [400.0, 1100.0, 1500.0])
def test_the_gain_we_inject_is_the_gain_we_measure(true_gain: float) -> None:
    got = g1.gain_from_sine(_bench_sine(true_gain), FS, 10.0, 1.0)
    assert got["gain_measured"] == pytest.approx(true_gain, rel=0.02)
    assert got["sdkconfig_ecg_gain"] == pytest.approx(true_gain * 100, rel=0.02)


def test_a_clone_with_the_wrong_configured_gain_is_still_measured_right() -> None:
    """The firmware divides by 1100 but the clone really has 600: the stream is ~0.55x, the answer 600."""
    wire = _bench_sine(true_gain=600.0, configured_gain=1100.0)
    got = g1.gain_from_sine(wire, FS, 10.0, 1.0, configured_gain=1100.0)
    assert got["stream_over_applied"] == pytest.approx(600.0 / 1100.0, rel=0.02)
    assert got["gain_measured"] == pytest.approx(600.0, rel=0.02)


def test_a_saturated_stream_is_refused_not_scored() -> None:
    wire = np.full(5000, 32767.0) * np.sign(np.sin(np.arange(5000) / 8.0))
    with pytest.raises(ValueError, match="s16 limit"):
        g1.gain_from_sine(wire, FS, 10.0, 1.0)


def test_a_capture_without_the_sine_is_refused_not_scored() -> None:
    rng = np.random.RandomState(0)
    shorted = np.rint(rng.normal(0, 3, 10_000))
    with pytest.raises(ValueError, match="no 10.0 Hz sine"):
        g1.gain_from_sine(shorted, FS, 10.0, 1.0)


def test_the_noise_floor_is_the_std_in_uv() -> None:
    rng = np.random.RandomState(1)
    shorted = rng.normal(0, 3.0, 30_000)
    assert g1.noise_floor_uv(shorted)["std_uv"] == pytest.approx(3.0, rel=0.03)


def test_the_capture_can_be_the_xdf_the_validator_writes(tmp_path: Path) -> None:
    pytest.importorskip("pyxdf", reason="pyxdf is a project dependency")
    from synapse_device.xdf_writer import write_xdf

    wire = _bench_sine(true_gain=600.0)
    path = tmp_path / "bench.xdf"
    write_xdf(
        path,
        [
            {
                "name": g1.ECG_STREAM,
                "type": "ECG",
                "channel_count": 1,
                "sampling_rate": FS,
                "channel_format": "float32",
                "channel_names": ["ecg_uv"],
                "channel_units": ["uV"],
                "source_id": "bench",
                "data": wire.reshape(-1, 1),
                "timestamps": np.arange(wire.size) / FS,
            }
        ],
    )
    values, fs = g1.load_capture(path, 123.0)
    assert fs == FS
    got = g1.gain_from_sine(values, fs, 10.0, 1.0)
    assert got["gain_measured"] == pytest.approx(600.0, rel=0.02)
    write_xdf(
        tmp_path / "other.xdf",
        [
            {
                "name": "SYNAPSE_PPG_T0",
                "type": "PPG",
                "channel_count": 1,
                "sampling_rate": 50.0,
                "channel_format": "float32",
                "channel_names": ["ir"],
                "channel_units": ["counts"],
                "source_id": "bench",
                "data": np.zeros((100, 1)),
                "timestamps": np.arange(100) / 50.0,
            }
        ],
    )
    with pytest.raises(ValueError, match="no SYNAPSE_ECG_T0"):
        g1.load_capture(tmp_path / "other.xdf", FS)
