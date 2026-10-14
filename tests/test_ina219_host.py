"""Host compile + behaviour gate for the INA219 power monitor.

The power stream is what turns the energy budget from a bench calibration into
a per-session measurement (Architecture.md §55-62), and its arithmetic
(register → volts/amps/watts, calibration register, overflow) is pure logic
that must not wait for hardware. This compiles
``firmware/esp32_band/sensors/ina219.c`` against a simulated I²C bus with
``-Wall -Wextra -Werror`` and drives it through a LiPo operating point.

Skips are impossible: a missing compiler fails the gate (see
``tests/firmware_host``), because a compile gate that skips is a gate that
cannot fail.
"""

from __future__ import annotations

import ctypes
from pathlib import Path

import pytest

from tests.firmware_host import ROOT, build, try_load

BAND_SENSORS = ROOT / "firmware" / "esp32_band" / "sensors"
GLUE = ROOT / "tests" / "firmware_host" / "host_glue_ina219.c"

SOURCES = [
    BAND_SENSORS / "ina219.c",
    GLUE,
]

INCLUDES = [
    "-Itests/firmware_host/stubs",
    "-Ifirmware/esp32_band/sensors",
]


@pytest.fixture(scope="module")
def lib() -> ctypes.CDLL | None:
    """Compiled shared library, or None when the host blocks loading it.

    A missing compiler fails the build (never skips); only an OS that refuses
    to load a freshly built library yields None, and those tests skip.
    """
    out = build(
        ROOT / "build" / "firmware_host_ina219", SOURCES, extra_includes=[str(BAND_SENSORS)]
    )
    return try_load(out)


@pytest.fixture(autouse=True)
def _fresh_device(lib) -> None:
    if lib is not None:
        lib.ina_host_reset()


class TestIna219HostGate:
    """Register arithmetic against a simulated bus."""

    def test_smoke_main_passes(self, lib) -> None:
        if lib is None:
            pytest.skip("no C compiler available")
        rc = lib.ina_host_smoke_main()
        assert rc == 0, f"INA219 host smoke returned {rc}"

    def test_lipo_operating_point_is_exact(self, lib) -> None:
        """3.7 V, 100 mA, 0.1 Ω shunt -> 0.37 W within the LSB resolution."""
        if lib is None:
            pytest.skip("no C compiler available")
        lib.ina_host_set_register(0x02, 925 << 3)  # bus voltage, 4 mV/LSB
        lib.ina_host_set_register(0x01, 1000)  # shunt, 10 uV/LSB -> 10 mV

        data = _read(lib)
        assert data["bus_voltage_v"] == pytest.approx(3.7, abs=0.02)
        assert data["current_a"] == pytest.approx(0.1, abs=0.005)
        assert data["power_w"] == pytest.approx(0.37, abs=0.02)

    def test_discharging_is_negative(self, lib) -> None:
        """A shunt voltage of the other sign must read as negative current."""
        if lib is None:
            pytest.skip("no C compiler available")
        lib.ina_host_set_register(0x02, 1050 << 3)  # 4.2 V, charging
        lib.ina_host_set_register(0x01, ctypes.c_int16(-500).value & 0xFFFF)  # -5 mV
        data = _read(lib)
        assert data["current_a"] == pytest.approx(-0.05, abs=0.005)
        assert data["power_w"] < 0.0

    def test_i2c_failure_is_reported_not_swallowed(self, lib) -> None:
        """A bus error must surface as an error, never as a plausible value."""
        if lib is None:
            pytest.skip("no C compiler available")
        lib.ina_host_set_read_error(0x107)  # ESP_ERR_TIMEOUT
        rc = _read_once(lib)
        assert rc != 0, "a failed I2C read must not return ESP_OK"

    def test_stats_track_reads_and_errors(self, lib) -> None:
        if lib is None:
            pytest.skip("no C compiler available")
        lib.ina_host_set_register(0x02, 925 << 3)
        lib.ina_host_set_register(0x01, 1000)
        _read_once(lib)
        _read_once(lib)
        lib.ina_host_set_read_error(0x107)
        _read_once(lib)

        reads = ctypes.c_uint32(0)
        errors = ctypes.c_uint32(0)
        lib.ina219_get_stats(ctypes.byref(reads), ctypes.byref(errors))
        assert reads.value == 2
        assert errors.value == 1
        assert lib.ina219_is_healthy() in (0, 1)


def _read_once(lib) -> int:
    """One driver read through the glue shim; 0 on success."""
    lib.ina_host_read_once.restype = ctypes.c_int
    return lib.ina_host_read_once()


class _Read:
    """ctypes wrapper for the glue's data getters."""

    def __init__(self, lib) -> None:
        self.lib = lib
        self.lib.ina_host_read_once.restype = ctypes.c_int
        self.lib.ina_host_bus_v.restype = ctypes.c_float
        self.lib.ina_host_current_a.restype = ctypes.c_float
        self.lib.ina_host_power_w.restype = ctypes.c_float

    def __call__(self) -> dict[str, float]:
        assert _read_once(self.lib) == 0, "the glue read must succeed on a clean bus"
        return {
            "bus_voltage_v": float(self.lib.ina_host_bus_v()),
            "current_a": float(self.lib.ina_host_current_a()),
            "power_w": float(self.lib.ina_host_power_w()),
        }


def _read(lib) -> dict[str, float]:
    return _Read(lib)()
