/**
 * @file ble_gatt_server.h
 * @brief Synapse Band v1 - BLE GATT Server Definitions
 *
 * Standard GATT Services:
 * - Heart Rate (0x180D) with HR Measurement (0x2A37) + Body Sensor Location (0x2A38)
 *   RR intervals are carried flag-gated inside 0x2A37 (never 0x2A38).
 * - Battery (0x180F) with Battery Level (0x2A19)
 * - Device Information (0x180A): serial number (0x2A25) and hardware revision
 *   (0x2A27) are registered so Averyn can bind device -> owner.
 *
 * Custom Synapse Service (128-bit, GATT v1 frozen):
 * - Feature Stream (notify, <=10 Hz): compressed features only, no raw samples.
 *   Frame: [version: u8][sequence: u16 LE][payload...], payload little-endian.
 * - Device Config (read/write): opaque config blob.
 * - Provisioning (write/notify): CSR flow, WiFi, Cloud URL.
 *
 * The research service (raw samples, separate UUID family) lives in
 * ble_research_service.c and is independent of this consumer service.
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
#define SYNAPSE_GATT_UUID_BATTERY_SERVICE             0x180F
#define SYNAPSE_GATT_UUID_DEVICE_INFO_SERVICE         0x180A

#define SYNAPSE_GATT_UUID_HR_MEASUREMENT              0x2A37
#define SYNAPSE_GATT_UUID_BODY_SENSOR_LOCATION        0x2A38
#define SYNAPSE_GATT_UUID_BATTERY_LEVEL               0x2A19
#define SYNAPSE_GATT_UUID_MANUFACTURER_NAME           0x2A29
#define SYNAPSE_GATT_UUID_MODEL_NUMBER                0x2A24
#define SYNAPSE_GATT_UUID_SERIAL_NUMBER               0x2A25
#define SYNAPSE_GATT_UUID_FIRMWARE_REVISION           0x2A26
#define SYNAPSE_GATT_UUID_HARDWARE_REVISION           0x2A27
#define SYNAPSE_GATT_UUID_SYSTEM_ID                   0x2A23

// Cadence services (RSC 0x1814 / CSC 0x1816) are intentionally NOT registered:
// Band v1 has no cadence/speed source. Keeping dead #defines here caused
// confusion; they live only in the header comment history. See ADR-0014 /
// docs/integrations/synapse.md for why GPS-derived speed is the phone's job.

// ============================================================================
// Custom Synapse Service UUID (128-bit)
// Generated with: uuidgen  (Fase 0 - freeze GATT v1, 27 Settembre 2026)
// ============================================================================
#define SYNAPSE_CUSTOM_SERVICE_UUID128 \
    {0xF1, 0x23, 0x81, 0x95, 0xF0, 0x1B, 0xA6, 0x97, \
     0x1F, 0x44, 0x34, 0x3D, 0xFD, 0x8F, 0xE0, 0xF9}  // f9e08ffd-3d34-441f-97a6-1bf0958123f1

// Custom Characteristic UUIDs (128-bit, derived from service UUID)
#define SYNAPSE_CHAR_FEATURE_STREAM_UUID128 \
    {0xE9, 0x68, 0x11, 0x92, 0xC3, 0x81, 0xE2, 0xA0, \
     0x52, 0x41, 0xB3, 0x6C, 0xE5, 0x07, 0x43, 0xDB}  // db4307e5-6cb3-4152-a0e2-81c3921168e9

#define SYNAPSE_CHAR_DEVICE_CONFIG_UUID128 \
    {0x2F, 0x33, 0x1F, 0xA5, 0x64, 0x79, 0x75, 0x8D, \
     0x9A, 0x47, 0x02, 0x0D, 0x47, 0xD6, 0x68, 0xBF}  // bf68d647-0d02-479a-8d75-7964a51f332f

#define SYNAPSE_CHAR_PROVISIONING_UUID128 \
    {0x4A, 0xB1, 0x3B, 0x5D, 0x3E, 0xA0, 0x61, 0xA1, \
     0x63, 0x4D, 0xAC, 0x94, 0xF1, 0x0C, 0x20, 0x4E}  // 4e200cf1-94ac-4d63-a161-a03e5d3bb14a

// ============================================================================
// Feature Stream Payload Format (packed, fits in MTU-3 bytes)
// ============================================================================
#define SYNAPSE_FEATURE_STREAM_VERSION  1
#define SYNAPSE_FEATURE_STREAM_MAX_HZ   10   // negotiated default 1 Hz, cap 10 Hz
#define SYNAPSE_STRESS_UNKNOWN          3    // model not run / no data yet

typedef struct __attribute__((packed)) {
    uint8_t  version;           // = SYNAPSE_FEATURE_STREAM_VERSION
    uint16_t sequence;          // Incremented per notification for reassembly (u16)
    uint16_t heart_rate;        // bpm, 0xFFFF = invalid
    uint16_t rr_interval_ms;    // ms, 0xFFFF = no RR available
    uint8_t  ppg_sqi;           // 0-255 (0.0-1.0 scaled)
    uint8_t  ppg_map;           // 0-255 (0.0-1.0 scaled)
    uint8_t  ecg_quality;       // 0-255 (0.0-1.0 scaled)
    uint8_t  stress_level;      // 0=baseline, 1=stress, 2=artifact, 3=unknown
    uint8_t  motion_level;      // 0-255 (0.0-1.0 scaled)
    uint8_t  battery_level;     // 0-100%
    uint8_t  charging;          // 0=no, 1=yes
    uint8_t  sport_type;        // 0=generic, 1=running, 2=cycling, 3=swimming, 4=sleep
    uint16_t cadence;           // rpm (running/cycling), 0xFFFF if unavailable
    int16_t  temperature;       // 0.01°C units, 0x7FFF = invalid
    uint16_t rmssd_ms;          // RMSSD over current window, ms (0xFFFF if invalid)
    uint16_t sdnn_ms;           // SDNN over current window, ms (0xFFFF if invalid)
} synapse_feature_stream_t;

#define SYNAPSE_FEATURE_STREAM_SIZE sizeof(synapse_feature_stream_t)
// Size = 26 bytes, fits in any negotiated MTU >= 29 (MTU-3). Default MTU 23
// still works: the sender must respect MTU-3 before notifying.

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
 * @brief Notify Heart Rate Measurement (0x2A37) with optional RR interval.
 * @param hr_bpm Heart rate in bpm (0xFFFF = invalid)
 * @param rr_interval_ms Latest RR interval in ms (0xFFFF = none)
 * @return ESP_OK on success
 */
