"""MVP-2.0 W2: the sensors on real hardware.

What this proves, and what it cannot:

* ``synapse_i2c.c`` (the one owner of the shared I2C bus), ``synapse_config_limits.h`` and
  ``ecg_stuck.h`` are compiled and run here against fakes: the logic is real, the bus is not.
* The drivers' own bus handling (acquire in init, release on a failed init and in deinit) is driven
  for the INA219 in ``tests/test_ina219_host.py``; the PPG, IMU and TMP117 only build under ESP-IDF,
  so for them the sources are guarded statically below. That proves the fix is in the source, not
  that the band behaves: only the ESP-IDF build of ``band-core`` and a board can.
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

ESP_ERR_INVALID_STATE = 0x103
ESP_ERR_INVALID_ARG = 0x102


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _load(
    tmp_path_factory: pytest.TempPathFactory, name: str, sources: list[Path], includes: list[str]
):
    out = tmp_path_factory.mktemp(name)
    lib = try_load(build(out, sources, extra_includes=includes))
    if lib is None:
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    return lib


def _kconfig_default(symbol: str) -> int:
    text = _text(BAND / "config" / "Kconfig")
    block = re.search(rf"^config {symbol}\b(.*?)(?=^config |^endmenu|\Z)", text, re.S | re.M)
    assert block, f"{symbol} is not declared in the Kconfig"
    default = re.search(r"^\s*default\s+(\S+)", block.group(1), re.M)
    assert default, f"{symbol} has no default"
    return int(default.group(1), 0)


# ---------------------------------------------------------------------------------------------
# synapse_i2c.c: the owner of the shared bus
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _i2c_lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = _load(
        tmp_path_factory,
        "i2c",
        [GLUE / "host_glue_i2c.c", COMMON / "sensors" / "synapse_i2c.c"],
        [],
    )
    lib.h_acquire.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    lib.h_release.argtypes = [ctypes.c_int]
    lib.h_write.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
    lib.h_read_reg.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint8,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.h_fail_next_bus.argtypes = [ctypes.c_int]
    lib.h_last_write_byte.argtypes = [ctypes.c_int]
    lib.h_default_freq.restype = ctypes.c_uint
    lib.h_last_dev_freq.restype = ctypes.c_uint
    return lib


@pytest.fixture
def bus(_i2c_lib: ctypes.CDLL) -> ctypes.CDLL:
    _i2c_lib.h_reset()  # releases whatever the previous test left, then zeroes the counters
    return _i2c_lib


def test_any_number_of_drivers_share_one_bus(bus: ctypes.CDLL) -> None:
    for _ in range(4):  # PPG, IMU, TMP117, INA219
        assert bus.h_acquire(0, 8, 9, 400000) == 0
    assert bus.h_buses_created() == 1
    assert bus.h_buses_deleted() == 0


def test_the_bus_survives_until_the_last_driver_leaves(bus: ctypes.CDLL) -> None:
    """The bug: stopping the PPG deleted the driver the IMU, TMP117 and INA219 were still using."""
    for _ in range(3):
        assert bus.h_acquire(0, 8, 9, 400000) == 0
    bus.h_release(0)
    bus.h_release(0)
    assert bus.h_buses_deleted() == 0, "two of three drivers left: the bus must still be there"
    assert bus.h_write(0, 0x48, b"\x01\x02", 2) == 0, "and still usable by the third"
    bus.h_release(0)
    assert bus.h_buses_deleted() == 1
    assert bus.h_write(0, 0x48, b"\x01\x02", 2) == ESP_ERR_INVALID_STATE, "no bus, no transfer"


def test_two_wirings_on_one_port_are_refused_not_ignored(bus: ctypes.CDLL) -> None:
    assert bus.h_acquire(0, 8, 9, 400000) == 0
    assert bus.h_acquire(0, 21, 9, 400000) == ESP_ERR_INVALID_STATE, "other SDA"
    assert bus.h_acquire(0, 8, 9, 100000) == ESP_ERR_INVALID_STATE, "other clock"
    assert bus.h_buses_created() == 1
    bus.h_release(0)  # the refused calls took no reference: one release is enough
    assert bus.h_buses_deleted() == 1


def test_negative_pins_and_zero_clock_mean_the_defaults(bus: ctypes.CDLL) -> None:
    assert bus.h_acquire(0, -1, -1, 0) == 0
    assert (bus.h_last_sda(), bus.h_last_scl()) == (bus.h_default_sda(), bus.h_default_scl())
    assert (bus.h_default_sda(), bus.h_default_scl()) == (8, 9)
    assert bus.h_write(0, 0x57, b"\x09\x40", 2) == 0
    assert bus.h_last_dev_freq() == bus.h_default_freq() == 400000
    # the same defaults spelled out by a second driver are the same wiring, not a conflict
    assert bus.h_acquire(0, 8, 9, 400000) == 0


def test_a_device_handle_is_created_once_per_address(bus: ctypes.CDLL) -> None:
    assert bus.h_acquire(0, 8, 9, 400000) == 0
    for _ in range(5):
        assert bus.h_write(0, 0x57, b"\x09\x40", 2) == 0
    assert bus.h_write(0, 0x68, b"\x7f\x00", 2) == 0
    assert bus.h_devs_added() == 2
    bus.h_release(0)
    assert bus.h_devs_removed() == 2, "the handles go with the bus"


def test_register_read_and_write_reach_the_device(bus: ctypes.CDLL) -> None:
    assert bus.h_acquire(0, 8, 9, 400000) == 0
    assert bus.h_write(0, 0x57, b"\x09\x03", 2) == 0
    assert (bus.h_last_write_len(), bus.h_last_write_byte(0), bus.h_last_write_byte(1)) == (
        2,
        0x09,
        0x03,
    )
    out = ctypes.create_string_buffer(3)
    assert bus.h_read_reg(0, 0x57, 0xFF, out, 3) == 0
    assert bus.h_last_write_byte(0) == 0xFF, "the register address goes out before the read"
    assert out.raw == b"\xa5\xa5\xa5"


def test_a_bus_that_cannot_be_created_leaves_nothing_held(bus: ctypes.CDLL) -> None:
    bus.h_fail_next_bus(ESP_ERR_INVALID_STATE)
    assert bus.h_acquire(0, 8, 9, 400000) == ESP_ERR_INVALID_STATE
    bus.h_release(0)  # a driver that cleans up after a failed init must be harmless
    assert bus.h_buses_deleted() == 0
    assert bus.h_acquire(0, 8, 9, 400000) == 0, "and the next driver gets a bus"
    assert bus.h_buses_created() == 1


def test_the_two_ports_are_independent(bus: ctypes.CDLL) -> None:
    assert bus.h_acquire(0, 8, 9, 400000) == 0
    assert bus.h_acquire(1, 10, 11, 100000) == 0
    bus.h_release(0)
    assert bus.h_buses_deleted() == 1
    assert bus.h_write(1, 0x40, b"\x05\x00\x00", 3) == 0, "port 1 is untouched by port 0 leaving"


def test_two_ports_on_the_same_pins_are_refused(bus: ctypes.CDLL) -> None:
    """A sensor whose port setting differs from the others' would put a second controller on SDA/SCL."""
    assert bus.h_acquire(0, 8, 9, 400000) == 0
    assert bus.h_acquire(1, 8, 9, 400000) == ESP_ERR_INVALID_STATE, "same pins"
    assert bus.h_acquire(1, 9, 10, 400000) == ESP_ERR_INVALID_STATE, "SCL of port 0 used as SDA"
    assert bus.h_buses_created() == 1
    assert bus.h_acquire(1, 10, 11, 400000) == 0, "other pins are another bus"
    bus.h_release(0)
    bus.h_release(1)
    assert bus.h_acquire(1, 8, 9, 400000) == 0, "and the pins are free again when port 0 is gone"


