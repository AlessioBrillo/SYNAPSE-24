"""MVP-2.3: the flashed-config readback covers every active contract field.

The old readback carried only six keys, so a stale sdkconfig could change the
IMU range, the PPG pulse width or the I2C pins and still pass G1. These static
guards pin the key set; the host validator has the matching behavioural tests.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATT = (ROOT / "firmware" / "esp32_band" / "ble" / "ble_gatt_server.c").read_text(encoding="utf-8")

READBACK_KEYS = (
    "ecg_hz",
    "ppg_hz",
    "imu_hz",
    "ecg_gain_x100",
    "ppg_led_red",
    "ppg_led_ir",
    "battery_mah",
    "feature_rate_hz",
    "pending_reboot",
    "accel_bias_mg",
    "gyro_bias_mdps",
    "ecg_adc_ch",
    "ecg_lead_off",
    "ppg_addr",
    "ppg_sr",
    "ppg_pw",
    "ppg_adc",
    "imu_addr",
    "accel_g",
    "gyro_dps",
    "accel_hz",
    "gyro_hz",
    "i2c_port",
    "i2c_sda",
    "i2c_scl",
    "sync_gpio",
    "ina_addr",
    "ina_shunt",
    "power_profile",
    "cal_serial",
    "cal_state",
    "cal_date",
    "cal_operator",
    "cal_instrument",
    "cal_fw_version",
    "cal_uncertainty_pct_x100",
    "cal_noise_std_uv_x10",
)


def test_every_active_field_is_reported() -> None:
    fmt_start = GATT.index('"{\\"v\\":1')
    fmt_end = GATT.index("synapse_config_get_fw_version()", fmt_start)
    fmt = GATT[fmt_start:fmt_end]
    for key in READBACK_KEYS:
        assert f'\\"{key}\\"' in fmt, key


def test_operator_strings_cannot_break_the_json() -> None:
    assert "json_safe(" in GATT
    assert "cal_operator" in GATT


def test_config_write_is_a_narrow_whitelist_in_factory_mode() -> None:
    assert "CONFIG_SYNAPSE_FACTORY_MODE" in GATT
    for key in (
        "led_red",
        "led_ir",
        "gain_x100",
        "feature_rate_hz",
        "battery_mah",
        "accel_bias_mg",
        "gyro_bias_mdps",
    ):
        assert f'"{key}"' in GATT
    assert "config write refused: factory mode off" in GATT
    assert "no whitelisted field" in GATT
    assert "unknown field in payload" in GATT
    assert "value out of range" in GATT


def test_whitelist_parsers_fail_closed() -> None:
    helper = (ROOT / "firmware" / "esp32_band" / "config" / "synapse_config_json.h").read_text(
        encoding="utf-8"
    )
    # Signed values must be refused: on the 32-bit firmware strtoul() would
    # wrap `{"battery_mah":-4294966296}` into 1000 and commit it.
    assert "*p == '-' || *p == '+'" in helper
    assert "errno == ERANGE" in helper
    assert "ULONG_MAX > 0xFFFFFFFFu" in helper
    # A truncated number and a malformed array are not silently accepted.
    assert "*end != ',' && *end != '}'" in helper
    assert "if (*p != '[') return false;" in helper
    assert "if (*p != ',') return false;" in helper
    assert "if (*p != ']') return false;" in helper
    # One balanced object, no duplicate keys, no quoted value, no trailing junk.
    assert "CFG_JSON_ERR_DUPLICATE_KEY" in helper
    assert "CFG_JSON_ERR_MALFORMED" in helper
    assert "if (*before != '{' && *before != ',')" in helper
    assert "if (*after != ':')" in helper
    assert "CFG_JSON_OK : CFG_JSON_ERR_MALFORMED" in helper
    assert "json_safe(" in GATT


def test_readback_reports_state_and_effective_pending_reboot() -> None:
    assert "synapse_calibration_effective_crc(&cal)" in GATT
    assert 'strcmp(state, "defaults") == 0 ? "none" : state' in GATT
    assert "synapse_calibration_state()" in GATT
    for key in (
        "cal_instrument",
        "cal_fw_version",
        "cal_uncertainty_pct_x100",
        "cal_noise_std_uv_x10",
    ):
        assert f'\\"{key}\\"' in GATT


def test_per_unit_writes_need_a_provenance_complete_record() -> None:
    assert "cal_has_provenance" in GATT
    for field in (
        "serial",
        "date",
        "operator",
        "instrument",
        "fw_version",
        "gain_uncertainty_pct_x100",
    ):
        assert f"cal->{field}" in GATT
    assert "no provenance-complete calibration record" in GATT
    assert "BLE_ATT_ERR_WRITE_NOT_PERMITTED" in GATT
    assert "malformed JSON payload" in GATT
    assert "duplicate key in payload" in GATT
    assert "feature_rate_hz is session-only" in GATT
    assert "if (!per_unit) return 0;" in GATT  # rate-only: RAM, never persisted


def test_product_build_never_registers_the_raw_service() -> None:
    assert "CONFIG_SYNAPSE_PRODUCT_MODE" in GATT
    assert "raw research service is disabled" in GATT
    kconfig = (ROOT / "firmware" / "esp32_band" / "config" / "Kconfig").read_text(encoding="utf-8")
    assert "config SYNAPSE_PRODUCT_MODE" in kconfig


def test_contract_owned_gpio_and_ina_values_are_used() -> None:
    for macro in (
        "SYNAPSE_CONTRACT_WIRED_SYNC_GPIO",
        "SYNAPSE_CONTRACT_INA219_I2C_ADDR",
        "SYNAPSE_CONTRACT_INA219_SHUNT_MOHM",
    ):
        assert macro in GATT
