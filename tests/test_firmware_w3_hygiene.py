"""MVP-1.9 W3: firmware hygiene that only builds under ESP-IDF, guarded statically.

``band-core`` is rebuilt from scratch for the PR (see the PR comment); these tests keep the fixes from
being undone by an edit nobody compiles on the host.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
COMMON = ROOT / "firmware" / "common"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _struct(source: str, name: str) -> str:
    m = re.search(r"typedef struct \{(.*?)\} " + name + ";", source, re.S)
    assert m is not None, name
    return re.sub(r"\s+", " ", re.sub(r"//[^\n]*", "", m.group(1))).strip()


def test_octal_psram_is_selected_for_the_n8r8_module() -> None:
    defaults = _text(BAND / "sdkconfig.defaults")
    assert "CONFIG_SPIRAM_MODE_OCT=y" in defaults
    assert "CONFIG_SPIRAM_TYPE_AUTO=y" in defaults
    assert "CONFIG_SPIRAM_TYPE_ESPPSRAM64" not in defaults, "that is a quad chip"
    assert "CONFIG_SPIRAM_MODE_QUAD" not in defaults


def test_the_defaults_carry_no_line_that_does_nothing() -> None:
    defaults = _text(BAND / "sdkconfig.defaults")
    for dead in (
        "CONFIG_BT_NIMBLE_LOG_LEVEL=0",  # a choice option: the level is picked by the symbol
        "CONFIG_BT_NIMBLE_GATT_MAX_",  # not symbols
        "CONFIG_I2C_ISR_HANDLER_IN_IRAM",  # not a symbol
        "CONFIG_SPIRAM_CACHE_WORKAROUND",  # ESP32 (not S3) only
        "CONFIG_ESP32S3_SPIRAM_SUPPORT",  # old name of CONFIG_SPIRAM
        "CONFIG_ESP32S3_DEFAULT_CPU_FREQ",  # old names
    ):
        assert dead not in defaults, dead
    assert "CONFIG_ESP_DEFAULT_CPU_FREQ_MHZ_240=y" in defaults


def test_a_rejected_nvs_config_never_reaches_the_live_config() -> None:
    code = _text(BAND / "config" / "synapse_config.c")
    load = code[code.index("static esp_err_t load_from_nvs(void) {") :]
    load = load[: load.index("\n}\n")]
    assert "synapse_hw_config_t stored;" in load
    assert "nvs_get_blob(handle, SYNAPSE_CONFIG_KEY, &stored," in load
    assert "&g_config" not in load.replace("memcpy(&g_config, &stored", ""), (
        "read into a local copy"
    )
    # the live config is replaced only after the version matched and validation passed
    assert load.index("validate_config(&stored)") < load.index("memcpy(&g_config, &stored")
    assert load.index("nvs_get_u32(handle, SYNAPSE_CONFIG_VERSION_KEY") < load.index(
        "validate_config"
    )
    assert "handle = nvs_open(" not in code, "the handle was overwritten by the return code"


def test_the_sensor_tasks_run_on_their_own_core_with_a_queue_that_outlasts_a_slow_tick() -> None:
    sched_h = _text(COMMON / "scheduler" / "sensor_scheduler.h")
    assert "#define SENSOR_TASK_CORE 1" in sched_h
    assert "#define SENSOR_SCHEDULER_MAX_SENSORS 3" in sched_h
    queue = int(re.search(r"define SENSOR_SCHEDULER_BLE_QUEUE_SIZE (\d+)", sched_h).group(1))
    # 600 samples/s (500 ECG + 50 PPG + 50 IMU): the queue must hold more than a quarter second
    assert queue / 600 >= 0.25
    sensors = _text(BAND / "sensors" / "synapse_sensors.c")
    assert "xQueueCreate(SENSOR_SCHEDULER_BLE_QUEUE_SIZE, sizeof(sensor_sample_t))" in sensors
    assert "xTaskCreatePinnedToCore(sensor_task_fn" in sensors
    assert "xTaskCreatePinnedToCore(coordinator_task_fn" in _text(
        COMMON / "scheduler" / "sensor_scheduler.cpp"
    )
    assert "xTaskCreate(" not in sensors


def test_every_polled_sensor_type_fits_the_scheduler_arrays() -> None:
    sched_h = _text(COMMON / "scheduler" / "sensor_scheduler.h")
    max_sensors = int(re.search(r"define SENSOR_SCHEDULER_MAX_SENSORS (\d+)", sched_h).group(1))
    enum = re.search(r"typedef enum \{(.*?)\} sensor_type_t;", sched_h, re.S).group(1)
    types = dict(re.findall(r"(SENSOR_TYPE_\w+)\s*=\s*(\d+)", enum))
    for polled in ("SENSOR_TYPE_ECG", "SENSOR_TYPE_PPG", "SENSOR_TYPE_IMU"):
        assert int(types[polled]) < max_sensors, polled


def test_the_advertised_name_is_the_kconfig_one() -> None:
    code = _text(BAND / "ble" / "ble_gatt_server.c")
    assert "#define SYNAPSE_BLE_DEVICE_NAME CONFIG_SYNAPSE_BLE_DEVICE_NAME" in code
    assert '"SYNAPSE-BAND"' not in code


def test_the_host_stub_of_the_sample_struct_is_the_real_one() -> None:
    """The stub lets the research service be compiled on the host; a drifted layout would test a ghost."""
    real = _struct(_text(COMMON / "scheduler" / "sensor_scheduler.h"), "sensor_sample_t")
    stub = _struct(
        _text(ROOT / "tests" / "firmware_host" / "stubs" / "sensor_scheduler.h"), "sensor_sample_t"
    )
    assert real == stub


def test_a_header_macro_is_defined_once() -> None:
    raw = _text(COMMON / "ble" / "synapse_raw_frame.h")
    assert raw.count("#define SYNAPSE_RAW_FRAME_MAX_SAMPLES") == 1
