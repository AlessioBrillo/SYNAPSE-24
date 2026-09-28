/**
 * @file ble_gatt_server.h
 * @brief Synapse Band v1 - BLE GATT Server Definitions
 * 
 * Standard GATT Services:
 * - Heart Rate (0x180D) with HR Measurement (0x2A37) + Body Sensor Location (0x2A38)
 * - Running Speed & Cadence (0x1814) with RSC Measurement (0x2A53)
 * - Cycling Speed & Cadence (0x1816) with CSC Measurement (0x2A5B)
 * - Battery (0x180F) with Battery Level (0x2A19)
 * - Device Information (0x180A)
 * 
 * Custom Synapse Service: 53594E41-5053-452D-4241-4E44-xxxxxxxxxxxx
 * - Feature Stream (notify): HR, RR intervals, SQI, Stress, Battery, Motion
 * - Device Config (read/write): Protobuf config
 * - Provisioning (write/notify): CSR flow, WiFi, Cloud URL
 */

#ifndef SYNAPSE_BLE_GATT_H
#define SYNAPSE_BLE_GATT_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// ============================================================================
// Standard GATT UUIDs (16-bit)
// ============================================================================
#define SYNAPSE_GATT_UUID_HEART_RATE_SERVICE          0x180D
#define SYNAPSE_GATT_UUID_RUNNING_SPEED_CADENCE       0x1814
#define SYNAPSE_GATT_UUID_CYCLING_SPEED_CADENCE       0x1816
#define SYNAPSE_GATT_UUID_BATTERY_SERVICE             0x180F
#define SYNAPSE_GATT_UUID_DEVICE_INFO_SERVICE         0x180A

#define SYNAPSE_GATT_UUID_HR_MEASUREMENT              0x2A37
#define SYNAPSE_GATT_UUID_BODY_SENSOR_LOCATION        0x2A38
#define SYNAPSE_GATT_UUID_RSC_MEASUREMENT             0x2A53
#define SYNAPSE_GATT_UUID_SC_CONTROL_POINT            0x2A55
#define SYNAPSE_GATT_UUID_CSC_MEASUREMENT             0x2A5B
#define SYNAPSE_GATT_UUID_BATTERY_LEVEL               0x2A19
#define SYNAPSE_GATT_UUID_MANUFACTURER_NAME           0x2A29
#define SYNAPSE_GATT_UUID_MODEL_NUMBER                0x2A24
#define SYNAPSE_GATT_UUID_SERIAL_NUMBER               0x2A25
#define SYNAPSE_GATT_UUID_FIRMWARE_REVISION           0x2A26
#define SYNAPSE_GATT_UUID_HARDWARE_REVISION           0x2A27
#define SYNAPSE_GATT_UUID_SYSTEM_ID                   0x2A23

// ============================================================================
// Custom Synapse Service UUID (128-bit)
// Generated with: uuidgen
// ============================================================================
#define SYNAPSE_CUSTOM_SERVICE_UUID128 \
    {0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6, 0x4A, 0x7B, \
     0x8C, 0x9D, 0x0E, 0x1F, 0x2A, 0x3B, 0x4C, 0x5D}  // Little-endian

// Custom Characteristic UUIDs (128-bit, derived from service UUID)
#define SYNAPSE_CHAR_FEATURE_STREAM_UUID128 \
    {0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6, 0x4A, 0x7B, \
     0x8C, 0x9D, 0x0E, 0x1F, 0x2A, 0x3B, 0x4C, 0x5E}

#define SYNAPSE_CHAR_DEVICE_CONFIG_UUID128 \
    {0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6, 0x4A, 0x7B, \
     0x8C, 0x9D, 0x0E, 0x1F, 0x2A, 0x3B, 0x4C, 0x5F}

#define SYNAPSE_CHAR_PROVISIONING_UUID128 \
    {0xA1, 0xB2, 0xC3, 0xD4, 0xE5, 0xF6, 0x4A, 0x7B, \
     0x8C, 0x9D, 0x0E, 0x1F, 0x2A, 0x3B, 0x4C, 0x60}

// ============================================================================
// Feature Stream Payload Format (packed, fits in MTU-3 bytes)
// ============================================================================
#define SYNAPSE_FEATURE_STREAM_VERSION  1