def test_a_transfer_holds_the_lock_so_release_cannot_free_its_handle() -> None:
    code = _text(COMMON / "sensors" / "synapse_i2c.c")
    for func, transfer in (
        ("synapse_i2c_write(", "i2c_master_transmit("),
        ("synapse_i2c_write_read(", "i2c_master_transmit_receive("),
    ):
        body = code[code.index(f"esp_err_t {func}") :]
        body = body[: body.index("\n}\n")]
        assert body.index("xSemaphoreTake") < body.index(transfer) < body.index("xSemaphoreGive"), (
            func
        )


def test_a_port_that_does_not_exist_is_an_argument_error(bus: ctypes.CDLL) -> None:
    assert bus.h_acquire(2, 8, 9, 400000) == ESP_ERR_INVALID_ARG
    assert bus.h_acquire(-1, 8, 9, 400000) == ESP_ERR_INVALID_ARG
    bus.h_release(5)  # out of range and not held: harmless
    assert bus.h_write(0, 0x57, b"", 0) == ESP_ERR_INVALID_ARG


# ---------------------------------------------------------------------------------------------
# synapse_config_limits.h
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def limits(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = _load(
        tmp_path_factory,
        "limits",
        [GLUE / "host_glue_config_limits.c"],
        [str(BAND / "config")],
    )
    lib.host_gpio_ok.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.host_i2c_addr_ok.argtypes = [ctypes.c_int]
    lib.host_i2c_bus_ok.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
    lib.host_ppg_codes_ok.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    lib.host_imu_ok.argtypes = [ctypes.c_int] * 4
    return lib


def test_gpio_rule_is_the_n8r8_module(limits: ctypes.CDLL) -> None:
    for pin in (0, 1, 15, 21, 38, 48):
        assert limits.host_gpio_ok(pin, 0) == 1, pin
    for pin in (19, 20, 43, 44):
        assert limits.host_gpio_ok(pin, 1) == 0, f"GPIO {pin} is the USB or the console"
    for pin in (22, 23, 24, 25, 26, 32, 33, 37, 49, -2, 100):
        assert limits.host_gpio_ok(pin, 1) == 0, f"GPIO {pin} does not work on the N8R8"
    assert limits.host_gpio_ok(-1, 1) == 1, "-1 is 'not wired' for an optional pin"
    assert limits.host_gpio_ok(-1, 0) == 0, "and an error for a mandatory one"


def test_i2c_addresses_outside_the_standard_range_are_rejected(limits: ctypes.CDLL) -> None:
    assert [limits.host_i2c_addr_ok(a) for a in (0x07, 0x08, 0x40, 0x77, 0x78, -1)] == [
        0,
        1,
        1,
        1,
        0,
        0,
    ]


def test_the_bus_needs_two_different_usable_pins_and_a_sane_clock(limits: ctypes.CDLL) -> None:
    assert limits.host_i2c_bus_ok(8, 9, 400000) == 1
    assert limits.host_i2c_bus_ok(9, 9, 400000) == 0, "SDA and SCL cannot be the same pin"
    assert limits.host_i2c_bus_ok(21, 22, 400000) == 0, "the classic ESP32 pair does not exist here"
    assert limits.host_i2c_bus_ok(8, 25, 400000) == 0
    assert limits.host_i2c_bus_ok(-1, 9, 400000) == 0, "the bus pins are not optional"
    assert (limits.host_i2c_bus_ok(8, 9, 9999), limits.host_i2c_bus_ok(8, 9, 1000001)) == (0, 0)
    assert (limits.host_i2c_bus_ok(8, 9, 10000), limits.host_i2c_bus_ok(8, 9, 1000000)) == (1, 1)


def test_ppg_codes_are_checked_not_masked(limits: ctypes.CDLL) -> None:
    assert limits.host_ppg_codes_ok(0, 3, 3) == 1
    assert limits.host_ppg_codes_ok(7, 0, 0) == 1
    assert limits.host_ppg_codes_ok(8, 0, 0) == 0, "sample-rate code 8 would have become 0 (50 Hz)"
    assert limits.host_ppg_codes_ok(0, 4, 0) == 0
    assert limits.host_ppg_codes_ok(0, 0, 4) == 0


def test_imu_ranges_and_rates(limits: ctypes.CDLL) -> None:
    assert limits.host_imu_ok(8, 500, 50, 50) == 1
    assert limits.host_imu_ok(3, 500, 50, 50) == 0, "3 g used to fall back to +-2 g without a word"
    assert limits.host_imu_ok(8, 300, 50, 50) == 0
    assert limits.host_imu_ok(8, 500, 0, 50) == 0, "a zero rate would divide by zero in the divider"
    assert limits.host_imu_ok(8, 500, 1126, 50) == 0
    assert limits.host_imu_ok(8, 500, 50, 1101) == 0


def test_the_factory_configuration_passes_its_own_limits(limits: ctypes.CDLL) -> None:
    """A Kconfig default the validator rejects would boot every band with a dead sensor."""
    k = _kconfig_default
    assert limits.host_gpio_ok(k("SYNAPSE_ECG_GPIO_LEAD_OFF"), 1) == 1
    assert limits.host_gpio_ok(k("SYNAPSE_PPG_GPIO_INT"), 1) == 1
    assert limits.host_gpio_ok(k("SYNAPSE_IMU_GPIO_INT"), 1) == 1
    assert limits.host_gpio_ok(k("SYNAPSE_TEMP_GPIO_ALERT"), 1) == 1
    assert (
        limits.host_i2c_bus_ok(
            k("SYNAPSE_I2C_SDA_GPIO"), k("SYNAPSE_I2C_SCL_GPIO"), k("SYNAPSE_I2C_FREQ_HZ")
        )
        == 1
    )
    for addr in (
        "SYNAPSE_PPG_I2C_ADDR",
        "SYNAPSE_IMU_I2C_ADDR",
        "SYNAPSE_TEMP_I2C_ADDR",
        "SYNAPSE_INA219_I2C_ADDR",
    ):
        assert limits.host_i2c_addr_ok(k(addr)) == 1, addr
    assert (
        limits.host_ppg_codes_ok(
            k("SYNAPSE_PPG_SAMPLE_RATE"), k("SYNAPSE_PPG_PULSE_WIDTH"), k("SYNAPSE_PPG_ADC_RANGE")
        )
        == 1
    )
    assert (
        limits.host_imu_ok(
            k("SYNAPSE_IMU_ACCEL_FSR_G"),
            k("SYNAPSE_IMU_GYRO_FSR_DPS"),
            k("SYNAPSE_IMU_ACCEL_ODR_HZ"),
            k("SYNAPSE_IMU_GYRO_ODR_HZ"),
        )
        == 1
    )


# ---------------------------------------------------------------------------------------------
# ecg_stuck.h
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stuck(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = _load(
        tmp_path_factory, "stuck", [GLUE / "host_glue_ecg_stuck.c"], [str(COMMON / "sensors")]
    )
    lib.host_stuck_feed.argtypes = [ctypes.c_int]
    lib.host_stuck_min_samples.restype = ctypes.c_uint32
    return lib


def _feed(lib: ctypes.CDLL, raws: list[int]) -> list[int]:
    return [lib.host_stuck_feed(r) for r in raws]


def _live(n: int) -> list[int]:
    """A signal that moves every sample, away from both rails."""
    return [2000 + (7 if i % 2 else -5) + (i % 13) for i in range(n)]


def test_a_live_signal_is_never_flagged(stuck: ctypes.CDLL) -> None:
    stuck.host_stuck_reset()
    assert not any(_feed(stuck, _live(20000)))


def test_a_constant_input_is_flagged_after_two_seconds(stuck: ctypes.CDLL) -> None:
    stuck.host_stuck_reset()
    n = stuck.host_stuck_min_samples()
    hits = _feed(stuck, [2048] * (n + 50))
    # the first sample has nothing to be equal to; then N equal ones in a row make the verdict
    assert hits.index(1) == n
    assert all(hits[n:]), "and it stays flagged while the input stays dead"


def test_an_input_on_a_rail_is_flagged_even_if_it_wiggles(stuck: ctypes.CDLL) -> None:
    low, high = stuck.host_stuck_low(), stuck.host_stuck_high()
    n = stuck.host_stuck_min_samples()
    for rail in (
        [0, 1, 2, 3] * n,
        [4095, 4090, 4085, 4080] * n,
        [low, low - 3] * n,
        [high, high + 3] * n,
    ):
        stuck.host_stuck_reset()
        assert any(_feed(stuck, rail[: n + 10]))


def test_the_rail_thresholds_are_inclusive_and_one_code_inside_is_a_signal(
    stuck: ctypes.CDLL,
) -> None:
    low, high = stuck.host_stuck_low(), stuck.host_stuck_high()
    n = stuck.host_stuck_min_samples()
    stuck.host_stuck_reset()
    assert any(_feed(stuck, [low, low - 1] * (n // 2 + 5)))
    stuck.host_stuck_reset()
    assert any(_feed(stuck, [high, high + 1] * (n // 2 + 5)))
    stuck.host_stuck_reset()
    inside = [low + 1 + (i % 2) for i in range(2 * n)]  # moving, one code above the bottom rail
    assert not any(_feed(stuck, inside))


def test_a_short_dead_spell_does_not_flag_and_recovery_clears_it(stuck: ctypes.CDLL) -> None:
    n = stuck.host_stuck_min_samples()
    stuck.host_stuck_reset()
    assert not any(_feed(stuck, [0] * (n - 1) + _live(5) + [0] * (n - 1)))  # one live run between
    stuck.host_stuck_reset()
    assert any(_feed(stuck, [2048] * (n + 5)))
    assert _feed(stuck, _live(1))[-1] == 0, "one sample that moves clears the verdict"


# ---------------------------------------------------------------------------------------------
# The drivers that only build under ESP-IDF: guarded in the source
# ---------------------------------------------------------------------------------------------

DRIVERS = {
    "ppg": COMMON / "sensors" / "ppg_max30102.cpp",
    "imu": COMMON / "sensors" / "imu_icm20948.cpp",
    "tmp117": BAND / "sensors" / "temp_tmp117.c",
    "ina219": BAND / "sensors" / "ina219.c",
}
LEGACY_I2C = re.compile(
    r'#include\s+"driver/i2c\.h"|\bi2c_driver_install\b|\bi2c_driver_delete\b|\bi2c_param_config\b'
    r"|\bi2c_master_cmd_begin\b|\bi2c_cmd_link_create\b|\bi2c_master_write_to_device\b"
    r"|\bi2c_master_write_read_device\b"
)


def _sources_of_the_firmware() -> list[Path]:
    files = []
    for folder in (COMMON / "sensors", BAND / "sensors", BAND / "power", BAND / "config"):
        files += [p for p in folder.iterdir() if p.suffix in {".c", ".cpp", ".h"}]
    return files


def test_no_driver_touches_the_legacy_i2c_api() -> None:
    """Two I2C driver generations cannot live in one image; every sensor goes through synapse_i2c."""
    offenders = [p.name for p in _sources_of_the_firmware() if LEGACY_I2C.search(_text(p))]
    assert not offenders, f"legacy I2C API still used in: {offenders}"
    assert (COMMON / "sensors" / "synapse_i2c.c").is_file()
    assert "synapse_i2c.c" in _text(BAND / "sensors" / "CMakeLists.txt"), "and it must be built"


@pytest.mark.parametrize("name", sorted(DRIVERS))
def test_each_driver_takes_the_bus_and_gives_it_back_on_every_exit(name: str) -> None:
    code = _text(DRIVERS[name])
    assert "synapse_i2c_acquire(" in code, f"{name} must take a reference on the shared bus"
    # one release for the deinit and one for a failed init (the second one is what the old code lacked)
    assert code.count("synapse_i2c_release(") >= 2, (
        f"{name} leaks the bus reference on a failed init"
    )


def test_the_pins_are_configuration_not_literals() -> None:
    sensors = _text(BAND / "sensors" / "synapse_sensors.c")
    assert not re.search(r"(sda|scl)_gpio_num\s*=\s*\d", sensors)
    assert "hw_cfg->i2c_sda_gpio" in sensors
    assert "hw_cfg->i2c_scl_gpio" in sensors
    assert "hw_cfg.i2c_sda_gpio" in _text(BAND / "power" / "synapse_power.c"), (
        "the INA219 uses the same bus"
    )


def test_a_sensor_that_fails_does_not_reboot_the_band() -> None:
    for path in (BAND / "sensors" / "synapse_sensors.c", BAND / "power" / "synapse_power.c"):
        calls = re.findall(r"^\s*ESP_ERROR_CHECK\(", _text(path), re.M)
        assert not calls, f"{path.name} still aborts the band on an error"


def test_the_settings_nobody_used_are_gone() -> None:
    gone = re.compile(
        r"ecg_vref_mv|SYNAPSE_ECG_VREF_MV|vref_mv|led_current_green|LED_CURRENT_GREEN", re.I
    )
    for path in (
        ROOT / "config" / "hardware.yaml",
        BAND / "config" / "Kconfig",
        BAND / "config" / "synapse_config.c",
        BAND / "config" / "synapse_config.h",
        BAND / "sensors" / "synapse_sensors.c",
        COMMON / "sensors" / "ecg_ad8232.h",
        COMMON / "sensors" / "ppg_max30102.h",
    ):
        text = re.sub(r"(//[^\n]*|#[^\n]*)", "", _text(path))  # comments may say why they went
        assert not gone.search(text), f"{path.name} still carries a setting that does nothing"


def test_the_sensors_cannot_be_split_over_two_i2c_ports() -> None:
    assert "must share one I2C port" in _text(BAND / "config" / "synapse_config.c")


def test_the_stored_configuration_layout_changed_so_its_version_did() -> None:
    header = _text(BAND / "config" / "synapse_config.h")
    assert re.search(r"#define SYNAPSE_CONFIG_VERSION\s+2\b", header)
    assert "i2c_sda_gpio" in header
    assert "i2c_freq_hz" in header


def test_the_ecg_driver_reports_a_dead_input() -> None:
    code = _text(COMMON / "sensors" / "ecg_ad8232.cpp")
    assert "ecg_stuck_update(" in code
    assert "ecg_ad8232_is_signal_stuck" in code
    assert "ecg_stuck_update(" not in _text(BAND / "ble" / "ble_research_service.c"), (
        "reported, never gating"
    )
