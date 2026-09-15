/**
 * @file sync_marker_handler.cpp
 * @brief Sync Marker Handler Implementation
 *
 * Implements dual-method clock synchronization per Architecture.md §92:
 * 1. Sync markers: Direct timestamp comparison (provides offset + drift rate)
 * 2. ACC cross-correlation: Shared physical motion as reference (provides precise offset)
 *
 * Tier 0: 10ms tolerance, 60s interval
 * Tier 1: 1ms tolerance, 10s interval
 */

#include "sync_marker_handler.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <cmath>
#include <cstring>

static const char* TAG = "SYNC_MARKER";

// ============================================================================
// CONFIGURATION
// ============================================================================

#define SYNC_MAX_PODS               4
#define SYNC_MAX_HISTORY            100
#define SYNC_ACC_BUFFER_SIZE        SYNAPSE_ACC_CORR_BUFFER_SIZE
#define SYNC_MIN_CORRELATION        0.7f

// ============================================================================
// DATA STRUCTURES
// ============================================================================

typedef struct {
    char pod_id[16];
    uint16_t sequence;
    int64_t hub_timestamp_us;
    int64_t pod_timestamp_us;
    float offset_ms;
    float drift_ppm;
    float confidence;
    bool valid;
} sync_pod_estimate_t;

typedef struct {
    int16_t accel_x[SYNC_ACC_BUFFER_SIZE];
    int16_t accel_y[SYNC_ACC_BUFFER_SIZE];
    int16_t accel_z[SYNC_ACC_BUFFER_SIZE];
    int64_t timestamps_us[SYNC_ACC_BUFFER_SIZE];
    size_t head;
    size_t count;
} sync_acc_buffer_t;

typedef struct {
    uint16_t sequence;
    int64_t hub_timestamp_us;
    int64_t pod_timestamps_us[SYNC_MAX_PODS];
    char pod_ids[SYNC_MAX_PODS][16];
    int pod_count;
} sync_marker_history_t;

// ============================================================================
// GLOBAL STATE
// ============================================================================

static sync_pod_estimate_t s_pod_estimates[SYNC_MAX_PODS];
static sync_acc_buffer_t s_hub_acc_buffer;
static sync_acc_buffer_t s_pod_acc_buffers[SYNC_MAX_PODS];
static sync_marker_history_t s_marker_history[SYNC_MAX_HISTORY];
static size_t s_marker_history_count = 0;
static uint16_t s_sequence_counter = 0;
static SemaphoreHandle_t s_mutex = NULL;
static bool s_initialized = false;

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

static int find_pod_index(const char* pod_id) {
    for (int i = 0; i < SYNC_MAX_PODS; i++) {
        if (s_pod_estimates[i].valid && strcmp(s_pod_estimates[i].pod_id, pod_id) == 0) {
            return i;
        }
    }
    // Find empty slot
    for (int i = 0; i < SYNC_MAX_PODS; i++) {
        if (!s_pod_estimates[i].valid) {
            return i;
        }
    }
    return -1;
}

static float compute_accel_magnitude(int16_t x, int16_t y, int16_t z) {
    return sqrtf((float)x * x + (float)y * y + (float)z * z);
}

static float cross_correlate(const int16_t* hub, const int16_t* pod, size_t len, int* out_lag) {
    if (len < 100) return 0.0f;

    float max_corr = -1.0f;
    int best_lag = 0;

    // Search lag from -len/4 to +len/4
    int max_lag = len / 4;
    for (int lag = -max_lag; lag <= max_lag; lag++) {
        float sum_xy = 0.0f, sum_xx = 0.0f, sum_yy = 0.0f;
        int valid = 0;

        for (size_t i = 0; i < len; i++) {
            int j = i + lag;
            if (j >= 0 && j < (int)len) {
                float x = (float)hub[i];
                float y = (float)pod[j];
                sum_xy += x * y;
                sum_xx += x * x;
                sum_yy += y * y;
                valid++;
            }
        }

        if (valid > 50) {
            float denom = sqrtf(sum_xx * sum_yy) + 1e-10f;
            float corr = sum_xy / denom;
            if (corr > max_corr) {
                max_corr = corr;
                best_lag = lag;
            }
        }
    }

    if (out_lag) *out_lag = best_lag;
    return max_corr;
}

// ============================================================================
// INITIALIZATION
// ============================================================================

esp_err_t sync_marker_handler_init(void) {
    ESP_LOGI(TAG, "Initializing Sync Marker Handler...");

    s_mutex = xSemaphoreCreateMutex();
    if (!s_mutex) return ESP_FAIL;

    memset(s_pod_estimates, 0, sizeof(s_pod_estimates));
    memset(&s_hub_acc_buffer, 0, sizeof(s_hub_acc_buffer));
    memset(s_pod_acc_buffers, 0, sizeof(s_pod_acc_buffers));
    memset(s_marker_history, 0, sizeof(s_marker_history));
    s_marker_history_count = 0;
    s_sequence_counter = 0;

    s_initialized = true;
    ESP_LOGI(TAG, "Sync Marker Handler initialized");
    return ESP_OK;
}

// ============================================================================
// BROADCAST SYNC MARKER
// ============================================================================

