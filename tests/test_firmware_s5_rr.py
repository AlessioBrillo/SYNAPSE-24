"""MVP-1.4 S5: beat-to-beat intervals from the infrared PPG, behind a flag.

``synapse_rr.c`` is pure C and is compiled and **run** here against synthetic pulse trains whose beat
times are known. That proves the algorithm does what it says on a clean signal and fails safe on a
bad one. It says nothing about a real finger: there is no band yet, the thresholds are starting
values, and the feature stays off (``CONFIG_SYNAPSE_RR_ENABLE=n``) until the bench run.
"""

from __future__ import annotations

import ctypes
import re
from pathlib import Path

import numpy as np
import pytest

from tests.firmware_host import build, try_load

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
RR_SRC = BAND / "signal_quality" / "synapse_rr.c"
GLUE = ROOT / "tests" / "firmware_host" / "host_glue_rr.c"
FS = 50.0
BASE = 120_000.0  # IR counts at rest
PULSE = 4_000.0  # counts of the pulse
T0_US = 10_000_000


@pytest.fixture(scope="module")
def lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    d = try_load(
        build(
            tmp_path_factory.mktemp("rr"),
            [GLUE, RR_SRC],
            extra_includes=[str(BAND / "signal_quality")],
        )
    )
    if d is None:
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    d.host_rr_size.restype = ctypes.c_uint32
    d.host_rr_init.argtypes = [ctypes.c_void_p]
    d.host_rr_push.argtypes = [ctypes.c_void_p, ctypes.c_float, ctypes.c_int64]
    d.host_rr_push.restype = ctypes.c_int
    d.host_rr_take.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.c_uint8]
    d.host_rr_take.restype = ctypes.c_uint32
    d.host_rr_peek.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint16), ctypes.c_uint8]
    d.host_rr_peek.restype = ctypes.c_uint32
    d.host_rr_commit.argtypes = [ctypes.c_void_p]
    d.host_rr_commit.restype = None
    d.host_rr_expire.argtypes = [ctypes.c_void_p, ctypes.c_int64]
    d.host_rr_expire.restype = None
    d.host_rr_hrv.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
    ]
    d.host_rr_hrv.restype = ctypes.c_int
    d.host_rr_accepted.argtypes = d.host_rr_rejected.argtypes = [ctypes.c_void_p]
    d.host_rr_accepted.restype = d.host_rr_rejected.restype = ctypes.c_uint32
    return d


class _Detector:
    def __init__(self, lib: ctypes.CDLL) -> None:
        self.lib = lib
        self.state = ctypes.create_string_buffer(int(lib.host_rr_size()))
        lib.host_rr_init(self.state)
        self.intervals_ms: list[float] = []

    def feed(self, ir: np.ndarray, ts_us: np.ndarray) -> None:
        for value, ts in zip(ir, ts_us, strict=True):
            self.lib.host_rr_push(self.state, float(value), int(ts))
            self.take()

    def take(self) -> None:
        out = (ctypes.c_uint16 * 8)()
        total = self.lib.host_rr_take(self.state, out, 8)
        self.intervals_ms += [out[i] * 1000.0 / 1024.0 for i in range(min(total, 8))]

    def hrv(self) -> tuple[float, float] | None:
        rmssd, sdnn = ctypes.c_float(), ctypes.c_float()
        if not self.lib.host_rr_hrv(self.state, ctypes.byref(rmssd), ctypes.byref(sdnn)):
            return None
        return rmssd.value, sdnn.value


