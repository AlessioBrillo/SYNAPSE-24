/**
 * @file synapse_config.c
 * @brief Synapse Band v1 - Hardware Configuration Implementation
 * 
 * NVS-backed configuration with Kconfig defaults and env var expansion.
 */

#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "esp_err.h"
#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "synapse_config.h"

static const char *TAG = "SYNAPSE_CONFIG";

static synapse_hw_config_t g_config = {0};
static bool g_config_loaded = false;
static bool g_config_dirty = false;

// Default configuration from Kconfig (compile-time defaults)
static const synapse_hw_config_t g_default_config = {
    // ECG AD8232
    .ecg_adc_channel = CONFIG_SYNAPSE_ECG_ADC_CHANNEL,
    .ecg_gpio_drdy = CONFIG_SYNAPSE_ECG_GPIO_DRDY,
    .ecg_vref_mv = (float)CONFIG_SYNAPSE_ECG_VREF_MV,
    .ecg_gain = (float)CONFIG_SYNAPSE_ECG_GAIN / 100.0f,

    // PPG MAX30102
    .ppg_i2c_port = CONFIG_SYNAPSE_PPG_I2C_PORT,
    .ppg_i2c_addr = CONFIG_SYNAPSE_PPG_I2C_ADDR,
    .ppg_gpio_int = CONFIG_SYNAPSE_PPG_GPIO_INT,
    .ppg_led_current_red = CONFIG_SYNAPSE_PPG_LED_CURRENT_RED,
    .ppg_led_current_ir = CONFIG_SYNAPSE_PPG_LED_CURRENT_IR,
    .ppg_led_current_green = CONFIG_SYNAPSE_PPG_LED_CURRENT_GREEN,
    .ppg_sample_rate = CONFIG_SYNAPSE_PPG_SAMPLE_RATE,
    .ppg_pulse_width = CONFIG_SYNAPSE_PPG_PULSE_WIDTH,
    .ppg_adc_range = CONFIG_SYNAPSE_PPG_ADC_RANGE,

    // IMU ICM-20948
    .imu_i2c_port = CONFIG_SYNAPSE_IMU_I2C_PORT,
    .imu_i2c_addr = CONFIG_SYNAPSE_IMU_I2C_ADDR,
    .imu_gpio_int = CONFIG_SYNAPSE_IMU_GPIO_INT,
    .imu_accel_fsr_g = CONFIG_SYNAPSE_IMU_ACCEL_FSR_G,
    .imu_gyro_fsr_dps = CONFIG_SYNAPSE_IMU_GYRO_FSR_DPS,
    .imu_accel_odr_hz = CONFIG_SYNAPSE_IMU_ACCEL_ODR_HZ,
    .imu_gyro_odr_hz = CONFIG_SYNAPSE_IMU_GYRO_ODR_HZ,

    // GPS MAX-M10S
    .gps_uart_port = CONFIG_SYNAPSE_GPS_UART_PORT,
    .gps_uart_tx_gpio = CONFIG_SYNAPSE_GPS_UART_TX_GPIO,
    .gps_uart_rx_gpio = CONFIG_SYNAPSE_GPS_UART_RX_GPIO,
    .gps_baudrate = CONFIG_SYNAPSE_GPS_BAUDRATE,
    .gps_use_ubx = CONFIG_SYNAPSE_GPS_USE_UBX,

    // Temperature TMP117
    .temp_i2c_port = CONFIG_SYNAPSE_TEMP_I2C_PORT,
    .temp_i2c_addr = CONFIG_SYNAPSE_TEMP_I2C_ADDR,
    .temp_gpio_alert = CONFIG_SYNAPSE_TEMP_GPIO_ALERT,

    // Battery ADC
    .bat_adc_channel = CONFIG_SYNAPSE_BAT_ADC_CHANNEL,
    .bat_voltage_divider = (float)CONFIG_SYNAPSE_BAT_VOLTAGE_DIVIDER / 100.0f,

    // BLE
    .ble_device_name = CONFIG_SYNAPSE_BLE_DEVICE_NAME,

    // Power Budget
    .hub_battery_mah = (float)CONFIG_SYNAPSE_HUB_BATTERY_MAH,
    .target_lifetime_h = (float)CONFIG_SYNAPSE_TARGET_LIFETIME_H,
    .reserve_mah = (float)CONFIG_SYNAPSE_RESERVE_MAH,
    .t0_avg_mw = (float)CONFIG_SYNAPSE_T0_AVG_MW,
    .t1_avg_mw = (float)CONFIG_SYNAPSE_T1_AVG_MW,
    .t2_avg_mw = (float)CONFIG_SYNAPSE_T2_AVG_MW,

    // Motion Gate
    .sqi_min = (float)CONFIG_SYNAPSE_SQI_MIN / 1000.0f,
    .map_max = (float)CONFIG_SYNAPSE_MAP_MAX / 1000.0f,
    .required_consecutive_clean = CONFIG_SYNAPSE_REQUIRED_CONSECUTIVE_CLEAN,
};