esp_err_t synapse_ble_gatt_notify_hr(uint16_t hr_bpm, uint16_t rr_interval_ms);

/**
 * @brief Notify Battery Level (0x2A19) if subscribers exist.
 * @param level 0-100%
 * @param charging 0=no, 1=yes
 * @return ESP_OK on success
 */
esp_err_t synapse_ble_gatt_notify_battery(uint8_t level, bool charging);

/**
 * @brief Mark battery as changed and notify subscribers when connected.
 * Call from the power module on level change.
 */
void synapse_ble_gatt_battery_changed(uint8_t level, bool charging);

/**
 * @brief Set device info strings (called at init). Serial + HW rev are now
 * stored and exposed via Device Information service (0x2A25 / 0x2A27).
 */
void synapse_ble_gatt_set_device_info(const char *manufacturer, const char *model,
                                       const char *serial, const char *fw_rev,
                                       const char *hw_rev);

/**
 * @brief Build and notify a feature stream frame from raw inputs.
 * HR/RR are also pushed to the standard 0x2A37 characteristic.
 * Call from the feature/signal-quality module whenever a new frame is ready
 * (default 1 Hz, capped at SYNAPSE_FEATURE_STREAM_MAX_HZ).
 * @return ESP_OK on success
 */
esp_err_t synapse_ble_gatt_push_feature(uint16_t hr_bpm, uint16_t rr_interval_ms,
                                         uint8_t ppg_sqi, uint8_t ppg_map,
                                         uint8_t ecg_quality, uint8_t stress_level,
                                         uint8_t motion_level, int16_t temperature);

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