"""Compile and run the calibration record on the host.

NVS is stubbed in memory, so the serial-binding rule is exercised, not just
asserted by reading the C source. This is the gate that caught the empty-serial
factory bug on the host instead of on the bench.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from tests.firmware_host import build, try_load

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
STUBS = ROOT / "tests" / "firmware_host" / "stubs"
GLUE = ROOT / "tests" / "firmware_host" / "host_glue_calibration.c"

RECORD_BYTES = 128
SERIAL_OFFSET = 30  # version(4) crc(4) gain(4) midrail(4) leds(2) bias(6+6)
ESP_ERR_INVALID_STATE = 0x103


@pytest.fixture(scope="module")
def lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    out = build(
        tmp_path_factory.mktemp("calibration_host"),
        [
            BAND / "config" / "synapse_calibration.c",
            BAND / "main" / "synapse_factory_cli.c",
            GLUE,
        ],
        extra_includes=[str(STUBS), str(BAND / "config"), str(BAND / "main")],
    )
    loaded = try_load(out)
    if loaded is None:  # pragma: no cover - Windows Application Control
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    ptr = ctypes.POINTER(ctypes.c_uint8)
    loaded.host_set_identity.argtypes = [ctypes.c_char_p]
    loaded.host_store_record.argtypes = [ptr]
    loaded.synapse_calibration_load.argtypes = [ptr]
    loaded.synapse_calibration_load.restype = ctypes.c_int
    loaded.synapse_calibration_seal.argtypes = [ptr]
    loaded.synapse_calibration_erase.restype = ctypes.c_int
    loaded.synapse_factory_cli_start.restype = None
    loaded.host_has_record.restype = ctypes.c_int
    return loaded


def _sealed_record(lib: ctypes.CDLL, serial: str) -> ctypes.Array:
    buf = (ctypes.c_uint8 * RECORD_BYTES)()
    ctypes.memset(buf, 0, RECORD_BYTES)
    ctypes.memmove(ctypes.addressof(buf) + SERIAL_OFFSET, serial.encode("ascii"), len(serial))
    lib.synapse_calibration_seal(buf)
    return buf


def test_a_record_for_another_serial_is_refused(lib: ctypes.CDLL) -> None:
    lib.host_set_identity(b"SYN-A1B2C3D4E5F6")
    lib.host_store_record(_sealed_record(lib, "SYN-000000000000"))
    assert lib.synapse_calibration_load(_sealed_record(lib, "")) == ESP_ERR_INVALID_STATE


def test_a_record_for_this_serial_loads(lib: ctypes.CDLL) -> None:
    lib.host_set_identity(b"SYN-A1B2C3D4E5F6")
    lib.host_store_record(_sealed_record(lib, "SYN-A1B2C3D4E5F6"))
    assert lib.synapse_calibration_load(_sealed_record(lib, "")) == 0


def test_factory_start_survives_a_virgin_band(lib: ctypes.CDLL) -> None:
    lib.host_set_identity(b"SYN-000000000000")
    lib.synapse_calibration_erase()
    lib.synapse_factory_cli_start()
    assert lib.host_has_record() == 0
