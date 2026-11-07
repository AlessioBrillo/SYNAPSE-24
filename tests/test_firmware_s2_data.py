"""MVP-1.4 S2: the values the band reports are the values it measured.

Two parts:

* ``synapse_feature_map.h`` is compiled and **run** on the host (it is pure C): a NaN or a negative
  quality is "not available" and never 0, the heart rate is rounded and an implausible one is
  invalid, and a PPG reading older than five seconds is not reported.
* Static guards for what only builds under ESP-IDF (the GATT server, the Kconfig): the L0 heart-rate
  notification no longer depends on the feature characteristic, the battery is never a placeholder,
  and the ECG gain is the board's real one. They prove the fixes are in place, not that the band
  works: that needs the hardware.
"""

from __future__ import annotations

import ctypes
import re
from pathlib import Path

import pytest
import yaml

from tests.firmware_host import build, try_load

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
GATT = BAND / "ble" / "ble_gatt_server.c"
KCONFIG = BAND / "config" / "Kconfig"
APP_MAIN = BAND / "main" / "app_main.c"
GLUE = ROOT / "tests" / "firmware_host" / "host_glue_feature_map.c"


@pytest.fixture(scope="module")
def fm(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = try_load(
        build(
            tmp_path_factory.mktemp("feature_map"),
            [GLUE],
            extra_includes=[str(BAND / "main"), str(BAND / "ble"), str(BAND / "acquisition")],
        )
    )
    if lib is None:
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    lib.host_unit_to_u8.restype = ctypes.c_uint8
    lib.host_unit_to_u8.argtypes = [ctypes.c_float]
    lib.host_hr_to_u16.restype = ctypes.c_uint16
    lib.host_hr_to_u16.argtypes = [ctypes.c_float]
    lib.host_is_fresh.restype = ctypes.c_int
    lib.host_is_fresh.argtypes = [ctypes.c_int64, ctypes.c_int64, ctypes.c_int64]
    lib.host_ppg_max_age_us.restype = ctypes.c_int64
    lib.host_window_is_new.restype = ctypes.c_int
    lib.host_window_is_new.argtypes = [ctypes.POINTER(ctypes.c_int64), ctypes.c_int64]
    lib.host_window_is_current.restype = ctypes.c_int
    lib.host_window_is_current.argtypes = [ctypes.c_int64, ctypes.c_int64]
    lib.host_hr_flags.restype = ctypes.c_uint8
    lib.host_hr_flags.argtypes = [ctypes.c_int]
    lib.host_hr_is_readable.restype = ctypes.c_int
    lib.host_hr_is_readable.argtypes = [ctypes.c_int64, ctypes.c_int64]
    return lib


# --- the mapping rules, executed --------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), -0.001, -1.0, float("-inf")])
def test_a_quality_that_was_not_measured_is_not_available_never_zero(
    fm: ctypes.CDLL, bad: float
) -> None:
    assert fm.host_unit_to_u8(bad) == 255


def test_the_unit_interval_maps_to_0_254_and_never_reaches_the_sentinel(fm: ctypes.CDLL) -> None:
    assert fm.host_unit_to_u8(0.0) == 0  # a real zero is a value
    assert fm.host_unit_to_u8(0.5) == 127
    assert fm.host_unit_to_u8(1.0) == 254
    assert fm.host_unit_to_u8(7.0) == 254  # above range clamps; it must not wrap the cast
    assert fm.host_unit_to_u8(float("inf")) == 254
    assert fm.host_unit_to_u8(0.99999994) == 254  # the largest float below 1
    previous = -1
    for i in range(1001):  # monotonic, and 255 is never produced
        value = fm.host_unit_to_u8(i / 1000)
        assert value != 255
        assert value >= previous
        previous = value


def test_the_heart_rate_is_rounded_not_truncated(fm: ctypes.CDLL) -> None:
    assert fm.host_hr_to_u16(71.9) == 72
    assert fm.host_hr_to_u16(71.4) == 71
    assert fm.host_hr_to_u16(62.0) == 62


@pytest.mark.parametrize("bad", [0.0, 0.4, 19.9, 300.5, 5000.0, float("nan"), -60.0])
def test_an_implausible_heart_rate_is_invalid_not_a_number(fm: ctypes.CDLL, bad: float) -> None:
    assert fm.host_hr_to_u16(bad) == 0xFFFF


def test_a_ppg_reading_goes_stale(fm: ctypes.CDLL) -> None:
    max_age = fm.host_ppg_max_age_us()
    assert max_age == 5_000_000
    now = 100_000_000
    assert fm.host_is_fresh(now, now - 1_000_000, max_age) == 1
    assert fm.host_is_fresh(now, now - max_age, max_age) == 1
    assert fm.host_is_fresh(now, now - max_age - 1, max_age) == 0  # the finger may be long gone
    assert fm.host_is_fresh(now, 0, max_age) == 0  # never stamped: not a reading
    assert fm.host_is_fresh(now, now + 1, max_age) == 0  # a stamp from the future is a bug


def test_a_quality_window_is_counted_once_however_often_the_fsm_ticks(fm: ctypes.CDLL) -> None:
    """The FSM ticks every 10 ms, the quality module publishes once per window.

    Counting ticks made 'five minutes of stillness' pass in 100 ms: 500 ticks over 5 windows
    must count 5 windows.
    """
    last = ctypes.c_int64(0)
    counted = 0
    stamp = 1_000_000
    for tick in range(500):
        if tick % 100 == 0:  # a new reading every 100 ticks
            stamp += 1_000_000
        counted += fm.host_window_is_new(ctypes.byref(last), stamp)
    assert counted == 5
    assert fm.host_window_is_new(ctypes.byref(last), 0) == 0, "an unstamped reading is no window"


def test_a_stale_quality_reading_is_not_current(fm: ctypes.CDLL) -> None:
    now = 100_000_000
    assert fm.host_window_is_current(now, now - 1_000_000) == 1
    assert fm.host_window_is_current(now, now - 5_000_000) == 1
    assert fm.host_window_is_current(now, now - 5_000_001) == 0, (
        "the finger left; `valid` stays set"
    )
    assert fm.host_window_is_current(now, 0) == 0
    assert fm.host_window_is_current(now, now + 1) == 0


def test_the_standard_heart_rate_reports_the_sensor_contact(fm: ctypes.CDLL) -> None:
    """Bits 1-2 of 0x2A37 are 0b11 (supported, detected): the value exists only with a finger on."""
    assert fm.host_hr_flags(0) == 0b0000_0111
    assert fm.host_hr_flags(1) == 0b0001_0111  # RR present adds bit 4 and nothing else
    assert (fm.host_hr_flags(0) >> 1) & 0b11 == 0b11


def test_a_heart_rate_read_needs_a_fresh_measurement(fm: ctypes.CDLL) -> None:
    """0x2A37 has no 'not available' code, so a stale or never-built value must not be readable."""
    now = 100_000_000
    assert fm.host_hr_is_readable(now, 0) == 0, "never measured: 65535 bpm would read as a value"
    assert fm.host_hr_is_readable(now, now - 1_000_000) == 1
    assert fm.host_hr_is_readable(now, now - 3_000_000) == 1
    assert fm.host_hr_is_readable(now, now - 3_000_001) == 0, "the finger left, the old value stays"
    assert fm.host_hr_is_readable(now, now + 1) == 0, "a stamp from the future is a bug"


def test_app_main_applies_the_freshness_rule_to_the_ppg_values() -> None:
    main = APP_MAIN.read_text(encoding="utf-8")
    block = main[main.index("if (ppg.valid && synapse_is_fresh(") :]
    block = block[: block.index("}")]
    assert "SYNAPSE_PPG_MAX_AGE_US" in block
    assert "synapse_hr_to_u16(ppg.hr_bpm)" in block
    assert "(uint16_t)ppg.hr_bpm" not in main  # truncation


# --- L0: the band as a plain heart-rate strap ------------------------------------------------------


def _push_feature_body() -> str:
    code = GATT.read_text(encoding="utf-8")
    body = code[code.index("esp_err_t synapse_ble_gatt_push_feature(") :]
    return body[: body.index("static esp_err_t notify_flat(")]


def test_the_standard_heart_rate_is_notified_before_and_apart_from_the_feature_stream() -> None:
    """A client that subscribed only to 0x2A37 (Averyn's generic HR adapter) must get its beats."""
    body = _push_feature_body()
    hr = body.index("synapse_ble_gatt_notify_hr(")
    feature_gate = body.index("if (!g_feat_subscribed")
    assert hr < feature_gate, "HR is behind the feature-subscription check: L0 stays silent"
    assert "s_hr_subscribed" in body[:feature_gate]
    assert body.count("synapse_ble_gatt_notify_hr(") == 1, "the old mirror inside the feature path"
    assert "s_last_hr_ms" in body  # its own 1 Hz gate


# --- the battery is never a placeholder ----------------------------------------------------------------


def test_an_unmeasured_battery_is_not_reported_as_full() -> None:
    body = _push_feature_body()
    assert "f.battery_level = s_batt_known ? s_batt_level : 0xFF;" in body
    assert "f.charging = 0; //" not in body
    code = GATT.read_text(encoding="utf-8")
    access = code[code.index("static int batt_access(") :]
    access = access[: access.index("\n}\n")]
    assert "if (!s_batt_known) return BLE_ATT_ERR_UNLIKELY;" in access


# --- the ECG gain ------------------------------------------------------------------------------------------


def _kconfig_default(symbol: str) -> int:
    m = re.search(rf"config {symbol}\n(?:.*\n)*?\s+default (-?\d+)", KCONFIG.read_text("utf-8"))
    assert m, symbol
    return int(m.group(1))


def test_the_ecg_gain_is_the_boards_and_agrees_with_the_manifest() -> None:
    kconfig_gain = _kconfig_default("SYNAPSE_ECG_GAIN") / 100.0
    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    ecg = next(
        p["metadata"]["ecg_config"]
        for p in manifest["pods"]
        if "ecg_config" in p.get("metadata", {})
    )
    assert kconfig_gain == ecg["gain"] == 1100.0
    # the host bring-up scripts already assumed the same total gain
    for script in ("hardware_bringup.py", "validate_phase1_entry.py"):
        assert "1.0 / 1100.0" in (ROOT / "scripts" / script).read_text(encoding="utf-8")


def test_a_real_qrs_fits_the_16_bit_microvolt_range_at_that_gain() -> None:
    """The research ECG is int16 in microvolts (+-32.7 mV): the old gain of 6 clipped every beat."""
    gain = _kconfig_default("SYNAPSE_ECG_GAIN") / 100.0
    adc_swing_mv = 1100.0  # a 1 mV R wave through a 1100x chain, from the mid-rail
    electrode_uv = adc_swing_mv / gain * 1000.0
    assert electrode_uv == pytest.approx(1000.0)
    assert electrode_uv < 32767
    old_uv = adc_swing_mv / 6.0 * 1000.0
    assert old_uv > 32767, "the regression this test exists for"


# --- S5: when RR and HRV may be published -----------------------------------------------------------------


def test_rr_is_published_only_from_a_fresh_clean_reading(fm: ctypes.CDLL) -> None:
    fm.host_rr_publishable.argtypes = [ctypes.c_int, ctypes.c_float, ctypes.c_float]
    fm.host_rr_publishable.restype = ctypes.c_int
    assert fm.host_rr_publishable(1, 0.8, 0.1) == 1
    assert fm.host_rr_publishable(1, 0.5, 0.3) == 1  # the limits are inclusive
    assert fm.host_rr_publishable(0, 0.9, 0.0) == 0  # a stale reading
    assert fm.host_rr_publishable(1, 0.49, 0.0) == 0  # signal too poor
    assert fm.host_rr_publishable(1, 0.9, 0.31) == 0  # too much motion
    assert fm.host_rr_publishable(1, float("nan"), 0.0) == 0
    assert fm.host_rr_publishable(1, 0.9, float("nan")) == 0
