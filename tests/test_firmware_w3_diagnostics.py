"""MVP-2.0 W3: bring-up diagnostics and the factory test.

What is proven here and what is not:

* ``synapse_selftest.h`` (the verdicts and the report), ``synapse_diag.c`` (I2C scan and safe
  register dump) and the bus probe of ``synapse_i2c.c`` are compiled and run against fakes.
* The INA219 self-test runs in ``tests/test_ina219_host.py`` against its register fake.
* The PPG, IMU and TMP117 self-tests and ``synapse_factory.c`` build only under ESP-IDF: they are
  guarded statically below (and compiled by the ``band-core`` / ``band-full`` builds). That shows
  the code is wired, not that a real part passes: only a board can.
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

ESP_ERR_INVALID_ARG = 0x102
ESP_ERR_INVALID_STATE = 0x103
ESP_ERR_NOT_FOUND = 0x105
ESP_ERR_TIMEOUT = 0x107


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _load(tmp_path_factory: pytest.TempPathFactory, name: str, sources: list[Path]) -> ctypes.CDLL:
    out = tmp_path_factory.mktemp(name)
    lib = try_load(build(out, sources, extra_includes=[]))
    if lib is None:
        pytest.skip("OS policy blocked loading the freshly built library; the compile succeeded")
    return lib


# ---------------------------------------------------------------------------------------------
# synapse_selftest.h
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def st(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = _load(tmp_path_factory, "selftest", [GLUE / "host_glue_selftest.c"])
    lib.h_temp_ok.argtypes = [ctypes.c_float]
    lib.h_max30102_temp.argtypes = [ctypes.c_int, ctypes.c_uint]
    lib.h_max30102_temp.restype = ctypes.c_float
    lib.h_fifo_advanced.argtypes = [ctypes.c_uint, ctypes.c_uint]
    lib.h_bus_v_ok.argtypes = [ctypes.c_float]
    lib.h_imu_rest.argtypes = [ctypes.c_float] * 6
    lib.h_mag_ok.argtypes = [ctypes.c_float] * 3
    lib.h_count_ok.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
    lib.h_report_add.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p]
    lib.h_report_add_n.argtypes = [ctypes.c_int]
    lib.h_report_detail.argtypes = [ctypes.c_int]
    lib.h_report_detail.restype = ctypes.c_char_p
    lib.h_report_name.argtypes = [ctypes.c_int]
    lib.h_report_name.restype = ctypes.c_char_p
    return lib


def test_temperature_bounds(st: ctypes.CDLL) -> None:
    assert [st.h_temp_ok(t) for t in (-20.0, 25.0, 85.0)] == [1, 1, 1]
    assert [st.h_temp_ok(t) for t in (-20.5, 85.5, 150.0, float("nan"))] == [0, 0, 0, 0]


def test_max30102_die_temperature_is_integer_plus_sixteenths(st: ctypes.CDLL) -> None:
    assert st.h_max30102_temp(25, 8) == pytest.approx(25.5)
    assert st.h_max30102_temp(-3, 4) == pytest.approx(-2.75)
    assert st.h_max30102_temp(30, 0xF5) == pytest.approx(30.3125), (
        "only the low nibble is the fraction"
    )


def test_fifo_pointer_must_move_and_may_wrap(st: ctypes.CDLL) -> None:
    assert st.h_fifo_advanced(5, 5) == 0, "a stuck pointer means the part is not sampling"
    assert st.h_fifo_advanced(5, 10) == 1
    assert st.h_fifo_advanced(30, 3) == 1, "the FIFO has 32 slots and wraps"


def test_bus_voltage_of_a_lipo_rail(st: ctypes.CDLL) -> None:
    assert [st.h_bus_v_ok(v) for v in (3.7, 4.2, 5.0)] == [1, 1, 1]
    assert [st.h_bus_v_ok(v) for v in (0.0, 0.5, 41.0)] == [0, 0, 0]


def test_imu_at_rest(st: ctypes.CDLL) -> None:
    assert st.h_imu_rest(0, 0, 1.0, 0, 0, 0) == 1
    assert st.h_imu_rest(0.6, 0.0, 0.8, 0.01, -0.02, 0.0) == 1, "tilted is still 1 g"
    assert st.h_imu_rest(0, 0, 1.3, 0, 0, 0) == 0, "1.3 g: moving, or the scale is wrong"
    assert st.h_imu_rest(0, 0, 0.0, 0, 0, 0) == 0, "0 g: free fall or a dead accelerometer"
    assert st.h_imu_rest(0, 0, 1.0, 0.3, 0, 0) == 0, "rotating"


def test_magnetometer_reads_an_earth_sized_field(st: ctypes.CDLL) -> None:
    assert st.h_mag_ok(20, 0, 30) == 1
    assert st.h_mag_ok(0, 0, 0) == 0, "zeros are what the driver holds while the AK09916 is silent"
    assert st.h_mag_ok(200, 0, 0) == 0


def test_a_stream_must_deliver_80_percent_of_its_rate(st: ctypes.CDLL) -> None:
    assert st.h_count_ok(400, 50, 10) == 1
    assert st.h_count_ok(399, 50, 10) == 0
    assert st.h_count_ok(0, 50, 10) == 0
    assert st.h_count_ok(4_000_000_000, 500, 10) == 1, "no overflow on a big count"


def test_an_empty_report_proves_nothing(st: ctypes.CDLL) -> None:
    st.h_report_init()
    assert st.h_report_pass() == 0


def test_the_report_passes_only_if_every_check_passes(st: ctypes.CDLL) -> None:
    st.h_report_init()
    st.h_report_add(b"a", 1, b"fine")
    st.h_report_add(b"b", 1, b"fine")
    assert (st.h_report_pass(), st.h_report_failed()) == (1, 0)
    st.h_report_add(b"c", 0, b"broken")
    assert (st.h_report_pass(), st.h_report_failed()) == (0, 1)
    assert st.h_report_detail(2) == b"broken"
    assert st.h_report_name(2) == b"c"


def test_a_report_that_lost_lines_is_never_a_pass(st: ctypes.CDLL) -> None:
    st.h_report_init()
    st.h_report_add_n(st.h_max_items() + 5)
    assert st.h_report_n() == st.h_max_items()
    assert st.h_report_dropped() == 5
    assert st.h_report_failed() == 5
    assert st.h_report_pass() == 0, "every shown check passed, but five were not shown"


def test_a_long_detail_is_cut_not_overflowed(st: ctypes.CDLL) -> None:
    st.h_report_init()
    st.h_report_add(b"x", 1, b"y" * 500)
    assert len(st.h_report_detail(0)) == st.h_detail_len() - 1


# ---------------------------------------------------------------------------------------------
# synapse_diag.c: scan and safe dump
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _diag_lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = _load(
        tmp_path_factory, "diag", [GLUE / "host_glue_diag.c", COMMON / "sensors" / "synapse_diag.c"]
    )
    lib.h_set_present.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.h_set_reg.argtypes = [ctypes.c_int] * 3
    lib.h_set_bus_error.argtypes = [ctypes.c_int]
    lib.h_set_read_error_addr.argtypes = [ctypes.c_int]
    lib.h_was_read.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.h_scan.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.h_name.argtypes = [ctypes.c_int]
    lib.h_name.restype = ctypes.c_char_p
    lib.h_dump_text.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    return lib


@pytest.fixture
def diag(_diag_lib: ctypes.CDLL) -> ctypes.CDLL:
    _diag_lib.h_reset()
    return _diag_lib


def _scan(lib: ctypes.CDLL, max_found: int = 16) -> tuple[int, list[int]]:
    buf = ctypes.create_string_buffer(max_found)
    n = lib.h_scan(buf, max_found)
    return n, list(buf.raw[: max(0, min(n, max_found))])


def _dump(lib: ctypes.CDLL, addr: int) -> list[tuple[int, int, int]]:
    regs = (ctypes.c_uint8 * 16)()
    values = (ctypes.c_uint16 * 16)()
    widths = (ctypes.c_uint8 * 16)()
    errs = (ctypes.c_int * 16)()
    n = lib.h_dump(addr, regs, values, widths, errs, 16)
    return [(regs[i], values[i], widths[i]) for i in range(max(n, 0))]


def _regs_read(lib: ctypes.CDLL, addr: int) -> set[int]:
    return {r for r in range(256) if lib.h_was_read(addr, r)}


def test_an_empty_bus_scans_to_nothing(diag: ctypes.CDLL) -> None:
    assert _scan(diag) == (0, [])


def test_the_scan_lists_who_answers_in_address_order(diag: ctypes.CDLL) -> None:
    for addr in (0x68, 0x57, 0x0C):
        diag.h_set_present(addr, 1)
    assert _scan(diag) == (3, [0x0C, 0x57, 0x68])


def test_reserved_addresses_are_never_probed(diag: ctypes.CDLL) -> None:
    assert (diag.h_first(), diag.h_last()) == (0x08, 0x77)
    diag.h_set_present(0x03, 1)
    diag.h_set_present(0x78, 1)
    assert _scan(diag) == (0, []), "0x00-0x07 and 0x78-0x7F belong to the I2C standard"


def test_a_failing_bus_is_an_error_not_an_empty_bus(diag: ctypes.CDLL) -> None:
    """'Nobody answers' and 'the bus is broken' must not look alike, or a dead bus reads as absent parts."""
    diag.h_set_bus_error(ESP_ERR_TIMEOUT)
    n, _ = _scan(diag)
    assert n == -ESP_ERR_TIMEOUT


def test_a_crowded_bus_reports_the_real_count(diag: ctypes.CDLL) -> None:
    for addr in range(0x10, 0x10 + 20):
        diag.h_set_present(addr, 1)
    n, listed = _scan(diag, max_found=diag.h_max_found())
    assert n == 20, "the caller learns the list was cut"
    assert listed == list(range(0x10, 0x10 + diag.h_max_found()))


def test_the_known_parts_have_names(diag: ctypes.CDLL) -> None:
    names = {a: diag.h_name(a) for a in (0x57, 0x68, 0x69, 0x0C, 0x48, 0x40)}
    assert names == {
        0x57: b"MAX30102",
        0x68: b"ICM-20948",
        0x69: b"ICM-20948 (AD0=1)",
        0x0C: b"AK09916",
        0x48: b"TMP117",
        0x40: b"INA219",
    }
    assert diag.h_name(0x50) is None, "a stranger has no name"


def test_the_max30102_dump_never_touches_the_fifo_or_the_interrupt_status(
    diag: ctypes.CDLL,
) -> None:
    """Reading 0x07 pops a FIFO sample and reading 0x00/0x01 clears the interrupt flags."""
    diag.h_set_present(0x57, 1)
    diag.h_set_reg(0x57, 0xFF, 0x15)
    diag.h_set_reg(0x57, 0x09, 0x03)
    diag.h_set_reg(0x57, 0x0C, 0x1F)
    dump = _dump(diag, 0x57)
    assert len(dump) == 12
    assert {r: v for r, v, _ in dump}[0xFF] == 0x15
    assert {r: v for r, v, _ in dump}[0x09] == 0x03
    read = _regs_read(diag, 0x57)
    assert read == {0x02, 0x03, 0x04, 0x05, 0x06, 0x08, 0x09, 0x0A, 0x0C, 0x0D, 0xFE, 0xFF}
    assert not read & {0x00, 0x01, 0x07}


def test_the_magnetometer_dump_leaves_st2_alone(diag: ctypes.CDLL) -> None:
    """Reading ST2 releases the AK09916 data protection; only the sample path may do that."""
    diag.h_set_present(0x0C, 1)
    diag.h_set_reg(0x0C, 0x00, 0x48)
    diag.h_set_reg(0x0C, 0x01, 0x09)
    assert [(r, v) for r, v, _ in _dump(diag, 0x0C)][:2] == [(0x00, 0x48), (0x01, 0x09)]
    assert _regs_read(diag, 0x0C) == {0x00, 0x01, 0x31}


def test_the_imu_dump_reads_who_am_i_and_the_power_registers_only(diag: ctypes.CDLL) -> None:
    diag.h_set_present(0x68, 1)
    diag.h_set_reg(0x68, 0x00, 0xEA)
    dump = _dump(diag, 0x68)
    assert dump[0] == (0x00, 0xEA, 1)
    assert _regs_read(diag, 0x68) == {0x00, 0x03, 0x06, 0x07, 0x0F}


def test_sixteen_bit_parts_are_read_big_endian(diag: ctypes.CDLL) -> None:
    diag.h_set_present(0x48, 1)
    diag.h_set_reg(0x48, 0x0F, 0x01)
    diag.h_set_reg(0x48, 0x10, 0x17)  # the second byte of DEVICE_ID
    dump = _dump(diag, 0x48)
    assert (0x0F, 0x0117, 2) in dump


def test_a_part_that_is_not_in_the_table_dumps_nothing(diag: ctypes.CDLL) -> None:
    diag.h_set_present(0x50, 1)
    assert _dump(diag, 0x50) == []
    assert _regs_read(diag, 0x50) == set(), "and nothing is read from a stranger"


def test_a_part_that_stops_answering_is_a_negative_result(diag: ctypes.CDLL) -> None:
    diag.h_set_present(0x57, 1)
    diag.h_set_read_error_addr(0x57)
    regs = (ctypes.c_uint8 * 16)()
    values = (ctypes.c_uint16 * 16)()
    widths = (ctypes.c_uint8 * 16)()
    errs = (ctypes.c_int * 16)()
    assert diag.h_dump(0x57, regs, values, widths, errs, 16) == -ESP_ERR_TIMEOUT


def test_the_console_line_of_a_dump(diag: ctypes.CDLL) -> None:
    diag.h_set_present(0x48, 1)
    diag.h_set_reg(0x48, 0x0F, 0x01)
    diag.h_set_reg(0x48, 0x10, 0x17)
    buf = ctypes.create_string_buffer(96)
    n = diag.h_dump_text(0x48, buf, 96)
    assert buf.value == b"0F=0117 02=0000 03=0000"
    assert n == len(buf.value)


def test_the_temperature_sensor_dump_does_not_clear_its_data_ready_flag(diag: ctypes.CDLL) -> None:
    """Reading CONFIGURATION (0x01) or the result (0x00) of a TMP117 clears Data_Ready."""
    diag.h_set_present(0x48, 1)
    _dump(diag, 0x48)
    read = _regs_read(diag, 0x48)
    assert not read & {0x00, 0x01}
    assert read == {0x0F, 0x10, 0x02, 0x03, 0x04}


def test_esp_fail_is_not_mistaken_for_a_device_count(diag: ctypes.CDLL) -> None:
    """ESP_FAIL is -1: negated it would be +1, 'one device found' with an empty list."""
    diag.h_set_bus_error(-1)
    n, _ = _scan(diag)
    assert n < 0
    assert n == -0x108, "reported as ESP_ERR_INVALID_RESPONSE"


def test_a_short_buffer_cuts_the_line_cleanly(diag: ctypes.CDLL) -> None:
    diag.h_set_present(0x57, 1)
    buf = ctypes.create_string_buffer(10)
    diag.h_dump_text(0x57, buf, 10)
    assert len(buf.value) <= 9
    assert not buf.value.endswith(b" ")


# ---------------------------------------------------------------------------------------------
# synapse_i2c_probe
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _bus_lib(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = _load(
        tmp_path_factory,
        "probe",
        [GLUE / "host_glue_i2c.c", COMMON / "sensors" / "synapse_i2c.c"],
    )
    lib.h_acquire.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    lib.h_release.argtypes = [ctypes.c_int]
    lib.h_set_present.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.h_probe.argtypes = [ctypes.c_int, ctypes.c_int]
    return lib


@pytest.fixture
def bus(_bus_lib: ctypes.CDLL) -> ctypes.CDLL:
    _bus_lib.h_reset()
    return _bus_lib


def test_a_probe_tells_present_from_absent(bus: ctypes.CDLL) -> None:
    assert bus.h_acquire(0, 8, 9, 400000) == 0
    bus.h_set_present(0x57, 1)
    assert bus.h_probe(0, 0x57) == 0
    assert bus.h_probe(0, 0x58) == ESP_ERR_NOT_FOUND


def test_a_probe_transfers_nothing_and_creates_no_device(bus: ctypes.CDLL) -> None:
    assert bus.h_acquire(0, 8, 9, 400000) == 0
    for addr in (0x40, 0x48, 0x57, 0x68):
        bus.h_probe(0, addr)
    assert bus.h_probes() == 4
    assert bus.h_transfers() == 0
    assert bus.h_devs_added() == 0, "a scan must not leave a handle behind for every address"


def test_a_probe_without_a_bus_or_on_a_missing_port(bus: ctypes.CDLL) -> None:
    assert bus.h_probe(0, 0x57) == ESP_ERR_INVALID_STATE
    assert bus.h_probe(2, 0x57) == ESP_ERR_INVALID_ARG


# ---------------------------------------------------------------------------------------------
# The parts that build only under ESP-IDF
# ---------------------------------------------------------------------------------------------


def test_every_driver_exposes_a_self_test_with_the_shared_shape() -> None:
    expected = {
        COMMON / "sensors" / "ppg_max30102.h": "ppg_max30102_self_test",
        COMMON / "sensors" / "imu_icm20948.h": "imu_icm20948_self_test",
        BAND / "sensors" / "temp_tmp117.h": "temp_tmp117_self_test",
        BAND / "sensors" / "ina219.h": "ina219_self_test",
    }
    for header, name in expected.items():
        assert re.search(rf"esp_err_t {name}\(char ?\* ?detail, size_t \w+\);", _text(header)), name


def test_the_imu_self_test_is_no_longer_a_stub_that_always_passes() -> None:
    code = _text(COMMON / "sensors" / "imu_icm20948.cpp")
    assert "self-test not implemented" not in code
    body = code[code.index("esp_err_t imu_icm20948_self_test(") :]
    assert "0xEA" in body, "it checks the identity"
    assert "st_imu_rest_ok(" in body
    assert "st_mag_ok(" in body
    assert body.count("return ESP_FAIL;") >= 3, "and it can fail"
    # the sensor task polls the same driver: the test must not run the stream's read path, which
    # updates the shared magnetometer cache and consumes the AK09916 data-ready flag
    assert "imu_icm20948_read(" not in body
    assert "s_ctx.mag_raw" not in body


def test_the_factory_test_starts_its_window_on_an_empty_queue() -> None:
    code = _text(BAND / "sensors" / "synapse_factory.c")
    body = code[code.index("static void step_acquisition(") :]
    drain = body.index("synapse_sensors_get_sample(&s) == ESP_OK")
    window = body.index("const int64_t end_us")
    assert drain < window, "the backlog is dropped before the 10 s are measured"


def test_the_power_monitor_cannot_be_built_on_another_port_than_the_bus() -> None:
    code = _text(BAND / "power" / "synapse_power.c")
    assert "CONFIG_SYNAPSE_INA219_I2C_PORT != CONFIG_SYNAPSE_PPG_I2C_PORT" in code
    assert "#error" in code


def test_the_ppg_self_test_checks_identity_temperature_leds_and_fifo() -> None:
    body = _text(COMMON / "sensors" / "ppg_max30102.cpp")
    body = body[body.index("esp_err_t ppg_max30102_self_test(") :]
    for needle in ("0x15", "st_max30102_temp_c(", "MAX30102_REG_LED1_PA", "st_fifo_advanced("):
        assert needle in body, needle


def test_the_factory_test_runs_every_check_and_gives_one_verdict() -> None:
    code = _text(BAND / "sensors" / "synapse_factory.c")
    for needle in (
        "synapse_diag_i2c_scan(",
        "ppg_max30102_self_test",
        "imu_icm20948_self_test",
        "temp_tmp117_self_test(",
        "ina219_self_test(",
        "st_count_ok(",
        "st_report_pass(",
        "FACTORY RESULT: PASS",
        "FACTORY RESULT: FAIL",
    ):
        assert needle in code, needle
    # a sensor that did not initialise is a failure, never a skipped line
    assert "did not initialise" in code


def test_the_factory_mode_is_off_everywhere_it_could_be_forced_on() -> None:
    kconfig = _text(BAND / "config" / "Kconfig")
    block = re.search(r"^config SYNAPSE_FACTORY_MODE\n(.*?)(?=^config |\Z)", kconfig, re.S | re.M)
    assert block, "SYNAPSE_FACTORY_MODE is not declared"
    assert "default n" in block.group(1)
    for defaults in BAND.glob("sdkconfig.defaults*"):
        assert "CONFIG_SYNAPSE_FACTORY_MODE=y" not in _text(defaults), defaults.name
    main = _text(BAND / "main" / "app_main.c")
    hook = main.index("synapse_factory_run()")
    assert "#if defined(CONFIG_SYNAPSE_FACTORY_MODE)" in main[max(0, hook - 80) : hook]
    assert '#include "synapse_factory.h"' in main


def test_a_sensor_that_does_not_come_up_scans_the_bus_by_itself() -> None:
    """No factory mode needed to learn 'nothing answers' from 'something answers at another address'."""
    code = _text(BAND / "sensors" / "synapse_sensors.c")
    helper = code[code.index("static void log_bus_scan(") :]
    helper = helper[: helper.index("\n}\n")]
    assert "synapse_i2c_acquire(" in helper, (
        "the bus may have no owner left when every driver failed"
    )
    assert "synapse_diag_i2c_scan(" in helper
    assert "synapse_i2c_release(" in helper, "and the reference taken for the scan is given back"
    call = code[code.index("log_bus_scan(&hw_cfg)") - 200 : code.index("log_bus_scan(&hw_cfg)")]
    assert "SENSOR_TYPE_PPG" in call
    assert "SENSOR_TYPE_IMU" in call


def test_the_new_sources_are_in_the_build() -> None:
    cmake = _text(BAND / "sensors" / "CMakeLists.txt")
    assert "synapse_factory.c" in cmake
    assert "synapse_diag.c" in cmake
