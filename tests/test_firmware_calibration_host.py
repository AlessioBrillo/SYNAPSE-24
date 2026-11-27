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
            ROOT / "tests" / "firmware_host" / "test_calibration_effective_crc.c",
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
    loaded.synapse_calibration_valid.argtypes = [ptr]
    loaded.synapse_calibration_valid.restype = ctypes.c_bool
    loaded.synapse_calibration_crc.argtypes = [ptr]
    loaded.synapse_calibration_crc.restype = ctypes.c_uint32
    loaded.synapse_calibration_effective_crc.argtypes = [ptr]
    loaded.synapse_calibration_effective_crc.restype = ctypes.c_uint32
    loaded.synapse_calibration_mark_applied.argtypes = [ptr]
    loaded.synapse_calibration_applied_crc.restype = ctypes.c_uint32
    loaded.synapse_calibration_erase.restype = ctypes.c_int
    loaded.synapse_factory_cli_start.restype = None
    loaded.host_has_record.restype = ctypes.c_int
    loaded.calibration_effective_host_run_all.restype = ctypes.c_int
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


def _copy_record(src: ctypes.Array) -> ctypes.Array:
    dst = (ctypes.c_uint8 * RECORD_BYTES)()
    ctypes.memmove(dst, src, RECORD_BYTES)
    return dst


def _put_text(buf: ctypes.Array, offset: int, text: bytes) -> None:
    ctypes.memmove(ctypes.addressof(buf) + offset, text, len(text))


def _put_u16(buf: ctypes.Array, offset: int, value: int) -> None:
    ctypes.memmove(ctypes.addressof(buf) + offset, ctypes.byref(ctypes.c_uint16(value)), 2)


def _put_u32(buf: ctypes.Array, offset: int, value: int) -> None:
    ctypes.memmove(ctypes.addressof(buf) + offset, ctypes.byref(ctypes.c_uint32(value)), 4)


def test_effective_crc_ignores_metadata_only_changes(lib: ctypes.CDLL) -> None:
    base = _sealed_record(lib, "SYN-A1B2C3D4E5F6")
    effective = lib.synapse_calibration_effective_crc(base)
    # Everything a USB `cal save` may add after the sensors had already booted.
    edited = _copy_record(base)
    _put_text(edited, SERIAL_OFFSET, b"SYN-METADATA0000000")
    _put_text(edited, 66, b"another-operator")
    _put_text(edited, 82, b"METER-0002")
    _put_text(edited, 106, b"1.2.3")
    _put_u16(edited, 122, 700)  # gain_uncertainty_pct_x100
    _put_u16(edited, 124, 55)  # noise_std_uv_x10
    lib.synapse_calibration_seal(edited)
    assert lib.synapse_calibration_valid(edited) == 1
    assert lib.synapse_calibration_crc(edited) != lib.synapse_calibration_crc(base)
    assert lib.synapse_calibration_effective_crc(edited) == effective


def test_effective_crc_tracks_sensor_effective_values(lib: ctypes.CDLL) -> None:
    base = _sealed_record(lib, "SYN-A1B2C3D4E5F6")
    edited = _copy_record(base)
    _put_u32(edited, 8, 112300)  # ecg_gain_x100
    lib.synapse_calibration_seal(edited)
    assert lib.synapse_calibration_effective_crc(edited) != lib.synapse_calibration_effective_crc(
        base
    )


def test_mark_applied_stores_the_effective_crc(lib: ctypes.CDLL) -> None:
    base = _sealed_record(lib, "SYN-A1B2C3D4E5F6")
    lib.synapse_calibration_mark_applied(base)
    applied = lib.synapse_calibration_applied_crc()
    assert applied == lib.synapse_calibration_effective_crc(base)
    assert applied != lib.synapse_calibration_crc(base)  # full CRC is not the sentinel
    # Adding a serial later must not look like a pending reboot.
    with_serial = _copy_record(base)
    _put_text(with_serial, SERIAL_OFFSET, b"SYN-A1B2C3D4E5F7")
    lib.synapse_calibration_seal(with_serial)
    assert lib.synapse_calibration_applied_crc() == lib.synapse_calibration_effective_crc(
        with_serial
    )


def test_effective_crc_host_c_rules(lib: ctypes.CDLL) -> None:
    """Compiled C checks, run alongside the ctypes record tests in one library."""
    assert lib.calibration_effective_host_run_all() == 0
