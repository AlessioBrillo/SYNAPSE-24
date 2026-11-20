"""MVP-1.9 W2: the BLE link, the v2 flags, the electrode contact and the POWER battery.

* ``ecg_contact.h`` is pure C and is compiled and run here.
* The rest only builds under ESP-IDF, so it is guarded statically: these tests prove the fixes are in
  the source, not that the band behaves (that needs a board). The ESP-IDF build of ``band-core``
  compiles every one of these call sites.
"""

from __future__ import annotations

import ctypes
import re
from pathlib import Path

import pytest

from tests.firmware_host import build, try_load

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
COMMON = ROOT / "firmware" / "common"
GLUE = ROOT / "tests" / "firmware_host"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start : source.index("\n}\n", start)]


# --- the electrode contact decides when the frozen bias is measured again ---------------------------


@pytest.fixture(scope="module")
def contact(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    out = tmp_path_factory.mktemp("contact")
    lib = try_load(
        build(out, [GLUE / "host_glue_ecg_contact.c"], extra_includes=[str(COMMON / "sensors")])
    )
    if lib is None:
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    lib.host_contact_feed.argtypes = [ctypes.c_int]
    lib.host_contact_feed.restype = ctypes.c_int
    lib.host_contact_reset.restype = None
    lib.host_contact_off_min.restype = ctypes.c_uint32
    lib.host_contact_settle.restype = ctypes.c_uint32
    return lib


def _feed(lib: ctypes.CDLL, lead_off: bool, n: int) -> list[int]:
    return [lib.host_contact_feed(1 if lead_off else 0) for _ in range(n)]


def test_a_start_up_with_good_contact_never_asks_for_a_new_bias(contact: ctypes.CDLL) -> None:
    contact.host_contact_reset()
    assert not any(_feed(contact, False, 5000))


def test_electrodes_applied_after_a_real_lead_off_measure_the_bias_once(
    contact: ctypes.CDLL,
) -> None:
    contact.host_contact_reset()
    off, settle = contact.host_contact_off_min(), contact.host_contact_settle()
    assert not any(_feed(contact, True, off))  # off for a second: nothing yet
    hits = _feed(contact, False, settle + 1000)
    assert hits.index(1) == settle - 1, "the bias is measured after the contact has settled"
    assert sum(hits) == 1, "once, not on every sample that follows"


def test_a_flickering_contact_does_not_interrupt_the_stream(contact: ctypes.CDLL) -> None:
    contact.host_contact_reset()
    off = contact.host_contact_off_min()
    for _ in range(50):  # brief lead-offs, far shorter than a real one
        assert not any(_feed(contact, True, off // 10))
        assert not any(_feed(contact, False, 100))


def test_contact_that_does_not_settle_waits(contact: ctypes.CDLL) -> None:
    contact.host_contact_reset()
    off, settle = contact.host_contact_off_min(), contact.host_contact_settle()
    _feed(contact, True, off)
    assert not any(_feed(contact, False, settle - 1))
    assert not any(_feed(contact, True, 1))  # lost again before it settled: the count restarts
    assert not any(_feed(contact, False, settle - 1))
    assert _feed(contact, False, 1) == [1]


# --- ECG driver ----------------------------------------------------------------------------------------


def test_the_lead_off_pin_is_pulled_down_so_an_unwired_pin_flags_nothing() -> None:
    code = _text(COMMON / "sensors" / "ecg_ad8232.cpp")
    pin = code[code.index("if (config->gpio_lead_off >= 0) {") :]
    pin = pin[: pin.index("gpio_config(&io_conf);")]
    assert ".pull_down_en = GPIO_PULLDOWN_ENABLE" in pin
    assert ".pull_up_en = GPIO_PULLUP_DISABLE" in pin


def test_the_ecg_driver_reports_its_calibration_gap_and_remeasures_on_contact() -> None:
    code = _text(COMMON / "sensors" / "ecg_ad8232.cpp")
    read = _body(code, "esp_err_t ecg_ad8232_read(")
    assert "ecg_contact_update(&s_ctx.contact, s_ctx.lead_off_detected)" in read
    assert "s_ctx.config.midrail_mv <= 0.0f" in read, "an explicit bias is trusted, never replaced"
    assert read.count("s_ctx.lost_unreported++") == 1
    assert "uint32_t ecg_ad8232_take_lost(" in code
    sensors = _text(BAND / "sensors" / "synapse_sensors.c")
    assert ".take_lost = ecg_ad8232_take_lost" in sensors


# --- v2 flags ----------------------------------------------------------------------------------------------


def test_lead_off_and_clock_wrapped_are_set_in_both_the_feature_frame_and_the_status() -> None:
    main = _text(BAND / "main" / "app_main.c")
    flags = _body(main, "static uint8_t common_flags(void)")
    assert "SYNAPSE_V2_FLAG_LEAD_OFF" in flags
    assert "synapse_sensors_ecg_lead_off()" in flags
    assert "SYNAPSE_V2_FLAG_CLOCK_WRAPPED" in flags
    assert ">> 32" in flags, "t_dev_ms wraps when the uptime in ms leaves 32 bits"
    assert "st->flags = common_flags();" in main
    assert "v2.flags |= common_flags();" in main


def test_the_lead_off_accessor_flags_nothing_when_the_ecg_did_not_initialise() -> None:
    sensors = _text(BAND / "sensors" / "synapse_sensors.c")
    body = _body(sensors, "bool synapse_sensors_ecg_lead_off(void)")
    assert "s_sensor_health & (1u << SENSOR_TYPE_ECG)" in body


# --- the BLE link -----------------------------------------------------------------------------------------


def test_the_mtu_and_mbuf_pool_match_the_contract() -> None:
    defaults = _text(BAND / "sdkconfig.defaults")
    assert "CONFIG_BT_NIMBLE_ATT_PREFERRED_MTU=247" in defaults
    blocks = re.search(r"CONFIG_BT_NIMBLE_MSYS_1_BLOCK_COUNT=(\d+)", defaults)
    assert blocks is not None
    assert int(blocks.group(1)) >= 32
    yaml_text = _text(ROOT / "config" / "hardware.yaml")
    assert "mtu_target: 247" in yaml_text


def test_a_new_connection_asks_for_a_faster_link_and_a_refusal_is_not_an_error() -> None:
    code = _text(BAND / "ble" / "ble_gatt_server.c")
    tune = _body(code, "static void tune_link(uint16_t conn_handle)")
    assert "ble_gap_update_params(conn_handle, &params)" in tune
    assert "ble_gap_set_data_len(conn_handle, 251, 2120)" in tune
    assert "ble_gap_set_prefered_le_phy(" in tune
    assert "BLE_GAP_LE_PHY_2M_MASK" in tune
    assert ".latency = 0" in tune
    for fatal in ("ESP_LOGE", "abort", "ESP_ERROR_CHECK"):
        assert fatal not in tune, fatal
    event = _body(code, "static int gap_event_cb(")
    assert "tune_link(g_conn_handle);" in event
    # the requests come after the MTU exchange and before the success log, in the connect branch
    connect = event[
        event.index("case BLE_GAP_EVENT_CONNECT:") : event.index("case BLE_GAP_EVENT_DISCONNECT:")
    ]
    assert connect.index("ble_gattc_exchange_mtu") < connect.index("tune_link(")


# --- POWER ------------------------------------------------------------------------------------------------


def test_the_power_stream_reports_a_battery_only_when_the_rail_is_a_cell() -> None:
    power = _text(BAND / "power" / "synapse_power.c")
    assert "#define SYNAPSE_POWER_BATTERY_MIN_V 3.0f" in power
    assert "#define SYNAPSE_POWER_BATTERY_MAX_V 4.4f" in power
    sink = power[power.index("if (g_telemetry_sink != NULL) {") :]
    sink = sink[:900]
    assert "battery_plausible ? (float)g_status.battery_level_pct : -1.0f" in sink
    # the Battery Service uses the same range: one definition, not two
    assert "g_status.voltage_v >= SYNAPSE_POWER_BATTERY_MIN_V" in power
    assert "voltage_v >= 3.0f" not in power


def test_the_energy_reserve_comes_from_the_configuration() -> None:
    power = _text(BAND / "power" / "synapse_power.c")
    assert "300.0f" not in power
    assert power.count("hw_cfg.reserve_mah") == 3


def test_the_contact_history_is_kept_while_the_first_bias_is_measured() -> None:
    read = _body(_text(COMMON / "sensors" / "ecg_ad8232.cpp"), "esp_err_t ecg_ad8232_read(")
    update = read.index("ecg_contact_update(")
    first_bias = read.index("if (!s_ctx.midrail_ready) {")
    assert update < first_bias, "electrodes off through the start-up second must be remembered"
    assert "s_ctx.midrail_ready &&" not in read[update - 80 : update + 200]


def test_a_refused_heart_rate_notification_is_retried_without_waiting_a_second() -> None:
    code = _text(BAND / "ble" / "ble_gatt_server.c")
    push = _body(code, "esp_err_t synapse_ble_gatt_push_heart_rate(")
    assert "rc == ESP_ERR_NO_MEM || rc == ESP_FAIL" in push
    assert "s_last_hr_ms = now_ms - 1000u" in push
