"""MVP-1.4 S4: what a stranger within BLE range can and cannot do to the band.

``ble_gatt_server.c`` only builds under ESP-IDF and links NimBLE, so these are static guards: they
prove the fixes are in place, not that the radio behaves. The audit found:

* the provisioning handler trusted the ``data_len`` of the header without comparing it with the bytes
  the packet carried, so a short packet made it read (and act on) uninitialised stack bytes, and the
  result of the command was dropped;
* anybody could write the provisioning (COMMIT, FACTORY_RESET) and configuration characteristics
  without even an encrypted link;
* the configuration write was accepted and ignored (and echoed back on a read).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATT = ROOT / "firmware" / "esp32_band" / "ble" / "ble_gatt_server.c"


def _code() -> str:
    return GATT.read_text(encoding="utf-8").replace("\r\n", "\n")


def _function(name: str) -> str:
    code = _code()
    start = code.index(name)
    return code[start : code.index("\n}\n", start)]


def _flags(uuid_symbol: str) -> str:
    code = _code()
    block = code[code.index(f".uuid = &{uuid_symbol}.u") :]
    return block[: block.index("},")]


def test_the_provisioning_length_is_checked_against_the_packet() -> None:
    body = _function("static int prov_access(")
    assert "(uint32_t)data_len + 4u != len" in body, "the header's length must equal the real one"
    assert "uint8_t payload[240] = {0};" in body  # nothing uninitialised is ever passed on
    assert re.search(r"os_mbuf_copydata\(ctxt->om, 4, data_len, payload\) != 0", body)
    assert re.search(r"os_mbuf_copydata\(ctxt->om, 0, 4, hdr\) != 0", body)


def test_the_result_of_a_provisioning_command_reaches_the_client() -> None:
    body = _function("static int prov_access(")
    assert "const esp_err_t pr = synapse_provisioning_process_cmd(" in body
    assert "if (pr != ESP_OK)" in body
    assert "return BLE_ATT_ERR_UNLIKELY;  // the client must know it did not work" in body


def test_provisioning_and_config_writes_need_an_encrypted_link() -> None:
    assert "BLE_GATT_CHR_F_WRITE_ENC" in _flags("PROV_UUID")
    assert "BLE_GATT_CHR_F_WRITE_ENC" in _flags("CFG_UUID")


def test_the_phone_can_still_set_the_clock_and_pause_the_raw_streams_without_pairing() -> None:
    """Control point writes are harmless (start/stop/rate/time) and the app needs them at once."""
    flags = _flags("CTL_UUID")
    assert "BLE_GATT_CHR_F_WRITE" in flags
    assert "WRITE_ENC" not in flags
    assert "WRITE_AUTHEN" not in flags


def test_nothing_that_notifies_can_be_written() -> None:
    for symbol in ("FEAT2_UUID", "STATUS_UUID", "FEAT_UUID"):
        assert "BLE_GATT_CHR_F_WRITE" not in _flags(symbol), symbol


def test_a_config_write_is_refused_not_swallowed() -> None:
    body = _function("static int cfg_access(")
    assert "BLE_ATT_ERR_WRITE_NOT_PERMITTED" in body
    assert "synapse_config_update_from_ble" not in body
    assert "s_cfg_len = len" not in body, "a write must not be echoed back on a read"


def test_the_pairing_mode_is_secure_connections_just_works_without_a_key_store() -> None:
    code = _code()
    assert "ble_hs_cfg.sm_sc = 1;" in code
    assert "ble_hs_cfg.sm_mitm = 0;" in code
    # bonding needs ble_store_config_init(); without a store, asking for it would fail the pairing
    assert "ble_hs_cfg.sm_bonding = 0;" in code
    assert "ble_store_config_init" not in code