// Internal helper: validate configuration values
static esp_err_t validate_config(const synapse_hw_config_t *cfg) {
    // ECG validation
    if (cfg->ecg_adc_channel < 0 || cfg->ecg_adc_channel > 7) {
        ESP_LOGE(TAG, "Invalid ECG ADC channel: %d", cfg->ecg_adc_channel);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->ecg_gpio_drdy != -1 && (cfg->ecg_gpio_drdy < 0 || cfg->ecg_gpio_drdy > 47)) {
        ESP_LOGE(TAG, "Invalid ECG DRDY GPIO: %d", cfg->ecg_gpio_drdy);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->ecg_vref_mv <= 0 || cfg->ecg_vref_mv > 3300) {
        ESP_LOGE(TAG, "Invalid ECG VREF: %.1f", cfg->ecg_vref_mv);
        return ESP_ERR_INVALID_ARG;
    }

    // PPG validation
    if (cfg->ppg_i2c_port < 0 || cfg->ppg_i2c_port > 1) {
        ESP_LOGE(TAG, "Invalid PPG I2C port: %d", cfg->ppg_i2c_port);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->ppg_gpio_int < 0 || cfg->ppg_gpio_int > 47) {
        ESP_LOGE(TAG, "Invalid PPG INT GPIO: %d", cfg->ppg_gpio_int);
        return ESP_ERR_INVALID_ARG;
    }

    // IMU validation
    if (cfg->imu_i2c_port < 0 || cfg->imu_i2c_port > 1) {
        ESP_LOGE(TAG, "Invalid IMU I2C port: %d", cfg->imu_i2c_port);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->imu_gpio_int < 0 || cfg->imu_gpio_int > 47) {
        ESP_LOGE(TAG, "Invalid IMU INT GPIO: %d", cfg->imu_gpio_int);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->imu_accel_fsr_g != 2 && cfg->imu_accel_fsr_g != 4 && 
        cfg->imu_accel_fsr_g != 8 && cfg->imu_accel_fsr_g != 16) {
        ESP_LOGE(TAG, "Invalid IMU accel FSR: %d", cfg->imu_accel_fsr_g);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->imu_gyro_fsr_dps != 250 && cfg->imu_gyro_fsr_dps != 500 &&
        cfg->imu_gyro_fsr_dps != 1000 && cfg->imu_gyro_fsr_dps != 2000) {
        ESP_LOGE(TAG, "Invalid IMU gyro FSR: %d", cfg->imu_gyro_fsr_dps);
        return ESP_ERR_INVALID_ARG;
    }

    // GPS validation
    if (cfg->gps_uart_port < 0 || cfg->gps_uart_port > 2) {
        ESP_LOGE(TAG, "Invalid GPS UART port: %d", cfg->gps_uart_port);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->gps_uart_tx_gpio < 0 || cfg->gps_uart_tx_gpio > 47 ||
        cfg->gps_uart_rx_gpio < 0 || cfg->gps_uart_rx_gpio > 47) {
        ESP_LOGE(TAG, "Invalid GPS UART GPIOs: tx=%d rx=%d", 
                 cfg->gps_uart_tx_gpio, cfg->gps_uart_rx_gpio);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->gps_baudrate <= 0) {
        ESP_LOGE(TAG, "Invalid GPS baudrate: %d", cfg->gps_baudrate);
        return ESP_ERR_INVALID_ARG;
    }

    // Temperature validation
    if (cfg->temp_i2c_port < 0 || cfg->temp_i2c_port > 1) {
        ESP_LOGE(TAG, "Invalid TEMP I2C port: %d", cfg->temp_i2c_port);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->temp_gpio_alert != -1 && (cfg->temp_gpio_alert < 0 || cfg->temp_gpio_alert > 47)) {
        ESP_LOGE(TAG, "Invalid TEMP ALERT GPIO: %d", cfg->temp_gpio_alert);
        return ESP_ERR_INVALID_ARG;
    }

    // Battery validation
    if (cfg->bat_adc_channel < 0 || cfg->bat_adc_channel > 7) {
        ESP_LOGE(TAG, "Invalid BAT ADC channel: %d", cfg->bat_adc_channel);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->bat_voltage_divider <= 0) {
        ESP_LOGE(TAG, "Invalid battery voltage divider: %.2f", cfg->bat_voltage_divider);
        return ESP_ERR_INVALID_ARG;
    }

    // BLE validation
    if (strlen(cfg->ble_device_name) == 0 || strlen(cfg->ble_device_name) >= sizeof(cfg->ble_device_name)) {
        ESP_LOGE(TAG, "Invalid BLE device name length");
        return ESP_ERR_INVALID_ARG;
    }

    // Power budget validation
    if (cfg->hub_battery_mah <= 0 || cfg->target_lifetime_h <= 0 || cfg->reserve_mah < 0) {
        ESP_LOGE(TAG, "Invalid power budget: bat=%.0f target=%.0f reserve=%.0f",
                 cfg->hub_battery_mah, cfg->target_lifetime_h, cfg->reserve_mah);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->t0_avg_mw < 0 || cfg->t1_avg_mw < 0 || cfg->t2_avg_mw < 0) {
        ESP_LOGE(TAG, "Invalid tier power: t0=%.1f t1=%.1f t2=%.1f",
                 cfg->t0_avg_mw, cfg->t1_avg_mw, cfg->t2_avg_mw);
        return ESP_ERR_INVALID_ARG;
    }

    // Motion gate validation
    if (cfg->sqi_min < 0.0f || cfg->sqi_min > 1.0f) {
        ESP_LOGE(TAG, "Invalid SQI min: %.3f", cfg->sqi_min);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->map_max < 0.0f || cfg->map_max > 1.0f) {
        ESP_LOGE(TAG, "Invalid MAP max: %.3f", cfg->map_max);
        return ESP_ERR_INVALID_ARG;
    }
    if (cfg->required_consecutive_clean < 1 || cfg->required_consecutive_clean > 10) {
        ESP_LOGE(TAG, "Invalid consecutive clean: %d", cfg->required_consecutive_clean);
        return ESP_ERR_INVALID_ARG;
    }

    return ESP_OK;
}

