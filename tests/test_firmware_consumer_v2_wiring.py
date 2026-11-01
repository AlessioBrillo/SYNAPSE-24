"""Static guards for the consumer contract v2 wiring (MVP-1.3 PR-D).

The encoders, the SDK codec and the generated contract are proven byte for byte
by ``test_consumer_v2_parity.py``. What cannot be compiled on the host is the
NimBLE glue (``ble_gatt_server.c``) and ``app_main.c``; these assertions scan
them for the structures that make the v2 characteristics reachable, so a
refactor cannot silently drop one. They prove the wiring is present, not that a
phone can use it: that needs the hardware (``docs/hardware/BRINGUP_SOP.md``).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
GATT = BAND / "ble" / "ble_gatt_server.c"
APP_MAIN = BAND / "main" / "app_main.c"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10))


def _code(path: Path) -> str:
    text = re.sub(r"/\*.*?\*/", "", _src(path), flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _consumer_service_table(code: str) -> str:
    start = code.index(".uuid = &SYN_SVC_UUID.u")
    return code[start : code.index(chr(10) + "};", start)]


def test_the_four_v2_characteristics_sit_on_the_consumer_service() -> None:
    table = _consumer_service_table(_code(GATT))
    for uuid in ("FEAT2_UUID", "STATUS_UUID", "META_UUID", "CTL_UUID"):
        assert f".uuid = &{uuid}.u" in table, uuid
    # v1 stays exactly where it was
    for uuid in ("FEAT_UUID", "CFG_UUID", "PROV_UUID"):
        assert f".uuid = &{uuid}.u" in table, uuid


def test_each_v2_characteristic_has_the_access_the_contract_gives_it() -> None:
    table = _consumer_service_table(_code(GATT))

    def flags(uuid: str) -> str:
        block = table[table.index(f".uuid = &{uuid}.u") :]
        return block[block.index(".flags =") : block.index("}")]

    assert "BLE_GATT_CHR_F_NOTIFY" in flags("FEAT2_UUID")
    assert "READ" not in flags(
        "FEAT2_UUID"
    )  # the value lives in the notification, not the database
    assert "BLE_GATT_CHR_F_READ" in flags("STATUS_UUID")
    assert "BLE_GATT_CHR_F_NOTIFY" in flags("STATUS_UUID")
    assert "BLE_GATT_CHR_F_READ" in flags("META_UUID")
    assert "WRITE" not in flags("META_UUID")
    assert "BLE_GATT_CHR_F_WRITE" in flags("CTL_UUID")
    assert "NOTIFY" not in flags("CTL_UUID")


def test_the_v2_value_handles_come_from_the_service_table() -> None:
    code = _code(GATT)
    assert ".val_handle = &s_feat2_handle" in code
    assert ".val_handle = &s_status_handle" in code
    assert "s_feat2_subscribed = event->subscribe.cur_notify" in code
    assert "s_status_subscribed = event->subscribe.cur_notify" in code


def test_a_disconnect_resets_the_v2_session_state() -> None:
    code = _code(GATT)
    disconnect = code[code.index("case BLE_GAP_EVENT_DISCONNECT:") :]
    disconnect = disconnect[: disconnect.index("case BLE_GAP_EVENT_SUBSCRIBE:")]
    for needle in (
        "s_feat2_subscribed = false",
        "s_status_subscribed = false",
        "s_feat2_seq = 0",
        "s_feat2_rate_hz = SYNAPSE_CONTRACT_V2_FEATURE_RATE_HZ",
    ):
        assert needle in disconnect, needle


def test_the_v2_feature_is_sent_only_when_the_mtu_can_carry_it() -> None:
    code = _code(GATT)
    body = code[code.index("esp_err_t synapse_ble_gatt_push_feature_v2(") :]
    body = body[: body.index("esp_err_t synapse_ble_gatt_push_status(")]
    assert "g_mtu < SYNAPSE_CONTRACT_V2_FEATURE_BYTES + 3u" in body
    assert "ESP_ERR_INVALID_SIZE" in body
    # the sequence is consumed by a frame that was queued, so a refusal leaves no phantom gap
    assert body.index("rc == ESP_OK") < body.index("s_feat2_seq++")
    assert "1000u / s_feat2_rate_hz" in body


def test_the_battery_is_reported_only_once_it_was_measured() -> None:
    code = _code(GATT)
    body = code[code.index("esp_err_t synapse_ble_gatt_push_feature_v2(") :]
    assert "if (s_batt_known)" in body
    assert (
        "s_batt_known = true" in code
    )  # set by the power module's update, not by the placeholder 100


def test_a_control_point_failure_rides_in_the_att_application_error_range() -> None:
    code = _code(GATT)
    body = code[code.index("static int ctl_access(") :]
    body = body[: body.index("\n}\n")]
    assert "synapse_control_apply(req, len, &ops)" in body
    assert "0x80 + err" in body  # ATT application errors: 0x80..0x9F


def test_the_status_mtu_is_the_negotiated_one() -> None:
    body = _code(GATT)
    body = body[body.index("static size_t build_status(") :]
    body = body[: body.index("\n}\n")]
    assert "st.mtu = g_mtu" in body


def test_v1_push_is_untouched_by_v2() -> None:
    code = _code(GATT)
    body = code[code.index("esp_err_t synapse_ble_gatt_push_feature(") :]
    body = body[: body.index("static esp_err_t notify_flat(")]
    assert "f.version = SYNAPSE_FEATURE_STREAM_VERSION" in body
    assert "feat2" not in body.lower()


def test_app_main_wires_the_provider_the_handler_and_the_v2_push() -> None:
    main = _code(APP_MAIN)
    assert "synapse_ble_gatt_set_status_provider(fill_status)" in main
    assert "synapse_ble_gatt_set_transmit_handler(on_transmit_request)" in main
    assert "synapse_feature_v2_init_na(&v2)" in main  # unmeasured fields stay at NA, never 0
    assert "synapse_ble_gatt_push_feature_v2(&v2)" in main
    assert "synapse_ble_gatt_push_status()" in main
    # a stop from the control point must reach the raw streams, not just a flag
    handler = main[main.index("static void on_transmit_request(") :]
    assert "synapse_research_service_set_transmitting(on)" in handler[: handler.index("\n}\n")]


def test_the_build_files_know_about_the_new_sources() -> None:
    assert "synapse_consumer_v2.c" in _src(BAND / "ble" / "CMakeLists.txt")
    assert "esp_timer" in _src(BAND / "main" / "CMakeLists.txt")
    assert "synapse_consumer_v2.c" in _src(ROOT / "scripts" / "gcc_probe.py")


def test_the_mtu_is_per_connection_and_the_device_asks_for_a_larger_one() -> None:
    code = _code(GATT)
    connect = code[code.index("case BLE_GAP_EVENT_CONNECT:") :]
    connect = connect[: connect.index("case BLE_GAP_EVENT_DISCONNECT:")]
    assert "g_mtu = 23" in connect  # never the previous peer's MTU
    assert "ble_gattc_exchange_mtu(g_conn_handle" in connect
    disconnect = code[code.index("case BLE_GAP_EVENT_DISCONNECT:") :]
    disconnect = disconnect[: disconnect.index("case BLE_GAP_EVENT_SUBSCRIBE:")]
    assert "g_mtu = 23" in disconnect


def test_a_new_session_starts_with_the_raw_streams_running() -> None:
    code = _code(GATT)
    disconnect = code[code.index("case BLE_GAP_EVENT_DISCONNECT:") :]
    disconnect = disconnect[: disconnect.index("case BLE_GAP_EVENT_SUBSCRIBE:")]
    assert "s_transmit_handler(true)" in disconnect


def test_a_too_small_mtu_is_reported_once_not_swallowed() -> None:
    code = _code(GATT)
    body = code[code.index("esp_err_t synapse_ble_gatt_push_feature_v2(") :]
    body = body[: body.index("esp_err_t synapse_ble_gatt_push_status(")]
    assert "s_mtu_warned" in body
    assert "ESP_LOGW(BLE_TAG" in body


def test_a_perfect_signal_cannot_be_reported_as_not_available() -> None:
    main = _code(APP_MAIN)
    helper = main[main.index("static uint8_t unit_to_u8(") :]
    helper = helper[: helper.index("\n}\n")]
    assert "return 254" in helper  # 255 is the NA sentinel
    assert "!(v > 0.0f)" in helper  # NaN and negatives
    assert "* 255.0f" not in main
    assert "ppg_sqi = unit_to_u8(ppg.sqi)" in main
