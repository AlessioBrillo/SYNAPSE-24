#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BLE_LSL_UUID_BASE "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_LSL_UUID_PPG  "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_LSL_UUID_ECG  "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_LSL_UUID_IMU  "6E400004-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_LSL_UUID_SYNC "6E400005-B5A3-F393-E0A9-E50E24DCCA9E"

#define BLE_LSL_MAX_MTU 247
#define BLE_LSL_NOTIFY_QUEUE_SIZE 16

// Connection parameters for 7.5ms interval (per Architecture.md §92 sync budget)
#define BLE_LSL_CONN_INTERVAL_MIN_MS 7.5f
#define BLE_LSL_CONN_INTERVAL_MAX_MS 7.5f
#define BLE_LSL_CONN_LATENCY 0
#define BLE_LSL_SUPERVISION_TIMEOUT_MS 4000

typedef struct {
    uint16_t conn_handle;
    bool notifications_enabled[4];
    QueueHandle_t notify_queue;
    TaskHandle_t notify_task;
    bool running;
    bool conn_params_updated;
} ble_lsl_bridge_t;

typedef enum {
    BLE_LSL_CHAR_PPG = 0,
    BLE_LSL_CHAR_ECG = 1,
    BLE_LSL_CHAR_IMU = 2,
    BLE_LSL_CHAR_SYNC = 3,
    BLE_LSL_CHAR_MAX = 4
} ble_lsl_char_t;

typedef struct {
    ble_lsl_char_t type;
    sensor_sample_t sample;
} ble_lsl_notify_item_t;

esp_err_t ble_lsl_bridge_init(ble_lsl_bridge_t* bridge, QueueHandle_t scheduler_queue);
esp_err_t ble_lsl_bridge_start(ble_lsl_bridge_t* bridge);
esp_err_t ble_lsl_bridge_stop(ble_lsl_bridge_t* bridge);
esp_err_t ble_lsl_bridge_deinit(ble_lsl_bridge_t* bridge);
esp_err_t ble_lsl_bridge_send_sample(ble_lsl_bridge_t* bridge, const sensor_sample_t* sample);
esp_err_t ble_lsl_bridge_send_sync_marker(ble_lsl_bridge_t* bridge, uint32_t sequence, int64_t hub_timestamp_us);
bool ble_lsl_bridge_is_connected(const ble_lsl_bridge_t* bridge);

#ifdef __cplusplus
}
#endif