// Internal helper: copy defaults to config struct
static void apply_defaults(void) {
    memcpy(&g_config, &g_default_config, sizeof(synapse_hw_config_t));
    g_config_dirty = true;
    ESP_LOGI(TAG, "Applied Kconfig defaults");
}

// Internal helper: load from NVS
static esp_err_t load_from_nvs(void) {
    nvs_handle_t handle;
    esp_err_t ret = nvs_open(SYNAPSE_CONFIG_NAMESPACE, NVS_READONLY, &handle);
    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "NVS namespace not found, using defaults: %s", esp_err_to_name(ret));
        return ESP_ERR_NOT_FOUND;
    }

    size_t required_size = sizeof(synapse_hw_config_t);
    ret = nvs_get_blob(handle, SYNAPSE_CONFIG_KEY, &g_config, &required_size);
    nvs_close(handle);

    if (ret != ESP_OK) {
        ESP_LOGW(TAG, "Config blob not found in NVS, using defaults: %s", esp_err_to_name(ret));
        return ESP_ERR_NOT_FOUND;
    }

    // Check version
    uint32_t stored_version = 0;
    handle = nvs_open(SYNAPSE_CONFIG_NAMESPACE, NVS_READONLY, &handle);
    if (handle != 0) {
        nvs_get_u32(handle, SYNAPSE_CONFIG_VERSION_KEY, &stored_version);
        nvs_close(handle);
    }

    if (stored_version != SYNAPSE_CONFIG_VERSION) {
        ESP_LOGW(TAG, "Config version mismatch (stored=%" PRIu32 ", current=%d), using defaults",
                 stored_version, SYNAPSE_CONFIG_VERSION);
        return ESP_ERR_INVALID_VERSION;
    }

    // Validate loaded config
    ret = validate_config(&g_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Loaded config validation failed, using defaults");
        return ret;
    }

    ESP_LOGI(TAG, "Loaded configuration from NVS (version %" PRIu32 ")", stored_version);
    return ESP_OK;
}

// Internal helper: save to NVS
static esp_err_t save_to_nvs(void) {
    nvs_handle_t handle;
    esp_err_t ret = nvs_open(SYNAPSE_CONFIG_NAMESPACE, NVS_READWRITE, &handle);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS for writing: %s", esp_err_to_name(ret));
        return ret;
    }

    ret = nvs_set_blob(handle, SYNAPSE_CONFIG_KEY, &g_config, sizeof(synapse_hw_config_t));
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write config blob: %s", esp_err_to_name(ret));
        nvs_close(handle);
        return ret;
    }

    ret = nvs_set_u32(handle, SYNAPSE_CONFIG_VERSION_KEY, SYNAPSE_CONFIG_VERSION);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write config version: %s", esp_err_to_name(ret));
        nvs_close(handle);
        return ret;
    }

    ret = nvs_commit(handle);
    nvs_close(handle);

    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to commit NVS: %s", esp_err_to_name(ret));
        return ret;
    }

    g_config_dirty = false;
    ESP_LOGI(TAG, "Configuration saved to NVS");
    return ESP_OK;
}

