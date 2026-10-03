/**
 * @file gps_max_m10s.h
 * @brief Synapse Band v1 - GPS MAX-M10S Driver (UBX Protocol)
 * 
 * UBX binary protocol parser for u-blox MAX-M10S.
 * Provides PVT (Position Velocity Time) solution at 1-10Hz.
 */

#ifndef GPS_MAX_M10S_H
#define GPS_MAX_M10S_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "synapse_sensor_types.h"

#ifdef __cplusplus
extern "C" {
#endif

// UBX Protocol Constants
#define UBX_SYNC_CHAR_1 0xB5
#define UBX_SYNC_CHAR_2 0x62

// UBX Message Classes
#define UBX_CLASS_NAV 0x01
#define UBX_CLASS_CFG 0x06
#define UBX_CLASS_MON 0x0A

// UBX Message IDs (NAV class)
#define UBX_NAV_PVT 0x07        // Position Velocity Time Solution
#define UBX_NAV_STATUS 0x03     // Receiver Navigation Status
#define UBX_NAV_SAT 0x35        // Satellite Information
#define UBX_NAV_SIG 0x43        // Signal Information

// UBX Message IDs (CFG class)
#define UBX_CFG_PRT 0x00        // Port Configuration
#define UBX_CFG_RATE 0x08       // Navigation/Measurement Rate
#define UBX_CFG_NAV5 0x24       // Navigation Engine Settings
#define UBX_CFG_MSG 0x01        // Message Rate Configuration

// Maximum UBX payload size
#define UBX_MAX_PAYLOAD 256

// GPS parser state
typedef enum {
    UBX_STATE_SYNC1 = 0,
    UBX_STATE_SYNC2,
    UBX_STATE_CLASS,
    UBX_STATE_ID,
    UBX_STATE_LENGTH_L,
    UBX_STATE_LENGTH_H,
    UBX_STATE_PAYLOAD,
    UBX_STATE_CK_A,
    UBX_STATE_CK_B
} ubx_parse_state_t;

typedef struct {
    gps_max_m10s_config_t config;
    QueueHandle_t uart_queue;
    TaskHandle_t parser_task;
    bool initialized;
    bool running;
    
    // Parser state
    ubx_parse_state_t parse_state;
    uint8_t msg_class;
    uint8_t msg_id;
    uint16_t payload_length;
    uint16_t payload_index;
    uint8_t payload[UBX_MAX_PAYLOAD];
    uint8_t ck_a, ck_b;
    
    // Latest parsed data
    gps_data_t latest_data;
    SemaphoreHandle_t data_mutex;
    
    // Stats
    uint32_t messages_parsed;
    uint32_t crc_errors;
    uint32_t sync_errors;
} gps_max_m10s_t;


// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize GPS MAX-M10S driver
 * Configures UART, starts parser task.
 * @param config GPS configuration
 * @return ESP_OK on success
 */
esp_err_t gps_max_m10s_init(const gps_max_m10s_config_t *config);

/**
 * @brief Start GPS (begin parsing)
 * @return ESP_OK on success
 */
esp_err_t gps_max_m10s_start(void);

/**
 * @brief Stop GPS
 * @return ESP_OK on success
 */
esp_err_t gps_max_m10s_stop(void);

/**
 * @brief Deinitialize GPS driver
 * @return ESP_OK on success
 */
esp_err_t gps_max_m10s_deinit(void);

/**
 * @brief Get latest GPS data (thread-safe copy)
 * @param[out] data Pointer to GPS data struct
 * @return ESP_OK on success, ESP_ERR_INVALID_STATE if no fix yet
 */
esp_err_t gps_max_m10s_get_latest(gps_data_t *data);

/**
 * @brief Check if GPS has valid fix
 * @return true if valid 3D fix
 */
bool gps_max_m10s_has_fix(void);

/**
 * @brief Configure GPS message rates (UBX-CFG-MSG)
 * Enable/disable specific UBX messages.
 * @param msg_class Message class
 * @param msg_id Message ID
 * @param rate Rate (0=disable, 1=every nav solution)
 * @return ESP_OK on success
 */
esp_err_t gps_max_m10s_set_message_rate(uint8_t msg_class, uint8_t msg_id, uint8_t rate);

/**
 * @brief Set GPS navigation rate (UBX-CFG-RATE)
 * @param meas_rate_ms Measurement rate in ms (e.g., 1000 = 1Hz)
 * @param nav_rate Navigation rate (cycles between nav solutions)
 * @param time_ref Time reference (0=UTC, 1=GPS)
 * @return ESP_OK on success
 */
esp_err_t gps_max_m10s_set_nav_rate(uint16_t meas_rate_ms, uint16_t nav_rate, uint16_t time_ref);

/**
 * @brief Send UBX command and wait for ACK
 * @param msg_class Message class
 * @param msg_id Message ID
 * @param payload Command payload
 * @param payload_len Payload length
 * @param timeout_ms Timeout for ACK
 * @return ESP_OK on success
 */
esp_err_t gps_max_m10s_send_command(uint8_t msg_class, uint8_t msg_id, 
                                     const uint8_t *payload, uint16_t payload_len,
                                     uint32_t timeout_ms);

/**
 * @brief Get parser statistics
 * @param[out] parsed Messages parsed
 * @param[out] crc_errors CRC errors
 * @param[out] sync_errors Sync errors
 * @return ESP_OK on success
 */
esp_err_t gps_max_m10s_get_stats(uint32_t *parsed, uint32_t *crc_errors, uint32_t *sync_errors);

#ifdef __cplusplus
}
#endif

#endif // GPS_MAX_M10S_H