"""Static guards for the Synapse Band first-light defects (MVP-1.3 PR-B).

``firmware/esp32_band`` only builds under ESP-IDF and has never run on a
board, and the host C gate stubs the scheduler, the drivers, the ADC and the
GATT server. Each defect below stopped the band from booting or left a data
stream empty, and none of them was visible to the host gate:

* the scheduler dereferenced a NULL ``task_handle`` while registering the first
  sensor, and the band never created the sensor tasks (zero samples);
* ADC1 was opened twice (ECG and battery), which aborted the boot;
* ``ble_gatts_find_chr()`` returns an rc, not a handle, so the feature stream,
  Heart Rate and Battery notifications were dead;
* the IMU enabled its internal I2C master and then talked to the magnetometer
  in bypass, and any I2C error aborted via ``ESP_ERROR_CHECK``;
* the PPG FIFO was never enabled, the register map was wrong and the Kconfig
  sample-rate code was not the datasheet's;
* GPIO 27/34/36 are flash/PSRAM pins on the N8R8 module, and GPIO 4 was both
  the lead-off input and the battery ADC.

The assertions are static (no compiler, no board): they scan the sources for
the structures the fixes introduced. They prove the fixes are still in place,
not that the band works: that needs the hardware (``docs/hardware/BRINGUP_SOP.md``).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BAND = ROOT / "firmware" / "esp32_band"
COMMON = ROOT / "firmware" / "common"

SCHEDULER = COMMON / "scheduler" / "sensor_scheduler.cpp"
SENSORS = BAND / "sensors" / "synapse_sensors.c"
ADC1 = BAND / "sensors" / "synapse_adc1.c"
POWER = BAND / "power" / "synapse_power.c"
ECG = COMMON / "sensors" / "ecg_ad8232.cpp"
IMU = COMMON / "sensors" / "imu_icm20948.cpp"
PPG = COMMON / "sensors" / "ppg_max30102.cpp"
GATT = BAND / "ble" / "ble_gatt_server.c"
KCONFIG = BAND / "config" / "Kconfig"
WIRED_H = COMMON / "sync" / "wired_sync_handler.h"
HW_YAML = ROOT / "config" / "hardware.yaml"
APP_MAIN = BAND / "main" / "app_main.c"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


# --- scheduler / sensor tasks -------------------------------------------------


def test_the_scheduler_tolerates_a_registration_without_a_task_handle() -> None:
    assert "config->task_handle ? *config->task_handle : NULL" in _src(SCHEDULER)


def test_the_band_spawns_one_task_per_sensor_and_publishes_it() -> None:
    src = _src(SENSORS)
    assert "xTaskCreate(sensor_task_fn" in src
    assert "sensor_scheduler_set_task(" in src
    # the task reads its config for as long as it lives: it must not be a local
    assert re.search(r"^static sensor_config_t s_sensor_cfg\[", src, re.M)
    for idx in (0, 1, 2):
        assert f"register_and_spawn({idx}," in src


def test_a_failed_sensor_init_is_recorded_not_fatal() -> None:
    src = _src(SENSORS)
    assert "s_sensor_health" in src
    assert "note_init(SENSOR_TYPE_ECG" in src
    assert "note_init(SENSOR_TYPE_PPG" in src
    assert "note_init(SENSOR_TYPE_IMU" in src


# --- ADC1 ---------------------------------------------------------------------


def test_adc1_has_a_single_owner_in_the_band() -> None:
    band_sources = [
        p for p in BAND.rglob("*") if p.suffix in {".c", ".cpp", ".h"} and "build" not in p.parts
    ]
    owners = [p.name for p in band_sources if "adc_oneshot_new_unit(" in _strip_comments(_src(p))]
    assert owners == ["synapse_adc1.c"], owners


def test_ecg_and_battery_borrow_the_shared_adc1_handle() -> None:
    assert "synapse_adc1_get(&g_adc_handle)" in _src(POWER)
    assert "adc_oneshot_del_unit" not in _strip_comments(_src(POWER))
    assert "synapse_adc1_get(&adc1)" in _src(SENSORS)
    ecg = _src(ECG)
    # the ECG driver only creates its own unit when it was not handed one
    assert "if (config->adc_handle)" in ecg
    assert "s_ctx.adc_handle && s_ctx.owns_adc" in ecg


# --- GATT handles ---------------------------------------------------------------


def test_gatt_value_handles_come_from_the_service_table() -> None:
    code = _strip_comments(_src(GATT))
    assert "ble_gatts_find_chr" not in code
    for handle in ("s_hr_handle", "s_batt_handle", "s_feat_handle", "s_prov_handle"):
        assert f".val_handle = &{handle}" in code, handle


def test_hr_and_battery_notifications_wait_for_a_subscription() -> None:
    code = _strip_comments(_src(GATT))
    assert "s_hr_handle == 0 || !s_hr_subscribed" in code
    assert "s_batt_handle == 0 || !s_batt_subscribed" in code
    assert "s_hr_subscribed = event->subscribe.cur_notify" in code
    assert "s_batt_subscribed = event->subscribe.cur_notify" in code


# --- IMU ------------------------------------------------------------------------


def test_the_imu_reaches_the_magnetometer_through_bypass() -> None:
    code = _strip_comments(_src(IMU))
    # USER_CTRL = 0x20 turns the internal I2C master on, which disables bypass
    assert re.search(r"ICM20948_REG_USER_CTRL,\s*0x00\)", code)
    assert re.search(r"ICM20948_REG_INT_PIN_CFG,\s*0x02\)", code)
    assert "ICM20948_REG_USER_CTRL, 0x20" not in code
    # ST1..ST2 burst: reading ST2 releases the data protection
    assert re.search(r"AK09916_REG_ST1, mag, 9\)", code)


def test_the_imu_applies_the_output_data_rate() -> None:
    code = _strip_comments(_src(IMU))
    assert "ICM20948_REG_ACCEL_SMPLRT_DIV_1" in code
    assert "ICM20948_REG_GYRO_SMPLRT_DIV" in code


def test_the_sensor_drivers_never_abort_on_an_i2c_error() -> None:
    for driver in (IMU, PPG):
        assert "ESP_ERROR_CHECK(" not in _strip_comments(_src(driver)), driver.name


# --- PPG --------------------------------------------------------------------------


def test_the_ppg_uses_spo2_mode_with_a_derived_spo2_config() -> None:
    code = _strip_comments(_src(PPG))
    assert "MAX30102_REG_MODE_CONFIG, MAX30102_MODE_SPO2" in code
    assert "MAX30102_MODE_MULTI_LED)" not in code
    assert "MAX30102_REG_SPO2_CONFIG, 0x27" not in code
    assert "spo2_cfg" in code
    # the FIFO must be on once init is done, or every read returns INVALID_STATE
    assert "s_ctx.fifo_enabled = true;  // acquisition starts" in _src(PPG)


def test_the_ppg_sample_rate_is_the_datasheet_spo2_sr_code() -> None:
    kconfig = _src(KCONFIG)
    block = re.search(r"config SYNAPSE_PPG_SAMPLE_RATE\n(.*?)\n\nconfig", kconfig, re.S)
    assert block, "SYNAPSE_PPG_SAMPLE_RATE not found"
    assert "default 0x00" in block.group(1)
    assert "There is no 64 Hz setting" in block.group(1)
    assert "== 0x02 ? 50 : 64" not in _src(SENSORS)


# --- pins -------------------------------------------------------------------------


def _kconfig_int(symbol: str) -> int:
    m = re.search(rf"config {symbol}\n(?:.*\n)*?\s+default (-?\d+)", _src(KCONFIG))
    assert m, symbol
    return int(m.group(1))


def _band_gpio_map() -> dict[str, int]:
    """Every GPIO the band claims by default. ESP32-S3: ADC1_CHn is GPIO(n+1)."""
    return {
        "ecg_adc": _kconfig_int("SYNAPSE_ECG_ADC_CHANNEL") + 1,
        "bat_adc": _kconfig_int("SYNAPSE_BAT_ADC_CHANNEL") + 1,
        "ecg_leadoff": _kconfig_int("SYNAPSE_ECG_GPIO_DRDY"),
        "ppg_int": _kconfig_int("SYNAPSE_PPG_GPIO_INT"),
        "imu_int": _kconfig_int("SYNAPSE_IMU_GPIO_INT"),
        "temp_alert": _kconfig_int("SYNAPSE_TEMP_GPIO_ALERT"),
        "gps_tx": _kconfig_int("SYNAPSE_GPS_UART_TX_GPIO"),
        "gps_rx": _kconfig_int("SYNAPSE_GPS_UART_RX_GPIO"),
        "i2c_sda": 8,
        "i2c_scl": 9,
        "wired_sync": int(re.search(r"SYNAPSE_WIRED_SYNC_GPIO\s+(\d+)", _src(WIRED_H)).group(1)),  # type: ignore[union-attr]
    }


def test_no_default_pin_is_shared_between_two_functions() -> None:
    pins = _band_gpio_map()
    by_gpio: dict[int, list[str]] = {}
    for name, gpio in pins.items():
        by_gpio.setdefault(gpio, []).append(name)
    shared = {g: names for g, names in by_gpio.items() if len(names) > 1}
    assert not shared, shared


def test_no_default_pin_is_a_flash_or_psram_pin() -> None:
    # SPI flash 26-32 and octal PSRAM 33-37 on the DevKitC-1 N8R8 module
    reserved = set(range(26, 38))
    used = {n: g for n, g in _band_gpio_map().items() if g in reserved}
    assert not used, used


def test_the_hardware_yaml_agrees_with_the_firmware_pins() -> None:
    yaml_text = _src(HW_YAML)
    pins = _band_gpio_map()
    assert re.search(rf"wired_sync_gpio:\s*{pins['wired_sync']}\b", yaml_text)
    assert re.search(rf"adc_pin:\s*{pins['ecg_adc']}\b", yaml_text)
    assert re.search(rf"gpio_drdy:\s*{pins['ecg_leadoff']}\b", yaml_text)


# --- edge AI ----------------------------------------------------------------------


def test_edge_ai_is_off_until_its_arena_bug_is_fixed() -> None:
    assert re.search(r"config SYNAPSE_FEATURE_EDGE_AI\n\s+bool .*\n\s+default n\b", _src(KCONFIG))
    main = _src(APP_MAIN)
    assert main.count("#if defined(CONFIG_SYNAPSE_FEATURE_EDGE_AI)") == 2


# --- review follow-ups (PR #56) ----------------------------------------------------


def test_a_failed_read_is_counted_not_pushed_as_a_sample() -> None:
    task = _src(SCHEDULER).split("void sensor_task_fn", 1)[1]
    assert "config->read(&sample, config->user_ctx) != ESP_OK" in task
    assert "read_errors[config->type]++" in task
    # the sequence number is consumed only by a delivered sample
    assert task.index("read_errors[config->type]++") < task.index("sample_counts[config->type]++")
    wrappers = _src(SENSORS)
    for name in ("ecg", "ppg", "imu"):
        assert f"static esp_err_t {name}_read_wrap" in wrappers, name


def test_a_sensor_that_failed_init_gets_no_task_and_no_abort() -> None:
    src = _src(SENSORS)
    assert "no polling task" in src
    assert "ESP_ERROR_CHECK(register_and_spawn" not in src


def test_the_imu_gyro_divider_uses_the_gyro_base_rate() -> None:
    code = _strip_comments(_src(IMU))
    assert re.search(r"ICM20948_GYRO_BASE_ODR_HZ\s+1100", code)
    assert "smplrt_div_for(ICM20948_GYRO_BASE_ODR_HZ, config->gyro_odr_hz)" in code
    assert "smplrt_div_for(ICM20948_ACCEL_BASE_ODR_HZ, config->accel_odr_hz)" in code


def test_the_ppg_tells_an_empty_fifo_from_a_wrapped_one() -> None:
    code = _strip_comments(_src(PPG))
    assert "MAX30102_REG_OVF_COUNTER, &ovf" in code
    assert "avail = 32;" in code