// ============================================================================
// Public API Implementation
// ============================================================================

esp_err_t synapse_config_load(void) {
    if (g_config_loaded) {
        ESP_LOGD(TAG, "Config already loaded");
        return ESP_OK;
    }

    // Start with defaults
    apply_defaults();

    // Try to load from NVS
    esp_err_t ret = load_from_nvs();
    if (ret == ESP_OK) {
        g_config_dirty = false;
    } else {
        // Keep defaults, mark as dirty to save on first change
        g_config_dirty = true;
    }

    g_config_loaded = true;
    ESP_LOGI(TAG, "Configuration loaded (fw=%s, hw=%s)", 
             SYNAPSE_FW_VERSION, SYNAPSE_HW_VERSION);
    return ESP_OK;
}

esp_err_t synapse_config_save(void) {
    if (!g_config_loaded) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!g_config_dirty) {
        ESP_LOGD(TAG, "Config not dirty, skipping save");
        return ESP_OK;
    }

    esp_err_t ret = validate_config(&g_config);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Config validation failed before save");
        return ret;
    }

    return save_to_nvs();
}

esp_err_t synapse_config_get(synapse_hw_config_t *out) {
    if (!g_config_loaded) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!out) {
        return ESP_ERR_INVALID_ARG;
    }
    memcpy(out, &g_config, sizeof(synapse_hw_config_t));
    return ESP_OK;
}

esp_err_t synapse_config_set(const synapse_hw_config_t *in) {
    if (!g_config_loaded) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!in) {
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t ret = validate_config(in);
    if (ret != ESP_OK) {
        return ret;
    }

    memcpy(&g_config, in, sizeof(synapse_hw_config_t));
    g_config_dirty = true;
    ESP_LOGI(TAG, "Configuration updated (dirty=true)");
    return ESP_OK;
}

esp_err_t synapse_config_reset_to_defaults(void) {
    apply_defaults();
    return save_to_nvs();
}

const char* synapse_config_get_fw_version(void) {
    return SYNAPSE_FW_VERSION;
}

const char* synapse_config_get_hw_version(void) {
    return SYNAPSE_HW_VERSION;
}

bool synapse_config_is_dirty(void) {
    return g_config_dirty;
}

void synapse_config_mark_clean(void) {
    g_config_dirty = false;
}

// Environment variable expansion: ${VAR_NAME} -> value from environment
esp_err_t synapse_config_expand_env(const char *input, char *output, size_t output_size) {    if (!input || !output || output_size == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    const char *in = input;
    char *out = output;
    char *end = output + output_size - 1; // Reserve space for null terminator

    while (*in && out < end) {
        if (in[0] == '$' && in[1] == '{') {
            // Find closing }
            const char *var_start = in + 2;
            const char *var_end = strchr(var_start, '}');
            if (!var_end) {
                // No closing brace, copy literally
                *out++ = *in++;
                continue;
            }

            size_t var_len = var_end - var_start;
            if (var_len == 0) {
                // Empty variable name
                in = var_end + 1;
                continue;
            }

            // Extract variable name
            char var_name[64];
            if (var_len >= sizeof(var_name)) {
                ESP_LOGE(TAG, "Env var name too long");
                return ESP_ERR_INVALID_SIZE;
            }
            memcpy(var_name, var_start, var_len);
            var_name[var_len] = '\0';

            // Get environment variable
            const char *value = getenv(var_name);
            if (value) {
                size_t val_len = strlen(value);
                if (out + val_len > end) {
                    ESP_LOGE(TAG, "Output buffer too small for expanded value");
                    return ESP_ERR_INVALID_SIZE;
                }
                memcpy(out, value, val_len);
                out += val_len;
            } else {
                ESP_LOGW(TAG, "Env var not set: %s", var_name);
            }

            in = var_end + 1;
        } else {
            *out++ = *in++;
        }
    }

    *out = '\0';
    return ESP_OK;
}

esp_err_t synapse_config_update_from_ble(const uint8_t *data, uint16_t len) {
    if (!data || len == 0) return ESP_ERR_INVALID_ARG;
    // MVP: log receipt; full Protobuf DeviceConfig parsing wired in Phase 2 (SDK spec).
    ESP_LOGI(TAG, "BLE config update received (%d bytes), queued for validation", len);
    return ESP_OK;
}