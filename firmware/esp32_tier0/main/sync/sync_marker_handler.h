#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#define SYNC_MARKER_MAX_HISTORY 64

typedef struct {
    uint32_t sequence;
    int64_t hub_timestamp_us;
    int64_t pod_timestamp_us;
    int64_t offset_us;
    float drift_ppm;
} sync_marker_entry_t;

typedef void (*sync_marker_cb_t)(uint32_t sequence, int64_t hub_timestamp_us, int64_t pod_timestamp_us, void* user_ctx);

typedef struct {
    sync_marker_entry_t history[SYNC_MARKER_MAX_HISTORY];
    uint8_t head;
    uint8_t count;
    uint32_t last_sequence;
    int64_t last_hub_ts;
    int64_t last_pod_ts;
    float estimated_drift_ppm;
    int64_t estimated_offset_us;
    bool initialized;
} sync_marker_handler_t;

esp_err_t sync_marker_handler_init(sync_marker_handler_t* handler);
esp_err_t sync_marker_handler_on_marker_received(sync_marker_handler_t* handler, uint32_t sequence, int64_t hub_timestamp_us, sync_marker_cb_t callback, void* user_ctx);
esp_err_t sync_marker_handler_estimate_drift(sync_marker_handler_t* handler, float* drift_ppm, int64_t* offset_us);
esp_err_t sync_marker_handler_correct_timestamp(const sync_marker_handler_t* handler, int64_t raw_pod_timestamp_us, int64_t* corrected_timestamp_us);
esp_err_t sync_marker_handler_get_stats(const sync_marker_handler_t* handler, uint32_t* markers_received, float* drift_ppm, int64_t* offset_us);
esp_err_t sync_marker_handler_reset(sync_marker_handler_t* handler);

#ifdef __cplusplus
}
#endif