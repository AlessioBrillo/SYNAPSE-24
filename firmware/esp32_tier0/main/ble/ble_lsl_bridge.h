#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

// Bluetooth LE LSL Clock Synchronization UUIDs (128-bit base: 6E400001-B5A3-F393-E0A9-E50E24DCCA9E)
#define BLE_LSL_UUID_BASE "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_LSL_UUID_PPG  "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_LSL_UUID_ECG  "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_LSL_UUID_IMU  "6E400004-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_LSL_UUID_SYNC "6E400005-B5A3-F393-E0A9-E50E24DCCA9E"

// Maximum MTU for BLE 5 2M PHY (matches Architecture.md §92 sync budget)
#define BLE_LSL_MAX_MTU 247

#define BLE_LSL_NOTIFY_QUEUE_SIZE 16

// Clock sync protocol defines
// Architecture.md §92: Tier 0 sync residual drift ≤10 ms (achieved ≤8 ms in Phase 0)
// BLE timestamp exchange every 1s with linear drift model updated every 60s

// Sync characteristic data format sizes
// 12-byte request: [uint32 seq, int64 pod_send_us]
// 20-byte response: [uint32 seq, int64 hub_recv_us, int64 hub_send_us]
#define BLE_LSL_SYNC_REQUEST_SIZE (sizeof(uint32_t) + sizeof(int64_t))  // 12 bytes
#define BLE_LSL_SYNC_RESPONSE_SIZE (sizeof(uint32_t) + 2 * sizeof(int64_t))  // 20 bytes

// Maximum number of sync exchange entries stored in BLE bridge
#define BLE_LSL_SYNC_HISTORY_MAX 120  // 2 minutes at 1Hz exchange rate

// Sync exchange entry stored in BLE bridge history
typedef struct {
    uint32_t sequence;          // Sync sequence number
    int64_t pod_send_us;        // Pod timestamp when send request was initiated
    int64_t hub_recv_us;        // Hub timestamp when request was received
    int64_t hub_send_us;        // Hub timestamp when response was sent
    int64_t pod_recv_us;        // Pod timestamp when response was received
    int64_t offset_us;          // Estimated clock offset (us)
    float drift_ppm;            // Estimated drift (parts per million)
} ble_lsl_sync_entry_t;

// Ble LSL character defines
typedef enum {
    BLE_LSL_CHAR_PPG = 0,
    BLE_LSL_CHAR_ECG = 1,
    BLE_LSL_CHAR_IMU = 2,
    BLE_LSL_CHAR_SYNC = 3,
    BLE_LSL_CHAR_MAX = 4
} ble_lsl_char_t;

// Ble LSL notify item
typedef struct {
    ble_lsl_char_t type;
    sensor_sample_t sample;
} ble_lsl_notify_item_t;

// Ble LSL bridge state
typedef struct {
    uint16_t conn_handle;
    bool notifications_enabled[4];
    QueueHandle_t notify_queue;
    TaskHandle_t notify_task;
    bool running;
    bool conn_params_updated;
    // Clock sync exchange history
    ble_lsl_sync_entry_t sync_history[BLE_LSL_SYNC_HISTORY_MAX];
    uint8_t sync_head;
    uint8_t sync_count;
    bool sync_initialized;
} ble_lsl_bridge_t;

esp_err_t ble_lsl_bridge_init(ble_lsl_bridge_t* bridge, QueueHandle_t scheduler_queue);
esp_err_t ble_lsl_bridge_start(ble_lsl_bridge_t* bridge);
esp_err_t ble_lsl_bridge_stop(ble_lsl_bridge_t* bridge);
esp_err_t ble_lsl_bridge_deinit(ble_lsl_bridge_t* bridge);
esp_err_t ble_lsl_bridge_send_sample(ble_lsl_bridge_t* bridge, const sensor_sample_t* sample);
esp_err_t ble_lsl_bridge_send_sync_marker(ble_lsl_bridge_t* bridge, uint32_t sequence, int64_t hub_timestamp_us);
bool ble_lsl_bridge_is_connected(const ble_lsl_bridge_t* bridge);
int64_t ble_lsl_bridge_get_hub_clock(void);

// BLE connection parameters (Architecture.md §92: 7.5ms interval for 500Hz ECG)
#ifndef BLE_LSL_CONN_INTERVAL_MIN_MS
#define BLE_LSL_CONN_INTERVAL_MIN_MS 7.5f
#endif
#ifndef BLE_LSL_CONN_INTERVAL_MAX_MS
#define BLE_LSL_CONN_INTERVAL_MAX_MS 15.0f
#endif
#ifndef BLE_LSL_CONN_LATENCY
#define BLE_LSL_CONN_LATENCY 0
#endif
#ifndef BLE_LSL_SUPERVISION_TIMEOUT_MS
#define BLE_LSL_SUPERVISION_TIMEOUT_MS 2000
#endif

#ifdef __cplusplus
}
#endif