typedef struct __attribute__((packed)) {
    uint8_t  version;           // = SYNAPSE_FEATURE_STREAM_VERSION
    uint8_t  sequence;          // Incremented per notification for reassembly
    uint16_t heart_rate;        // bpm, 0xFFFF = invalid
    uint16_t rr_interval_ms;    // ms, 0xFFFF = no RR available
    uint8_t  ppg_sqi;           // 0-255 (0.0-1.0 scaled)
    uint8_t  ppg_map;           // 0-255 (0.0-1.0 scaled)
    uint8_t  ecg_quality;       // 0-255 (0.0-1.0 scaled)
    uint8_t  stress_level;      // 0=baseline, 1=stress, 2=artifact
    uint8_t  motion_level;      // 0-255 (0.0-1.0 scaled)
    uint8_t  battery_level;     // 0-100%
    uint8_t  charging;          // 0=no, 1=yes
    uint8_t  sport_type;        // 0=generic, 1=running, 2=cycling, 3=swimming, 4=sleep
    uint16_t cadence;           // rpm (running/cycling)
    int16_t  temperature;       // 0.01°C units
} synapse_feature_stream_t;

#define SYNAPSE_FEATURE_STREAM_SIZE sizeof(synapse_feature_stream_t)
// Size = 20 bytes, fits easily in any negotiated MTU

// ============================================================================
// Device Config (Protobuf encoded, read/write)
// ============================================================================
#define SYNAPSE_MAX_CONFIG_SIZE 512

typedef struct {
    uint8_t data[SYNAPSE_MAX_CONFIG_SIZE];
    uint16_t length;
} synapse_device_config_t;

// ============================================================================
// Provisioning Protocol
// ============================================================================
typedef enum {
    SYNAPSE_PROV_CMD_NONE = 0,
    SYNAPSE_PROV_CMD_WIFI_CREDENTIALS = 1,    // Write: SSID + PASS (plaintext over encrypted BLE)
    SYNAPSE_PROV_CMD_CLOUD_URL = 2,           // Write: HTTPS URL
    SYNAPSE_PROV_CMD_REQUEST_CSR = 3,         // Write: trigger CSR generation
    SYNAPSE_PROV_CMD_CSR_RESPONSE = 4,        // Notify: CSR PEM data
    SYNAPSE_PROV_CMD_CERT_INSTALL = 5,        // Write: signed cert + CA chain
    SYNAPSE_PROV_CMD_COMMIT = 6,              // Write: commit provisioning
    SYNAPSE_PROV_CMD_FACTORY_RESET = 7,       // Write: erase all config
} synapse_prov_cmd_t;

typedef enum {
    SYNAPSE_PROV_STATUS_IDLE = 0,
    SYNAPSE_PROV_STATUS_WIFI_SET = 1,
    SYNAPSE_PROV_STATUS_CLOUD_SET = 2,
    SYNAPSE_PROV_STATUS_CSR_GENERATED = 3,
    SYNAPSE_PROV_STATUS_CERT_INSTALLED = 4,
    SYNAPSE_PROV_STATUS_COMMITTED = 5,
    SYNAPSE_PROV_STATUS_FAILED = 0xFF,
} synapse_prov_status_t;

typedef struct __attribute__((packed)) {
    uint8_t cmd;
    uint8_t status;
    uint16_t data_len;
    uint8_t data[240];  // Max payload for MTU ~247
} synapse_prov_packet_t;

// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize BLE GATT Server
 * Registers all standard services + custom Synapse service
 * @return ESP_OK on success
 */
esp_err_t synapse_ble_gatt_init(void);

/**
 * @brief Start advertising in provisioning mode
 * Uses custom service UUID in advertising data
 */
void synapse_ble_gatt_start_advertising_provisioning(void);

/**
 * @brief Start advertising in normal mode
 * Uses standard HR service UUID in advertising data
 */
void synapse_ble_gatt_start_advertising_normal(void);

/**
 * @brief Process BLE events (call periodically from main loop)
 */
void synapse_ble_gatt_process_events(void);

/**
 * @brief Send feature stream notification
 * @param feature Pointer to feature data
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if not connected/not subscribed
 */
esp_err_t synapse_ble_gatt_notify_features(const synapse_feature_stream_t *feature);

/**
 * @brief Send provisioning notification
 * @param packet Provisioning packet to send
 * @return ESP_OK on success
 */
esp_err_t synapse_ble_gatt_notify_provisioning(const synapse_prov_packet_t *packet);

/**
 * @brief Get current connection status
 * @return true if connected and subscribed to feature stream
 */
bool synapse_ble_gatt_is_connected(void);

/**
 * @brief Get negotiated MTU size
 * @return MTU in bytes (default 23, up to 517)
 */
uint16_t synapse_ble_gatt_get_mtu(void);

/**
 * @brief Set device info strings (called at init)
 */
void synapse_ble_gatt_set_device_info(const char *manufacturer, const char *model,
                                       const char *serial, const char *fw_rev,
                                       const char *hw_rev);

/**
 * @brief Update battery level (triggers notification if subscribed)
 */
void synapse_ble_gatt_update_battery(uint8_t level, bool charging);

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_BLE_GATT_H