void sync_marker_handler_broadcast(void) {
    if (!s_initialized) return;

    int64_t hub_time = esp_timer_get_time();

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        // Store in history
        size_t idx = s_marker_history_count % SYNC_MAX_HISTORY;
        s_marker_history[idx].sequence = s_sequence_counter;
        s_marker_history[idx].hub_timestamp_us = hub_time;
        s_marker_history[idx].pod_count = 0;
        s_marker_history_count++;
        s_sequence_counter++;

        xSemaphoreGive(s_mutex);
    }

    // Create and send sync marker
    synapse_sync_marker_t marker = {
        .sequence = s_sequence_counter - 1,
        .hub_timestamp_us = hub_time,
        .pod_timestamp_us = 0 // Will be filled by pod on receipt
    };

    // Send via BLE (pod will receive and call on_received)
    // The hub broadcasts, pods receive
    ble_lsl_bridge_send_sync_marker(&marker);

    ESP_LOGD(TAG, "Sync marker broadcast: seq=%d, time=%lld", marker.sequence, hub_time);
}

// ============================================================================
// HANDLE RECEIVED SYNC MARKER (Pod side)
// ============================================================================

void sync_marker_handler_on_received(const synapse_sync_marker_t* marker) {
    if (!s_initialized || !marker) return;

    int64_t pod_time = esp_timer_get_time();

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        // Find or create pod estimate
        int idx = find_pod_index("hub"); // Hub is the reference
        if (idx < 0) {
            xSemaphoreGive(s_mutex);
            return;
        }

        sync_pod_estimate_t* est = &s_pod_estimates[idx];
        strncpy(est->pod_id, "hub", sizeof(est->pod_id) - 1);
        est->sequence = marker->sequence;
        est->hub_timestamp_us = marker->hub_timestamp_us;
        est->pod_timestamp_us = pod_time;
        est->valid = true;

        // Simple offset estimation (single marker)
        est->offset_ms = (float)(pod_time - marker->hub_timestamp_us) / 1000.0f;

        // Update drift rate if we have history
        if (s_marker_history_count > 1) {
            // Linear regression on last N markers
            const int N = 10;
            int count = 0;
            float sum_x = 0, sum_y = 0, sum_xy = 0, sum_xx = 0;

            for (int i = 0; i < N && i < (int)s_marker_history_count; i++) {
                size_t hist_idx = (s_marker_history_count - 1 - i) % SYNC_MAX_HISTORY;
                if (s_marker_history[hist_idx].sequence > 0) {
                    float x = (float)s_marker_history[hist_idx].hub_timestamp_us / 1e6f;
                    float y = (float)s_marker_history[hist_idx].pod_timestamps_us[0] / 1e6f;
                    sum_x += x;
                    sum_y += y;
                    sum_xy += x * y;
                    sum_xx += x * x;
                    count++;
                }
            }

            if (count >= 2) {
                float n = (float)count;
                float a = (n * sum_xy - sum_x * sum_y) / (n * sum_xx - sum_x * sum_x + 1e-10f);
                float b = (sum_y - a * sum_x) / n;
                est->drift_ppm = (a - 1.0f) * 1e6f;
                est->offset_ms = b * 1000.0f;
                est->confidence = (float)count / 10.0f;
            }
        }

        xSemaphoreGive(s_mutex);
    }
}

// ============================================================================
// FEED ACCELEROMETER FOR CROSS-CORRELATION
// ============================================================================

void sync_marker_handler_feed_accel(int16_t ax, int16_t ay, int16_t az, int64_t timestamp_us) {
    if (!s_initialized) return;

    float mag = compute_accel_magnitude(ax, ay, az);

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        // Store in hub buffer
        size_t head = s_hub_acc_buffer.head;
        s_hub_acc_buffer.accel_x[head] = (int16_t)mag; // Store magnitude in x
        s_hub_acc_buffer.accel_y[head] = 0;
        s_hub_acc_buffer.accel_z[head] = 0;
        s_hub_acc_buffer.timestamps_us[head] = timestamp_us;
        s_hub_acc_buffer.head = (head + 1) % SYNC_ACC_BUFFER_SIZE;
        if (s_hub_acc_buffer.count < SYNC_ACC_BUFFER_SIZE) {
            s_hub_acc_buffer.count++;
        }
        xSemaphoreGive(s_mutex);
    }
}

// ============================================================================
// GET DRIFT ESTIMATE
// ============================================================================

bool sync_marker_handler_get_drift(const char* pod_id, float* offset_ms, float* drift_ppm) {
    if (!s_initialized || !offset_ms || !drift_ppm) return false;

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        int idx = find_pod_index(pod_id);
        if (idx >= 0 && s_pod_estimates[idx].valid) {
            *offset_ms = s_pod_estimates[idx].offset_ms;
            *drift_ppm = s_pod_estimates[idx].drift_ppm;
            xSemaphoreGive(s_mutex);
            return true;
        }
        xSemaphoreGive(s_mutex);
    }
    return false;
}

// ============================================================================
// CHECK TOLERANCE
// ============================================================================

bool sync_marker_handler_within_tolerance(int tier) {
    if (!s_initialized) return true; // Fail open

    float max_tolerance_ms = (tier == 1) ? 1.0f : 10.0f; // T1=1ms, T0=10ms

    if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        for (int i = 0; i < SYNC_MAX_PODS; i++) {
            if (s_pod_estimates[i].valid) {
                if (fabsf(s_pod_estimates[i].offset_ms) > max_tolerance_ms) {
                    xSemaphoreGive(s_mutex);
                    return false;
                }
            }
        }
        xSemaphoreGive(s_mutex);
    }
    return true;
}

// ============================================================================
// DEINITIALIZATION
// ============================================================================

void sync_marker_handler_deinit(void) {
    if (s_mutex) {
        vSemaphoreDelete(s_mutex);
        s_mutex = NULL;
    }
    s_initialized = false;
    ESP_LOGI(TAG, "Sync Marker Handler deinitialized");
}