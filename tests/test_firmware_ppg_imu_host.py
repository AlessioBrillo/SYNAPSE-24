"""Host tests for firmware/common PPG/IMU processing (Architecture.md 34, Roadmap Phase 1).

ppg_dsp is checked against synthetic PPG with known beat times (the Phase-0 HR/RMSSD
gates, now on the 50 Hz on-device path). ppg_processor / imu_processor are driven
sample-by-sample, which regression-tests the never-set `initialized` flag and the
pre-fill ring ordering that previously made them emit nothing.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np
import pytest

from tests.firmware_host import COMMON, ROOT, build, load

FS = 50
HOST = ROOT / "tests" / "firmware_host"
fp = ctypes.POINTER(ctypes.c_float)


class DspResult(ctypes.Structure):
    _fields_ = [
        ("hr_bpm", ctypes.c_float),
        ("rmssd_ms", ctypes.c_float),
        ("sdnn_ms", ctypes.c_float),
        ("ppi_ms", ctypes.c_float),
        ("snr_db", ctypes.c_float),
        ("peak_count", ctypes.c_int),
        ("interval_count", ctypes.c_int),
        ("valid", ctypes.c_int),
        ("peak_pos", ctypes.c_float * 32),
    ]


class PpgFeatures(ctypes.Structure):
    _fields_ = [
        ("hr_bpm", ctypes.c_float),
        ("rmssd_ms", ctypes.c_float),
        ("sdnn_ms", ctypes.c_float),
        ("ppi_ms", ctypes.c_float),
        ("snr_db", ctypes.c_float),
        ("pi_percent", ctypes.c_float),
        ("valid", ctypes.c_bool),
        ("motion_artifact", ctypes.c_bool),
        ("peak_count", ctypes.c_int),
        ("timestamp_us", ctypes.c_int64),
    ]


class PpgSqi(ctypes.Structure):
    _fields_ = [
        ("sqi", ctypes.c_float),
        ("perfusion_index", ctypes.c_float),
        ("motion_artifact_prob", ctypes.c_float),
        ("timestamp_us", ctypes.c_int64),
    ]


class ImuFeatures(ctypes.Structure):
    _fields_ = [(n, ctypes.c_float) for n in (
        "motion_intensity", "acc_rms_x", "acc_rms_y", "acc_rms_z",
        "gyro_rms_x", "gyro_rms_y", "gyro_rms_z",
        "tilt_x_deg", "tilt_y_deg", "tilt_z_deg",
        "spectral_entropy", "dominant_freq_hz", "sleep_probability",
    )] + [("is_stationary", ctypes.c_bool), ("timestamp_us", ctypes.c_int64)]  # fmt: skip


@pytest.fixture(scope="module")
def fw(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = build(
        tmp_path_factory.mktemp("fw"),
        c_sources=[COMMON / "sensors" / "ppg_dsp.c", HOST / "host_glue.c"],
        cpp_sources=[COMMON / "sensors" / "ppg_processor.cpp", COMMON / "sensors" / "imu_processor.cpp"],
    )  # fmt: skip
    d = load(lib)
    d.ppg_dsp_analyse.argtypes = [fp, ctypes.c_int, ctypes.POINTER(DspResult)]
    d.ppg_processor_init.restype = ctypes.c_int
    d.ppg_processor_process_sample.restype = ctypes.c_int
    d.ppg_processor_process_sample.argtypes = [
        ctypes.c_float, ctypes.c_float, ctypes.c_int64,
        ctypes.POINTER(PpgSqi), ctypes.POINTER(PpgFeatures),
    ]  # fmt: skip
    d.imu_processor_init.restype = ctypes.c_int
    d.imu_processor_process_sample.restype = ctypes.c_int
    d.imu_processor_process_sample.argtypes = [ctypes.c_float] * 6 + [
        ctypes.c_int64, ctypes.POINTER(ImuFeatures),
    ]  # fmt: skip
    d.host_last_ppg_rate.restype = ctypes.c_uint32
    d.host_set_ppg_rate.argtypes = [ctypes.c_uint32]
    d.host_set_ppg_rate.restype = None
    return d


def synth_ppg(seconds: float, seed: int, hr: float = 70.0, hrv_ms: float = 25.0):
    """Raw-count PPG (DC 1e5) with known beat times: systolic + diastolic Gaussians."""
    rng = np.random.default_rng(seed)
    rr = 60.0 / hr + rng.normal(0, hrv_ms / 1000.0, int(seconds * 3))
    beats = np.cumsum(rr) - rr[0] + rng.uniform(0.2, 0.6)
    t = np.arange(int(seconds * FS)) / FS
    sig = np.zeros_like(t)
    for b in beats:
        sig += np.exp(-0.5 * ((t - b) / 0.06) ** 2) + 0.35 * np.exp(
            -0.5 * ((t - b - 0.28) / 0.09) ** 2
        )
    sig = 800 * sig + 150 * np.sin(2 * np.pi * 0.12 * t) + rng.normal(0, 8, t.size)
    return (1e5 + sig).astype(np.float32), beats[beats < seconds]


def analyse(fw: ctypes.CDLL, ir: np.ndarray) -> DspResult:
    r = DspResult()
    assert fw.ppg_dsp_analyse(ir.ctypes.data_as(fp), len(ir), ctypes.byref(r)) == 0
    return r


def test_ppg_dsp_hr_hrv_vs_ground_truth(fw: ctypes.CDLL) -> None:
    hr_err, rmssd_err, timing = [], [], []
    for seed in range(30):
        ir, beats = synth_ppg(5.0, seed)  # one on-device window (5 s)
        r = analyse(fw, ir)
        assert r.valid, f"seed {seed}: {r.peak_count} peaks"
        pos = np.array(r.peak_pos[: r.peak_count]) / FS
        near = np.array([beats[np.argmin(np.abs(beats - p))] for p in pos])
        lag = np.median(pos - near)  # constant filter group delay, irrelevant to intervals
        timing += list(np.abs(pos - near - lag) * 1000)
        true_iv = np.diff(near)
        hr_err.append(abs(r.hr_bpm - 60.0 / true_iv.mean()))
        rmssd_err.append(abs(r.rmssd_ms - 1000 * np.sqrt(np.mean(np.diff(true_iv) ** 2))))
    assert np.mean(hr_err) < 1.0, np.mean(hr_err)  # bpm
    assert np.mean(rmssd_err) < 5.0, np.mean(rmssd_err)  # ms, Phase-0 RMSSD MAE gate
    assert np.percentile(timing, 95) < 10.0, np.percentile(timing, 95)  # ms, 20 ms sampling


def test_ppg_dsp_flat_signal_is_invalid(fw: ctypes.CDLL) -> None:
    r = analyse(fw, np.full(250, 1e5, dtype=np.float32))
    assert not r.valid
    assert r.peak_count == 0


def test_ppg_processor_streams_features_at_10hz(fw: ctypes.CDLL) -> None:
    """Regression: `initialized` never set -> context wiped every sample -> no output."""
    assert fw.ppg_processor_init() == 0
    ir, beats = synth_ppg(20.0, seed=3, hrv_ms=10.0)
    sqi = PpgSqi(0.9, 2.0, 0.05, 0)
    out = PpgFeatures()
    emitted, hrs = [], []
    for i, v in enumerate(ir):
        ret = fw.ppg_processor_process_sample(v, v, i * 20000, ctypes.byref(sqi), ctypes.byref(out))
        if ret == 0:
            emitted.append(i)
            if i >= 6 * FS and out.valid:  # window (5 s) full
                hrs.append(out.hr_bpm)
    gaps = np.diff(emitted)
    assert emitted[0] >= FS * 5 // 2 - 1  # nothing reported before the window is half full
    assert len(emitted) >= 170, len(emitted)  # ~10 Hz from 2.5 s to 20 s
    assert set(gaps) == {5}, set(gaps)  # 50 Hz / 10 Hz, no stale 6/7 alternation
    assert len(hrs) > 100
    true_hr = 60.0 / np.mean(np.diff(beats))
    assert abs(np.median(hrs) - true_hr) < 2.0, (np.median(hrs), true_hr)


def test_imu_processor_stationary_and_motion_gate(fw: ctypes.CDLL) -> None:
    assert fw.imu_processor_init() == 0
    out = ImuFeatures()
    emitted = 0
    for i in range(15 * FS):  # 10 s window + margin, device lying still: 1 g on z
        ret = fw.imu_processor_process_sample(
            0.0, 0.0, 1.0, 0.0, 0.0, 0.0, i * 20000, ctypes.byref(out)
        )
        emitted += ret == 0
    assert emitted >= 5  # 1 Hz output once the window is half full
    assert out.is_stationary
    assert abs(out.motion_intensity - 1.0) < 0.01
    assert abs(out.dominant_freq_hz) < 3.0
    assert fw.host_last_ppg_rate() == 50  # nominal PPG rate (was hard-coded 64)


def test_imu_processor_ring_order_before_fill(fw: ctypes.CDLL) -> None:
    """Pre-fill window must start at the oldest sample (it used to start at write_idx: zeros)."""
    assert fw.imu_processor_init() == 0
    out = ImuFeatures()
    got = None
    for i in range(6 * FS):  # > half window, < full window
        if fw.imu_processor_process_sample(1.0, 1.0, 1.0, 0.0, 0.0, 0.0, i, ctypes.byref(out)) == 0:
            got = out.acc_rms_x
    assert got is not None
    assert abs(got - 1.0) < 1e-3  # zero-padded wrap would give ~0.7


def test_ppg_processor_ignores_motion_gated_rate(fw: ctypes.CDLL) -> None:
    """At 16 Hz (motion gate) the 50 Hz DSP would time-warp the window: drop and restart."""
    assert fw.ppg_processor_init() == 0
    ir, _ = synth_ppg(12.0, seed=5)
    sqi, out = PpgSqi(0.9, 2.0, 0.05, 0), PpgFeatures()

    def ret(v: float, i: int) -> int:
        return fw.ppg_processor_process_sample(
            v, v, i * 20000, ctypes.byref(sqi), ctypes.byref(out)
        )

    fw.host_set_ppg_rate(16)
    try:
        assert all(ret(v, i) == 0x10B for i, v in enumerate(ir[:200]))  # NOT_FINISHED, never ESP_OK
    finally:
        fw.host_set_ppg_rate(50)
    emitted = [i for i, v in enumerate(ir) if ret(v, i) == 0]
    assert emitted
    assert emitted[0] >= 125 - 1  # window restarted from empty after the gated period
