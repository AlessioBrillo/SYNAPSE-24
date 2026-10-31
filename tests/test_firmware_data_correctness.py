"""Static guards for the Synapse Band data-correctness fixes (MVP-1.3 PR-C).

First light (PR-B) made the band boot and stream. These defects made the data
wrong, and the host C gate cannot see them because it never compiles the ECG
driver, the power module or the GATT server:

* the AD8232 output (a ~1.65 V bias plus the ECG) was divided by the gain
  without removing the bias, so the research stream sat at full scale;
* the POWER stream was emitted from the 10 ms main loop (100 Hz, ~2.8 kB/s
  against a 64 B/s budget), the measured current was overwritten by the tier
  model, and the first battery reading was always 0 V;
* the SYNC reply stamped ``pod_send`` at the write instead of the read, biasing
  the clock offset by half the write->read gap;
* 0x2A37 carried RR in milliseconds; the standard unit is 1/1024 s;
* the measured battery level never reached the Battery Service.

The behavioural fixes inside ``ble_research_service.c`` (per-sensor timestamp
check, wrap witness reset) are covered by real host tests in
``test_research_service_host.py``. The assertions here are static: they prove
the fixes are still in place, not that the band measures correctly (that needs
the hardware: ``docs/hardware/BRINGUP_SOP.md``).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
COMMON = ROOT / "firmware" / "common"

ECG = COMMON / "sensors" / "ecg_ad8232.cpp"
POWER = BAND / "power" / "synapse_power.c"
GATT = BAND / "ble" / "ble_gatt_server.c"
RESEARCH = BAND / "ble" / "ble_research_service.c"
APP_MAIN = BAND / "main" / "app_main.c"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code(path: Path) -> str:
    text = re.sub(r"/\*.*?\*/", "", _src(path), flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _function(text: str, signature: str) -> str:
    """Body of the function that starts at ``signature`` (brace matched)."""
    start = text.index(signature)
    depth, i = 0, text.index("{", start)
    begin = i
    while True:
        depth += text[i] == "{"
        depth -= text[i] == "}"
        i += 1
        if depth == 0:
            return text[begin:i]


# --- ECG ----------------------------------------------------------------------


def test_the_ecg_driver_removes_the_ad8232_bias_before_dividing_by_the_gain() -> None:
    body = _function(_code(ECG), "esp_err_t ecg_ad8232_read(")
    assert "- s_ctx.midrail_mv) / gain" in body
    # the bias is measured over the first samples; those reads deliver nothing
    assert "ECG_MIDRAIL_CAL_SAMPLES" in body
    assert "return ESP_ERR_NOT_FOUND" in body
    # an explicit bias skips the calibration and the tracking
    assert "config->midrail_mv > 0.0f" in _code(ECG)
    # otherwise the bias keeps being tracked: a start-up with the electrodes off recovers
    assert "ECG_MIDRAIL_TRACK_ALPHA" in body


def test_the_ecg_stream_is_not_gated_on_the_lead_off_pin() -> None:
    # An unwired LO pin (pull-up) reads "off" forever: gating on it would silence the stream.
    body = _function(_code(ECG), "esp_err_t ecg_ad8232_read(")
    assert not re.search(r"if\s*\(\s*s_ctx\.lead_off_detected", body)


# --- power --------------------------------------------------------------------


def test_the_power_tick_runs_at_one_hertz_not_at_the_main_loop_rate() -> None:
    body = _function(_code(POWER), "void synapse_power_tick(void)")
    gate = body.index("(run_us - g_last_tick_us) < 1000000")
    # the gate comes before any ADC read or telemetry emission
    assert gate < body.index("battery_mv_unchecked()")
    assert gate < body.index("g_telemetry_sink(")


def test_the_measured_current_is_not_overwritten_by_the_tier_model() -> None:
    body = _function(_code(POWER), "void synapse_power_tick(void)")
    assert "current_measured = true" in body
    assert re.search(
        r"if \(!current_measured\) \{[^}]*g_status\.current_ma = \(current_mw / model_v\)", body
    )


def test_the_first_battery_reading_is_taken_before_init_completes() -> None:
    code = _code(POWER)
    init = _function(code, "esp_err_t synapse_power_init(void)")
    assert "battery_mv_unchecked()" in init
    assert "synapse_power_read_battery_mv()" not in init  # that one returns 0 until g_initialized
    # declared before its first use, or the C does not compile
    assert code.index("static uint16_t battery_mv_unchecked(void);") < code.index(
        "battery_mv_unchecked()"
    )


def test_the_measured_battery_level_reaches_the_ble_service() -> None:
    # the power module owns the policy; app_main only wires the sink
    assert "synapse_power_set_battery_sink(synapse_ble_gatt_battery_changed)" in _code(APP_MAIN)
    tick = _function(_code(POWER), "void synapse_power_tick(void)")
    # a missing divider (USB-powered kit) must not publish noise
    assert "g_status.voltage_v >= 3.0f && g_status.voltage_v <= 4.4f" in tick
    assert "g_battery_sink(" in tick


def test_a_missing_battery_divider_does_not_decay_the_voltage_to_zero() -> None:
    tick = _function(_code(POWER), "void synapse_power_tick(void)")
    assert "if (mv > 0)" in tick
    # the tier-model current never divides by an unmeasured (zero) voltage
    assert "g_status.voltage_v > 1.0f ? g_status.voltage_v : 3.7f" in tick
    assert "g_last_tick_us = 0;" in _function(_code(POWER), "esp_err_t synapse_power_deinit(void)")


# --- clock sync ---------------------------------------------------------------


def test_the_sync_reply_is_stamped_when_it_leaves_not_when_the_request_arrives() -> None:
    body = _function(_code(RESEARCH), "static int sync_access(")
    read_branch = body[
        body.index("BLE_GATT_ACCESS_OP_READ_CHR") : body.index("BLE_GATT_ACCESS_OP_WRITE_CHR")
    ]
    assert "esp_timer_get_time()" in read_branch
    assert "s_sync_response + 12" in read_branch
    write_branch = body[body.index("BLE_GATT_ACCESS_OP_WRITE_CHR") :]
    assert "send_us = esp_timer_get_time()" not in write_branch
    # a read with no request pending returns the zeroed buffer, not a half-made reply
    assert "if (s_sync_valid)" in read_branch
    assert "s_sync_valid = true" in write_branch


def test_an_empty_poll_is_not_counted_as_a_read_error() -> None:
    sched = (COMMON / "scheduler" / "sensor_scheduler.cpp").read_text(encoding="utf-8")
    task = sched.split("void sensor_task_fn", 1)[1]
    assert "rd != ESP_ERR_NOT_FOUND" in task


# --- Heart Rate RR unit -------------------------------------------------------


def _rr_ms_to_1024(rr_ms: int) -> int:
    """Python mirror of ``rr_ms_to_1024`` in ble_gatt_server.c (same integer maths)."""
    v = (rr_ms * 1024 + 500) // 1000
    return min(v, 0xFFFE)


def test_the_heart_rate_rr_is_sent_in_1024ths_of_a_second() -> None:
    code = _code(GATT)
    body = _function(code, "static uint16_t rr_ms_to_1024(")
    assert "* 1024u + 500u) / 1000u" in body
    # both the read path and the notify path convert; neither sends raw milliseconds
    assert code.count("rr_ms_to_1024(") == 3  # definition + read + notify
    assert "val[3] = (uint8_t)(rr & 0xFF)" not in code
    assert "val[n++] = (uint8_t)(rr_interval_ms & 0xFF)" not in code
    assert "project-specific" not in _src(GATT)


def test_the_rr_conversion_matches_the_standard_unit() -> None:
    assert _rr_ms_to_1024(1000) == 1024
    assert _rr_ms_to_1024(800) == 819  # 819.2 rounded
    assert _rr_ms_to_1024(0) == 0
    assert _rr_ms_to_1024(0xFFFE) == 0xFFFE  # clamped below the 0xFFFF "no RR" sentinel
