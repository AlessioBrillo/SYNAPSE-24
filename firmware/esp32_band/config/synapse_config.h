/**
 * @file synapse_config.h
 * @brief Synapse Band v1 - Hardware Configuration Manager
 * 
 * NVS-backed configuration with defaults from Kconfig and hardware.yaml.
 * Supports environment variable expansion for BLE MACs and serial ports.
 */

#ifndef SYNAPSE_CONFIG_H
#define SYNAPSE_CONFIG_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Configuration namespace in NVS
#define SYNAPSE_CONFIG_NAMESPACE "synapse_cfg"
#define SYNAPSE_CONFIG_KEY "hw_config"
#define SYNAPSE_CONFIG_VERSION_KEY "cfg_version"

#define SYNAPSE_CONFIG_VERSION 1

// Firmware/Hardware version strings (from Kconfig.projbuild)
#define SYNAPSE_FW_VERSION CONFIG_SYNAPSE_FIRMWARE_VERSION
#define SYNAPSE_HW_VERSION CONFIG_SYNAPSE_HARDWARE_VERSION

// Hardware configuration struct
// Mirrors config/hardware.yaml forearm_hub section
typedef struct {
    // ========================================================================
    // ECG AD8232
    // ========================================================================
    int ecg_adc_channel;          // ADC1 channel (ADC1_CHANNEL_0 = 0 -> GPIO36)
    int ecg_gpio_drdy;            // GPIO for DRDY/lead-off (-1 = disabled)
    float ecg_vref_mv;            // ADC reference voltage in mV
    float ecg_gain;               // AD8232 gain (e.g., 6.0)

    // ========================================================================
    // PPG MAX30102
    // ========================================================================
    int ppg_i2c_port;             // I2C_NUM_0 or I2C_NUM_1
    int ppg_i2c_addr;             // I2C address (0x57)
    int ppg_gpio_int;             // GPIO for INT pin
    uint8_t ppg_led_current_red;  // Red LED current register (0x00-0x3F)
    uint8_t ppg_led_current_ir;   // IR LED current register (0x00-0x3F)
    uint8_t ppg_led_current_green;// Green LED current register (0x00-0x3F)
    uint8_t ppg_sample_rate;      // Sample rate register (0x02=50Hz, 0x03=64Hz)
    uint8_t ppg_pulse_width;      // Pulse width register (0x03=411us)
    uint8_t ppg_adc_range;        // ADC range register (0x03=16384nA)

    // ========================================================================
    // IMU ICM-20948
    // ========================================================================
    int imu_i2c_port;             // I2C_NUM_0 or I2C_NUM_1
    int imu_i2c_addr;             // I2C address (0x68)
    int imu_gpio_int;             // GPIO for INT pin
    int imu_accel_fsr_g;          // Accel FSR: 2, 4, 8, 16g
    int imu_gyro_fsr_dps;         // Gyro FSR: 250, 500, 1000, 2000 dps
    int imu_accel_odr_hz;         // Accel ODR in Hz
    int imu_gyro_odr_hz;          // Gyro ODR in Hz

    // ========================================================================
    // GPS MAX-M10S
    // ========================================================================
    int gps_uart_port;            // UART_NUM_0, UART_NUM_1, or UART_NUM_2
    int gps_uart_tx_gpio;         // TX GPIO
    int gps_uart_rx_gpio;         // RX GPIO
    int gps_baudrate;             // Baudrate (115200 for UBX)
    bool gps_use_ubx;             // true = UBX binary, false = NMEA

    // ========================================================================
    // Temperature TMP117
    // ========================================================================
    int temp_i2c_port;            // I2C_NUM_0 or I2C_NUM_1 (shared)
    int temp_i2c_addr;            // I2C address (0x48)
    int temp_gpio_alert;          // GPIO for ALERT pin (-1 = disabled)

    // ========================================================================
    // Battery ADC
    // ========================================================================
    int bat_adc_channel;          // ADC1 channel (ADC1_CHANNEL_3 = 3 -> GPIO3)
    float bat_voltage_divider;    // Voltage divider ratio (e.g., 2.0 for 1:1)

    // ========================================================================
    // BLE
    // ========================================================================
    char ble_device_name[32];     // GAP device name

    // ========================================================================
    // Power Budget (from hardware.yaml power_budget section)
    // ========================================================================
    float hub_battery_mah;        // Battery capacity in mAh
    float target_lifetime_h;      // Target lifetime in hours
    float reserve_mah;            // Reserve capacity in mAh
    float t0_avg_mw;              // Tier 0 average power (mW)
    float t1_avg_mw;              // Tier 1 average power (mW)
    float t2_avg_mw;              // Tier 2 average power (mW)

    // ========================================================================
    // Motion Gate Thresholds (from hardware.yaml motion_gate section)
    // ========================================================================
    float sqi_min;                // Minimum PPG SQI (0.0-1.0)
    float map_max;                // Maximum Motion Artifact Probability (0.0-1.0)
    int required_consecutive_clean; // Consecutive clean windows to arm gate
} synapse_hw_config_t;


// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Load configuration from NVS, falling back to Kconfig defaults
 * @return ESP_OK on success, ESP_ERR_NOT_FOUND if no config in NVS (defaults used)
 */
esp_err_t synapse_config_load(void);

/**
 * @brief Save current configuration to NVS
 * @return ESP_OK on success
 */
esp_err_t synapse_config_save(void);

/**
 * @brief Get current hardware configuration (read-only copy)
 * @param[out] out Pointer to struct to fill
 * @return ESP_OK on success
 */
esp_err_t synapse_config_get(synapse_hw_config_t *out);

/**
 * @brief Set hardware configuration (validates and marks dirty)
 * @param[in] in Pointer to new configuration
 * @return ESP_OK on success, ESP_ERR_INVALID_ARG on validation failure
 */
esp_err_t synapse_config_set(const synapse_hw_config_t *in);

/**
 * @brief Reset configuration to Kconfig defaults and save to NVS
 * @return ESP_OK on success
 */
esp_err_t synapse_config_reset_to_defaults(void);

/**
 * @brief Get firmware version string
 * @return Version string (e.g., "1.0.0")
 */
const char* synapse_config_get_fw_version(void);

/**
 * @brief Get hardware version string
 * @return Version string (e.g., "band-v1.0")
 */
const char* synapse_config_get_hw_version(void);

/**
 * @brief Check if configuration has been modified since last save
 * @return true if dirty
 */
bool synapse_config_is_dirty(void);

/**
 * @brief Mark configuration as clean (after successful save)
 */
void synapse_config_mark_clean(void);

/**
 * @brief Expand environment variables in a string
 * Supports ${VAR_NAME} syntax. Used for BLE MACs, serial ports.
 * @param[in] input Input string with potential ${VAR} patterns
 * @param[out] output Output buffer
 * @param[in] output_size Size of output buffer
 * @return ESP_OK on success, ESP_ERR_INVALID_SIZE if output too small
 */
esp_err_t synapse_config_expand_env(const char *input, char *output, size_t output_size);

/**
 * @brief Update config from BLE Device Config characteristic
 * @param data Protobuf-encoded config payload
 * @param len Payload length
 * @return ESP_OK on success
 */
esp_err_t synapse_config_update_from_ble(const uint8_t *data, uint16_t len);

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_CONFIG_H