"""Compile the shared device-config whitelist parsers on the host and run them.

The parsers live in the production header
``firmware/esp32_band/config/synapse_config_json.h`` and are included by both
the firmware's ble_gatt_server.c and the host harness, so the rules exercised
here are the exact rules the band applies to a BLE config write. No NimBLE
headers are needed.

On the ESP32 ``unsigned long`` is 32-bit, which is why a negative value used to
wrap into a plausible uint32; the host is 64-bit, so the test covers both the
sign guard and the explicit uint32 overflow guard.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from tests.firmware_host import build, try_load

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "tests" / "firmware_host"
GATT = (ROOT / "firmware" / "esp32_band" / "ble" / "ble_gatt_server.c").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    out = build(
        tmp_path_factory.mktemp("cfg_json_host"),
        [
            HOST / "cfg_json_helpers.c",
            HOST / "test_cfg_json_helpers.c",
        ],
    )
    loaded = try_load(out)
    if loaded is None:  # pragma: no cover - Windows Application Control
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    loaded.cfg_json_host_run_all.restype = ctypes.c_int
    return loaded


def test_firmware_shares_the_host_parser_header() -> None:
    assert '#include "synapse_config_json.h"' in GATT
    assert "json_only_whitelisted(" in GATT
    assert "json_u32(" in GATT
    assert "json_i16_array(" in GATT
    helper = (ROOT / "firmware" / "esp32_band" / "config" / "synapse_config_json.h").read_text(
        encoding="utf-8"
    )
    assert "#include <stdio.h>" not in helper  # firmware may not gain a stdio dependency
    assert "errno == ERANGE" in helper
    assert "ULONG_MAX > 0xFFFFFFFFu" in helper  # the 64-bit-host / 32-bit-firmware case
    assert "*p == '-' || *p == '+'" in helper


def test_parsers_reject_the_known_attacks(lib: ctypes.CDLL) -> None:
    assert lib.cfg_json_host_run_all() == 0