def _signal(
    beat_times_s: np.ndarray,
    seconds: float,
    *,
    noise: float = 30.0,
    seed: int = 1,
    dicrotic: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Reflective IR PPG at 50 Hz: the count FALLS on each pulse (a 120 ms-wide systolic wave).

    ``dicrotic`` adds the second wave that real pulses have, 350 ms after each systolic peak, at
    that share of its height.
    """
    t = np.arange(int(seconds * FS)) / FS
    pulse = np.zeros_like(t)
    for tb in beat_times_s:
        pulse += np.exp(-0.5 * ((t - tb) / 0.12) ** 2)
        if dicrotic:
            pulse += dicrotic * np.exp(-0.5 * ((t - tb - 0.35) / 0.08) ** 2)
    rng = np.random.RandomState(seed)
    ir = BASE - PULSE * pulse + rng.normal(0.0, noise, t.size)
    return ir, T0_US + (t * 1e6).astype(np.int64)


def _beats(rr_ms: np.ndarray, start_s: float = 1.0) -> np.ndarray:
    return start_s + np.concatenate([[0.0], np.cumsum(rr_ms)]) / 1000.0


def _variable_rr(n: int, mean: float = 900.0, sd: float = 45.0, seed: int = 7) -> np.ndarray:
    return np.clip(np.random.RandomState(seed).normal(mean, sd, n), 600.0, 1300.0)


# --- accuracy on a clean signal -------------------------------------------------------------------------


def _aligned_error(got: np.ndarray, truth: np.ndarray) -> tuple[float, int]:
    """Mean absolute error at the best shift.

    The first interval of the detector is made while the baseline settles, so the two sequences
    may start a beat or two apart.
    """
    best = (np.inf, 0)
    for shift in range(-4, 5):
        a = got[max(shift, 0) :]
        b = truth[max(-shift, 0) :]
        n = min(len(a), len(b))
        err = float(np.mean(np.abs(a[:n] - b[:n])))
        if err < best[0]:
            best = (err, shift)
    return best


def test_intervals_are_recovered_to_a_few_milliseconds(lib: ctypes.CDLL) -> None:
    truth = _variable_rr(60)
    ir, ts = _signal(_beats(truth), seconds=62.0)
    det = _Detector(lib)
    det.feed(ir, ts)
    got = np.array(det.intervals_ms)
    assert len(got) >= 55, "most of the 60 beats should give an interval"
    error, shift = _aligned_error(got, truth)
    assert error < 6.0, f"mean interval error {error:.1f} ms (the sample period is 20 ms)"
    assert abs(shift) <= 2


def test_interpolation_beats_the_sample_period(lib: ctypes.CDLL) -> None:
    """A heart at rest varies by tens of ms: intervals quantised to 20 ms would erase it."""
    truth = _variable_rr(60, sd=40.0, seed=11)
    ir, ts = _signal(_beats(truth), seconds=62.0, noise=20.0, seed=5)
    det = _Detector(lib)
    det.feed(ir, ts)
    got = np.array(det.intervals_ms)
    assert len(np.unique(np.round(got))) > 30, "intervals look quantised to the sample period"
    error, _shift = _aligned_error(got, truth)
    assert error < 6.0
    # the error is well under the 20 ms a nearest-sample detector is stuck with
    assert error < 20.0 / 2.0


def test_rmssd_and_sdnn_match_the_truth(lib: ctypes.CDLL) -> None:
    truth = _variable_rr(70, sd=50.0, seed=3)
    ir, ts = _signal(_beats(truth), seconds=72.0, noise=20.0, seed=2)
    det = _Detector(lib)
    det.feed(ir, ts)
    hrv = det.hrv()
    assert hrv is not None
    rmssd, sdnn = hrv
    last = truth[-32:]
    assert sdnn == pytest.approx(float(np.std(last, ddof=1)), abs=12.0)
    assert rmssd == pytest.approx(float(np.sqrt(np.mean(np.diff(last) ** 2))), abs=15.0)


def test_nothing_is_reported_before_enough_clean_intervals(lib: ctypes.CDLL) -> None:
    truth = _variable_rr(5)
    ir, ts = _signal(_beats(truth), seconds=8.0)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert det.hrv() is None


# --- fails safe on a bad signal -----------------------------------------------------------------------------


def test_a_flat_signal_has_no_beats(lib: ctypes.CDLL) -> None:
    ir, ts = _signal(np.array([]), seconds=30.0, noise=5.0)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert det.intervals_ms == []
    assert det.hrv() is None


def test_noise_alone_does_not_invent_a_heart_rate(lib: ctypes.CDLL) -> None:
    ir, ts = _signal(np.array([]), seconds=30.0, noise=400.0, seed=9)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert det.hrv() is None, "a statistic out of noise would be worse than none"


def test_a_missed_beat_is_rejected_not_averaged(lib: ctypes.CDLL) -> None:
    rr = np.full(40, 900.0)
    beats = _beats(rr)
    beats = np.delete(beats, 20)  # the sensor misses one pulse: a 1800 ms interval
    ir, ts = _signal(beats, seconds=40.0, noise=15.0)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert max(det.intervals_ms) < 1000.0, "the doubled interval must not be reported"
    assert lib.host_rr_rejected(det.state) >= 1


def test_a_gap_in_the_samples_makes_no_interval_across_it(lib: ctypes.CDLL) -> None:
    beats = _beats(np.full(40, 900.0))
    ir, ts = _signal(beats, seconds=40.0, noise=15.0)
    keep = ~((ts >= T0_US + 15_000_000) & (ts < T0_US + 18_000_000))  # 3 s with no data
    det = _Detector(lib)
    det.feed(ir[keep], ts[keep])
    assert all(800.0 < v < 1000.0 for v in det.intervals_ms), "an interval spans the hole"
    assert len(det.intervals_ms) > 25


@pytest.mark.parametrize("bpm", [40, 180])
def test_the_physiological_range_is_followed(lib: ctypes.CDLL, bpm: int) -> None:
    rr = 60000.0 / bpm
    ir, ts = _signal(_beats(np.full(60, rr)), seconds=50.0, noise=15.0)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert len(det.intervals_ms) > 15
    assert np.median(det.intervals_ms) == pytest.approx(rr, abs=15.0)


def test_nan_and_old_samples_are_ignored(lib: ctypes.CDLL) -> None:
    det = _Detector(lib)
    ir, ts = _signal(_beats(np.full(30, 900.0)), seconds=20.0, noise=15.0)
    det.feed(ir[:200], ts[:200])
    before = len(det.intervals_ms)
    assert lib.host_rr_push(det.state, float("nan"), int(ts[200])) == 0
    assert lib.host_rr_push(det.state, 1.0, int(ts[100])) == 0  # older than the last
    det.feed(ir[200:], ts[200:])
    assert len(det.intervals_ms) > before


def test_the_pending_queue_keeps_the_newest_and_says_how_many_there_were(
    lib: ctypes.CDLL,
) -> None:
    ir, ts = _signal(_beats(np.full(30, 900.0)), seconds=28.0, noise=15.0)
    state = ctypes.create_string_buffer(int(lib.host_rr_size()))
    lib.host_rr_init(state)
    for v, t in zip(ir, ts, strict=True):
        lib.host_rr_push(state, float(v), int(t))  # never taken
    out = (ctypes.c_uint16 * 3)()
    total = lib.host_rr_take(state, out, 3)
    assert total > 8, "it counts every interval, even the ones that no longer fit"
    assert all(abs(out[i] * 1000.0 / 1024.0 - 900.0) < 25.0 for i in range(3))
    assert lib.host_rr_take(state, out, 3) == 0, "taking clears the queue"


def test_the_dicrotic_wave_is_not_taken_for_a_beat(lib: ctypes.CDLL) -> None:
    """A second wave 350 ms after each peak, at 60 % of its height, clears the amplitude threshold.

    With a fixed 300 ms refractory it became a beat of its own: two short intervals per heartbeat,
    each rejected against the median, so the chain broke every time and no statistic ever came out.
    """
    truth = _variable_rr(70, sd=50.0, seed=3)
    ir, ts = _signal(_beats(truth), seconds=72.0, noise=20.0, seed=2, dicrotic=0.6)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert len(det.intervals_ms) > 20, "no intervals"
    assert min(det.intervals_ms) > 500.0, "an interval shorter than a real beat was reported"
    assert lib.host_rr_rejected(det.state) <= 3
    hrv = det.hrv()
    assert hrv is not None, "the dicrotic wave silenced the statistics"
    assert hrv[1] == pytest.approx(float(np.std(truth[-32:], ddof=1)), abs=15.0)


def test_peek_leaves_the_intervals_until_they_are_committed(lib: ctypes.CDLL) -> None:
    """A frame that cannot leave must not eat the intervals: peek, send, then commit."""
    ir, ts = _signal(_beats(np.full(12, 900.0)), seconds=12.0, noise=15.0)
    state = ctypes.create_string_buffer(int(lib.host_rr_size()))
    lib.host_rr_init(state)
    for v, t in zip(ir, ts, strict=True):
        lib.host_rr_push(state, float(v), int(t))
    out = (ctypes.c_uint16 * 4)()
    first = lib.host_rr_peek(state, out, 4)
    assert first > 0
    assert lib.host_rr_peek(state, out, 4) == first, "peeking consumed the intervals"
    lib.host_rr_commit(state)
    assert lib.host_rr_peek(state, out, 4) == 0, "commit left intervals behind"


def test_stale_intervals_expire_when_the_beats_stop(lib: ctypes.CDLL) -> None:
    """The finger leaves the sensor: RMSSD/SDNN must not describe a rhythm nobody is measuring."""
    truth = _variable_rr(70, sd=50.0, seed=3)
    beats = _beats(truth)
    ir, ts = _signal(beats, seconds=float(beats[-1]) + 1.0, noise=20.0, seed=2)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert det.hrv() is not None
    last = T0_US + int(beats[-1] * 1e6)  # the instant of the last beat
    lib.host_rr_expire(det.state, last + 2_000_000)
    assert det.hrv() is not None, "two seconds of silence is not yet stale"
    lib.host_rr_expire(det.state, last + 8_000_000)
    assert det.hrv() is None, "intervals from before the silence still feed the statistics"


# --- the feature is off by default and wired as a flag --------------------------------------------------------


def test_the_feature_is_off_by_default() -> None:
    kconfig = (BAND / "config" / "Kconfig").read_text(encoding="utf-8")
    block = re.search(r"config SYNAPSE_RR_ENABLE\n(.*?)\n\n", kconfig, re.S)
    assert block, "SYNAPSE_RR_ENABLE not declared"
    assert "default n" in block.group(1)
    assert "bool" in block.group(1)
    for name in ("sdkconfig.defaults", "sdkconfig.defaults.core"):
        text = (BAND / name).read_text(encoding="utf-8")
        assert "CONFIG_SYNAPSE_RR_ENABLE=y" not in text, name


def test_app_main_only_uses_it_behind_the_flag_and_gates_it_on_quality() -> None:
    main = (BAND / "main" / "app_main.c").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "#if defined(CONFIG_SYNAPSE_RR_ENABLE)" in main
    block = main[main.index("#if defined(CONFIG_SYNAPSE_RR_ENABLE)", main.index("fill_status")) :]
    # published only for a fresh reading with a good signal
    helper = (BAND / "main" / "synapse_feature_map.h").read_text(encoding="utf-8")
    assert "#define SYNAPSE_RR_MIN_SQI 0.5f" in helper
    assert "#define SYNAPSE_RR_MAX_MOTION 0.3f" in helper
    assert "synapse_rr_publishable(" in main
    assert "synapse_rr_peek(" in block
    assert "synapse_rr_hrv(" in block
    # the library is part of the component either way (it is small and tested)
    cmake = (BAND / "signal_quality" / "CMakeLists.txt").read_text(encoding="utf-8")
    assert '"synapse_rr.c"' in cmake


def test_a_few_artefacts_do_not_silence_a_good_recording(lib: ctypes.CDLL) -> None:
    rr = _variable_rr(60, sd=30.0, seed=13)
    beats = np.delete(_beats(rr), [15, 33])  # two missed pulses in 60
    ir, ts = _signal(beats, seconds=62.0, noise=20.0, seed=4)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert det.hrv() is not None
    assert lib.host_rr_rejected(det.state) >= 2


@pytest.mark.parametrize("noise", [150.0, 400.0, 1500.0])
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
def test_noise_of_any_size_never_yields_a_statistic(
    lib: ctypes.CDLL, noise: float, seed: int
) -> None:
    ir, ts = _signal(np.array([]), seconds=40.0, noise=noise, seed=seed)
    det = _Detector(lib)
    det.feed(ir, ts)
    assert det.hrv() is None


def test_intervals_are_taken_only_when_a_frame_can_leave() -> None:
    """They are consumed when reported: taking them on a tick whose frame is held back loses them."""
    main = (BAND / "main" / "app_main.c").read_text(encoding="utf-8").replace("\r\n", "\n")
    block = main[main.index("if (synapse_ble_gatt_feature_v2_due())") :]
    block = block[
        : block.index("const esp_err_t feature_rc = synapse_ble_gatt_push_feature_v2(&v2);")
    ]
    assert "synapse_rr_peek(&s_rr, rr_units, 4)" in block
    assert "synapse_rr_take(" not in block, "taking would consume the intervals before the push"
    after = main[main.index("(void)feature_rc") - 400 : main.index("(void)feature_rc")]
    assert "rr_in_frame && feature_rc == ESP_OK" in after
    assert "synapse_rr_commit(&s_rr);" in after
    gatt = (BAND / "ble" / "ble_gatt_server.c").read_text(encoding="utf-8").replace("\r\n", "\n")
    due = gatt[gatt.index("bool synapse_ble_gatt_feature_v2_due(void) {") :]
    due = due[: due.index("\n}\n")]
    push = gatt[gatt.index("esp_err_t synapse_ble_gatt_push_feature_v2(") :]
    push = push[: push.index("\n}\n")]
    # one gate, one timestamp: due() and push() cannot disagree
    assert "s_feat2_last_ms" in due
    assert "s_feat2_last_ms" in push
    assert "synapse_ble_gatt_feature_v2_subscribed()" in due


def test_more_than_one_interval_per_frame_is_reported_not_hidden() -> None:
    main = (BAND / "main" / "app_main.c").read_text(encoding="utf-8")
    assert "v2.rr_count = (uint8_t)(n_rr < 254 ? n_rr : 254);" in main
    assert "v2.rr_last = rr_units[held - 1];" in main  # the newest one
    assert "ble_gatt_notify_hr(hr_bpm, rr" not in main, "the standard characteristic carries no RR"
