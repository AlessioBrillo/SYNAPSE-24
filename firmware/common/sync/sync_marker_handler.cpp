#include "sync_marker_handler.h"
#include "esp_log.h"
#include "esp_timer.h"
#include <string.h>
#include <math.h>

static const char* TAG = "sync_marker";

#define MIN_MARKERS_FOR_DRIFT 3
#define MAX_DRIFT_PPM 1000.0f

esp_err_t sync_marker_handler_init(sync_marker_handler_t* handler) {
    if (!handler) return ESP_ERR_INVALID_ARG;

    memset(handler, 0, sizeof(sync_marker_handler_t));
    handler->initialized = true;
    ESP_LOGI(TAG, "Sync marker handler initialized");
    return ESP_OK;
}

esp_err_t sync_marker_handler_on_marker_received(sync_marker_handler_t* handler, uint32_t sequence, int64_t hub_timestamp_us, sync_marker_cb_t callback, void* user_ctx) {
    if (!handler || !handler->initialized) return ESP_ERR_INVALID_STATE;

    int64_t pod_timestamp_us = esp_timer_get_time();

    if (handler->count > 0 && sequence <= handler->last_sequence) {
        ESP_LOGW(TAG, "Out-of-order sync marker: seq=%" PRIu32 " (last=%" PRIu32 ")", sequence, handler->last_sequence);
        return ESP_ERR_INVALID_ARG;
    }

    sync_marker_entry_t entry = {
        .sequence = sequence,
        .hub_timestamp_us = hub_timestamp_us,
        .pod_timestamp_us = pod_timestamp_us,
        .offset_us = pod_timestamp_us - hub_timestamp_us,
        .drift_ppm = 0.0f
    };

    if (handler->count >= MIN_MARKERS_FOR_DRIFT && handler->last_sequence > 0) {
        int64_t hub_delta = hub_timestamp_us - handler->last_hub_ts;
        int64_t pod_delta = pod_timestamp_us - handler->last_pod_ts;
        if (hub_delta > 0) {
            entry.drift_ppm = ((float)(pod_delta - hub_delta) / (float)hub_delta) * 1000000.0f;
        }
    }

    handler->history[handler->head] = entry;
    handler->head = (handler->head + 1) % SYNC_MARKER_MAX_HISTORY;
    if (handler->count < SYNC_MARKER_MAX_HISTORY) handler->count++;

    handler->last_sequence = sequence;
    handler->last_hub_ts = hub_timestamp_us;
    handler->last_pod_ts = pod_timestamp_us;

    if (callback) {
        callback(sequence, hub_timestamp_us, pod_timestamp_us, user_ctx);
    }

    if (handler->count >= MIN_MARKERS_FOR_DRIFT) {
        sync_marker_handler_estimate_drift(handler, &handler->estimated_drift_ppm, &handler->estimated_offset_us);
    }

    ESP_LOGD(TAG, "Sync marker: seq=%" PRIu32 ", offset=%" PRId64 " us, drift=%.2f ppm", sequence, entry.offset_us, entry.drift_ppm);
    return ESP_OK;
}

esp_err_t sync_marker_handler_estimate_drift(sync_marker_handler_t* handler, float* drift_ppm, int64_t* offset_us) {
    if (!handler || !handler->initialized || handler->count < MIN_MARKERS_FOR_DRIFT) {
        return ESP_ERR_INVALID_STATE;
    }

    int n = handler->count;
    int start = (handler->head + SYNC_MARKER_MAX_HISTORY - n) % SYNC_MARKER_MAX_HISTORY;

    double sum_x = 0, sum_y = 0, sum_xy = 0, sum_x2 = 0;
    int valid_count = 0;

    for (int i = 0; i < n; i++) {
        int idx = (start + i) % SYNC_MARKER_MAX_HISTORY;
        const sync_marker_entry_t* e = &handler->history[idx];

        if (fabsf(e->drift_ppm) < MAX_DRIFT_PPM) {
            double x = (double)e->hub_timestamp_us / 1e6;
            double y = (double)e->offset_us;
            sum_x += x;
            sum_y += y;
            sum_xy += x * y;
            sum_x2 += x * x;
            valid_count++;
        }
    }

    if (valid_count < 2) {
        if (drift_ppm) *drift_ppm = 0.0f;
        if (offset_us) *offset_us = handler->history[(handler->head + SYNC_MARKER_MAX_HISTORY - 1) % SYNC_MARKER_MAX_HISTORY].offset_us;
        return ESP_OK;
    }

    double denom = valid_count * sum_x2 - sum_x * sum_x;
    double drift_rate = 0.0;
    double offset = 0.0;

    if (fabs(denom) > 1e-10) {
        drift_rate = (valid_count * sum_xy - sum_x * sum_y) / denom;
        offset = (sum_y - drift_rate * sum_x) / valid_count;
    } else {
        offset = sum_y / valid_count;
    }

    if (drift_ppm) *drift_ppm = (float)(drift_rate * 1e6);
    if (offset_us) *offset_us = (int64_t)offset;

    return ESP_OK;
}

esp_err_t sync_marker_handler_correct_timestamp(const sync_marker_handler_t* handler, int64_t raw_pod_timestamp_us, int64_t* corrected_timestamp_us) {
    if (!handler || !handler->initialized || !corrected_timestamp_us) return ESP_ERR_INVALID_ARG;

    if (handler->count < MIN_MARKERS_FOR_DRIFT) {
        *corrected_timestamp_us = raw_pod_timestamp_us - handler->estimated_offset_us;
        return ESP_OK;
    }

    double drift_rate = handler->estimated_drift_ppm / 1e6;
    double offset = (double)handler->estimated_offset_us;
    double raw_s = raw_pod_timestamp_us / 1e6;

    double corrected_s = (raw_s - offset / 1e6) / (1.0 + drift_rate);
    *corrected_timestamp_us = (int64_t)(corrected_s * 1e6);

    return ESP_OK;
}

esp_err_t sync_marker_handler_get_stats(const sync_marker_handler_t* handler, uint32_t* markers_received, float* drift_ppm, int64_t* offset_us) {
    if (!handler) return ESP_ERR_INVALID_ARG;
    if (markers_received) *markers_received = handler->count;
    if (drift_ppm) *drift_ppm = handler->estimated_drift_ppm;
    if (offset_us) *offset_us = handler->estimated_offset_us;
    return ESP_OK;
}

esp_err_t sync_marker_handler_reset(sync_marker_handler_t* handler) {
    if (!handler) return ESP_ERR_INVALID_ARG;

    handler->count = 0;
    handler->head = 0;
    handler->last_hub_ts = 0;
    handler->last_pod_ts = 0;
    handler->last_sequence = 0;
    handler->estimated_drift_ppm = 0.0f;
    handler->estimated_offset_us = 0;
    ESP_LOGI(TAG, "Sync marker handler reset");
    return ESP_OK;
}