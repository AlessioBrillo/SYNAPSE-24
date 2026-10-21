"""Host gate for the firmware PPG SQI (``firmware/common/sensors/ppg_sqi.cpp``).

The audit found that the device-side SQI rewarded noise: the kurtosis
feature scored ``1/(1+|excess kurtosis|*0.5)``, and a Gaussian noise buffer
has *zero* excess kurtosis, so pure noise scored 1.0. The host
implementation had the same class of bug.

The firmware source is compiled here (no ESP-IDF needed) and driven with a
clean pulse train and a noise buffer: the SQI of the signal must be
clearly higher than the SQI of the noise. A firmware metric nobody compiles
is a firmware metric nobody can trust.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np
import pytest

from tests.firmware_host import COMMON, build, try_load  # noqa: E402

PPG_SQI_SRC = COMMON / "sensors" / "ppg_sqi.cpp"

# The PPG stream runs at 50 Hz (config/hardware.yaml).
FS_HZ = 50.0


class _PpgSqiResult(ctypes.Structure):
    """ctypes mirror of ``ppg_sqi_result_t``."""

    _fields_ = [
        ("sqi", ctypes.c_float),
        ("perfusion_index", ctypes.c_float),
        ("motion_artifact_prob", ctypes.c_float),
        ("timestamp_us", ctypes.c_int64),
    ]


@pytest.fixture(scope="module")
def sqi_lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = build(tmp_path_factory.mktemp("ppg_sqi"), [], cpp_sources=[PPG_SQI_SRC])
    d = try_load(lib)
    if d is None:
        pytest.skip(
            "OS policy blocked loading the freshly built library; the compile "
            "succeeded, which is what this gate proves here"
        )
    d.ppg_sqi_init.restype = ctypes.c_int
    d.ppg_sqi_process_sample.restype = ctypes.c_int
    d.ppg_sqi_process_sample.argtypes = [
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_int64,
        ctypes.POINTER(_PpgSqiResult),
    ]
    return d


def _run(sqi_lib: ctypes.CDLL, signal: np.ndarray) -> _PpgSqiResult:
    """Feed a signal to the firmware SQI and return the last result."""
    result = _PpgSqiResult()
    for index, value in enumerate(signal):
        sqi_lib.ppg_sqi_process_sample(
            float(value),
            float(value),
            int(index * 1_000_000 / FS_HZ),
            ctypes.byref(result),
        )
    return result


def _pulse_train(n: int, fs: float = FS_HZ, hr_hz: float = 1.2) -> np.ndarray:
    """A clean pulse train: narrow peaks at ``hr_hz`` on a flat baseline."""
    t = np.arange(n) / fs
    period = 1.0 / hr_hz
    signal = np.zeros(n)
    for index in range(int(t[-1] / period) + 1):
        signal += np.exp(-0.5 * ((t - index * period) / 0.02) ** 2)
    return signal


def test_firmware_sqi_prefers_the_signal_over_the_noise(sqi_lib: ctypes.CDLL) -> None:
    """A clean pulse train must score higher than a noise buffer.

    Before the fix the noise scored 1.0 (a Gaussian has zero excess
    kurtosis), i.e. the device reported perfect quality on a dead sensor.
    """
    n = 600
    clean = _run(sqi_lib, _pulse_train(n))
    rng = np.random.default_rng(11)
    noise = _run(sqi_lib, rng.standard_normal(n) * 1000.0)

    assert clean.sqi > noise.sqi, (
        f"the firmware SQI does not separate signal from noise: "
        f"clean={clean.sqi:.3f} noise={noise.sqi:.3f}"
    )
    assert noise.sqi < 0.7, f"a noise buffer scored {noise.sqi:.3f}"


def test_firmware_sqi_is_bounded(sqi_lib: ctypes.CDLL) -> None:
    """The SQI and the MAP must stay in [0, 1] for any input."""
    rng = np.random.default_rng(3)
    for signal in (
        _pulse_train(400),
        rng.standard_normal(400) * 1e4,
        np.zeros(400),
        np.full(400, 42.0),
    ):
        result = _run(sqi_lib, signal)
        assert 0.0 <= result.sqi <= 1.0
        assert 0.0 <= result.motion_artifact_prob <= 1.0


def test_firmware_and_host_agree_on_the_noise_case(sqi_lib: ctypes.CDLL) -> None:
    """The device and the host must not disagree on an obvious case.

    The host implementation (src/synapse24/signal_quality/ppg.py) uses the
    same feature set: a noise buffer must score low on both sides.
    """
    from synapse24.signal_quality.ppg import compute_ppg_sqi

    n = 600
    rng = np.random.default_rng(11)
    noise = rng.standard_normal(n) * 1000.0
    firmware = _run(sqi_lib, noise)
    host = compute_ppg_sqi(noise, int(FS_HZ))

    assert firmware.sqi < 0.7, f"the firmware SQI scored noise {firmware.sqi:.3f}"
    assert host < 0.7, f"the host SQI scored noise {host:.3f}"
