#include "clock_sync.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_err.h"
#include <string.h>
#include <math.h>
#include <inttypes.h>

static const char* TAG = "clock_sync";

static esp_err_t clock_sync_update_drift_model(clock_sync_t* sync, int64_t ref_time_us);

esp_err_t clock_sync_init(clock_sync_t* sync) {
    if (!sync) return ESP_ERR_INVALID_ARG;
    
    memset(sync, 0, sizeof(clock_sync_t));
    sync->exchange_interval_ms = CLOCK_SYNC_EXCHANGE_INTERVAL_MS;
    sync->drift_update_interval_s = CLOCK_SYNC_DRIFT_UPDATE_INTERVAL_S;
    sync->initialized = true;
    sync->model_ref_time_us = esp_timer_get_time();
    
    ESP_LOGI(TAG, "Clock sync initialized (exchange=%dms, drift_update=%ds)", 
             sync->exchange_interval_ms, sync->drift_update_interval_s);
    return ESP_OK;
}

esp_err_t clock_sync_pod_send_request(clock_sync_t* sync, int64_t* send_timestamp_us) {
    if (!sync || !sync->initialized || !send_timestamp_us) return ESP_ERR_INVALID_ARG;

    *send_timestamp_us = esp_timer_get_time();
    sync->next_sequence++;

    // Record the request so pod_receive_reply can match it later.
    clock_sync_entry_t entry = {};
    entry.sequence = sync->next_sequence;
    entry.pod_send_us = *send_timestamp_us;
    sync->history[sync->head] = entry;
    sync->head = (uint8_t)((sync->head + 1) % CLOCK_SYNC_MAX_HISTORY);
    if (sync->count < CLOCK_SYNC_MAX_HISTORY) sync->count++;

    ESP_LOGD(TAG, "Pod send sync request: seq=%" PRIu32 ", t=%" PRId64, sync->next_sequence, *send_timestamp_us);
    return ESP_OK;
}

esp_err_t clock_sync_pod_receive_reply(clock_sync_t* sync, uint32_t sequence, 
                                        int64_t hub_recv_us, int64_t hub_send_us, 
                                        int64_t pod_recv_us) {
    if (!sync || !sync->initialized) return ESP_ERR_INVALID_STATE;
    
    // Find the matching request in history (search newest-first, wrap-safe).
    int idx = -1;
    for (int i = 0; i < sync->count; i++) {
        int h = (sync->head - 1 - i) % CLOCK_SYNC_MAX_HISTORY;
        if (h < 0) h += CLOCK_SYNC_MAX_HISTORY;
        if (sync->history[h].sequence == sequence) {
            idx = h;
            break;
        }
    }
    
    if (idx < 0) {
        ESP_LOGW(TAG, "Sync reply for unknown sequence: %" PRIu32, sequence);
        sync->exchanges_failed++;
        return ESP_ERR_NOT_FOUND;
    }
    
    // Update entry with reply timestamps
    sync->history[idx].hub_recv_us = hub_recv_us;
    sync->history[idx].hub_send_us = hub_send_us;
    sync->history[idx].pod_recv_us = pod_recv_us;
    
    // Compute RTT and offset using NTP-style algorithm
    // offset = ((hub_recv - pod_send) + (hub_send - pod_recv)) / 2
    int64_t pod_send = sync->history[idx].pod_send_us;
    int64_t offset1 = hub_recv_us - pod_send;
    int64_t offset2 = hub_send_us - pod_recv_us;
    int64_t offset = (offset1 + offset2) / 2;
    int64_t rtt = (pod_recv_us - pod_send) - (hub_send_us - hub_recv_us);
    
    sync->history[idx].offset_us = offset;
    sync->last_rtt_us = (float)rtt;
    sync->exchanges_completed++;
    
    // Estimate instantaneous drift
    // drift_ppm = (pod_recv - pod_send - (hub_send - hub_recv)) / (hub_send - hub_recv) * 1e6
    int64_t hub_elapsed = hub_send_us - hub_recv_us;
    if (hub_elapsed > 0) {
        float inst_drift = ((float)(pod_recv_us - pod_send - hub_elapsed) / (float)hub_elapsed) * 1e6f;
        sync->history[idx].drift_ppm = inst_drift;
    }
    
    // Check if we should update the drift model
    int64_t now = esp_timer_get_time();
    if (now - sync->model_ref_time_us >= (int64_t)sync->drift_update_interval_s * 1000000LL) {
        clock_sync_update_drift_model(sync, now);
    }
    
    ESP_LOGD(TAG, "Sync reply: seq=%" PRIu32 ", offset=%" PRId64 "us, rtt=%" PRId64 "us, drift=%.2fppm",
             sequence, offset, rtt, sync->history[idx].drift_ppm);
    
    return ESP_OK;
}

static esp_err_t clock_sync_update_drift_model(clock_sync_t* sync, int64_t ref_time_us) {
    if (!sync || sync->count < CLOCK_SYNC_MIN_SAMPLES_FOR_DRIFT) return ESP_ERR_INVALID_STATE;
    
    double sum_x = 0, sum_y = 0, sum_xy = 0, sum_x2 = 0;
    int valid = 0;
    
    int start = (sync->head + CLOCK_SYNC_MAX_HISTORY - sync->count) % CLOCK_SYNC_MAX_HISTORY;
    for (int i = 0; i < sync->count; i++) {
        int idx = (start + i) % CLOCK_SYNC_MAX_HISTORY;
        const clock_sync_entry_t* e = &sync->history[idx];
        
        if (e->hub_recv_us > 0 && e->hub_send_us > 0 && e->pod_recv_us > 0 && fabsf(e->drift_ppm) < 1000.0f) {
            // x = time in seconds from reference
            double x = (double)(e->hub_recv_us - ref_time_us) / 1e6;
            // y = offset in microseconds
            double y = (double)e->offset_us;
            
            sum_x += x;
            sum_y += y;
            sum_xy += x * y;
            sum_x2 += x * x;
            valid++;
        }
    }
    
    if (valid < 2) return ESP_ERR_INVALID_STATE;
    
    double denom = valid * sum_x2 - sum_x * sum_x;
    if (fabs(denom) < 1e-10) return ESP_ERR_INVALID_STATE;
    
    double a = (valid * sum_xy - sum_x * sum_y) / denom;  // drift rate (us/s)
    double b = (sum_y - a * sum_x) / valid;                // initial offset (us)
    
    sync->drift_a_ppm_per_sec = (float)(a * 1e6);  // Convert us/s to ppm
    sync->drift_b_us = (int64_t)b;
    sync->model_ref_time_us = ref_time_us;
    sync->model_valid = true;
    
    ESP_LOGI(TAG, "Drift model updated: a=%.3f ppm/s, b=%" PRId64 " us (n=%d)",
             sync->drift_a_ppm_per_sec, (int64_t)sync->drift_b_us, valid);
    
    return ESP_OK;
}

esp_err_t clock_sync_hub_handle_request(clock_sync_t* sync, uint32_t sequence, 
                                         int64_t pod_send_us,
                                         int64_t* hub_recv_us, int64_t* hub_send_us) {
    if (!sync || !sync->initialized || !hub_recv_us || !hub_send_us) return ESP_ERR_INVALID_ARG;
    
    *hub_recv_us = esp_timer_get_time();
    
    // Add to history for drift modeling
    clock_sync_entry_t entry = {
        .sequence = sequence,
        .pod_send_us = pod_send_us,
        .hub_recv_us = *hub_recv_us,
        .hub_send_us = 0,
        .pod_recv_us = 0,
        .offset_us = 0,
        .drift_ppm = 0.0f
    };
    
    sync->history[sync->head] = entry;
    sync->head = (sync->head + 1) % CLOCK_SYNC_MAX_HISTORY;
    if (sync->count < CLOCK_SYNC_MAX_HISTORY) sync->count++;
    
    // Simulate small processing delay
    *hub_send_us = esp_timer_get_time();
    
    // Update the entry with send time
    int idx = (sync->head + CLOCK_SYNC_MAX_HISTORY - 1) % CLOCK_SYNC_MAX_HISTORY;
    sync->history[idx].hub_send_us = *hub_send_us;
    
    ESP_LOGD(TAG, "Hub handle sync: seq=%" PRIu32 ", recv=%" PRId64 ", send=%" PRId64, 
             sequence, *hub_recv_us, *hub_send_us);
    return ESP_OK;
}

esp_err_t clock_sync_get_drift_model(const clock_sync_t* sync, float* drift_ppm, int64_t* offset_us) {
    if (!sync || !sync->initialized) return ESP_ERR_INVALID_STATE;
    
    if (!sync->model_valid) {
        // Fallback to last measured offset
        if (sync->count > 0) {
            int idx = (sync->head + CLOCK_SYNC_MAX_HISTORY - 1) % CLOCK_SYNC_MAX_HISTORY;
            if (drift_ppm) *drift_ppm = sync->history[idx].drift_ppm;
            if (offset_us) *offset_us = sync->history[idx].offset_us;
        } else {
            if (drift_ppm) *drift_ppm = 0.0f;
            if (offset_us) *offset_us = 0;
        }
        return ESP_OK;
    }
    
    if (drift_ppm) *drift_ppm = sync->drift_a_ppm_per_sec;
    if (offset_us) *offset_us = sync->drift_b_us;
    return ESP_OK;
}

esp_err_t clock_sync_correct_timestamp(const clock_sync_t* sync, int64_t pod_timestamp_us, int64_t* corrected_us) {
    if (!sync || !sync->initialized || !corrected_us) return ESP_ERR_INVALID_ARG;
    
    if (!sync->model_valid) {
        // Simple offset correction
        int64_t offset = 0;
        if (sync->count > 0) {
            int idx = (sync->head + CLOCK_SYNC_MAX_HISTORY - 1) % CLOCK_SYNC_MAX_HISTORY;
            offset = sync->history[idx].offset_us;
        }
        *corrected_us = pod_timestamp_us - offset;
        return ESP_OK;
    }
    
    // Apply linear drift model: offset(t) = a * t + b
    double t = (double)(pod_timestamp_us - sync->model_ref_time_us) / 1e6;
    double offset = sync->drift_a_ppm_per_sec * t / 1e6 + sync->drift_b_us;
    *corrected_us = pod_timestamp_us - (int64_t)offset;
    
    return ESP_OK;
}

esp_err_t clock_sync_get_stats(const clock_sync_t* sync, uint32_t* exchanges, float* drift_ppm, int64_t* offset_us, float* last_rtt) {
    if (!sync) return ESP_ERR_INVALID_ARG;
    
    if (exchanges) *exchanges = sync->exchanges_completed;
    if (last_rtt) *last_rtt = sync->last_rtt_us;
    
    if (sync->model_valid) {
        if (drift_ppm) *drift_ppm = sync->drift_a_ppm_per_sec;
        if (offset_us) *offset_us = sync->drift_b_us;
    } else if (sync->count > 0) {
        int idx = (sync->head + CLOCK_SYNC_MAX_HISTORY - 1) % CLOCK_SYNC_MAX_HISTORY;
        if (drift_ppm) *drift_ppm = sync->history[idx].drift_ppm;
        if (offset_us) *offset_us = sync->history[idx].offset_us;
    } else {
        if (drift_ppm) *drift_ppm = 0.0f;
        if (offset_us) *offset_us = 0;
    }
    
    return ESP_OK;
}

esp_err_t clock_sync_reset(clock_sync_t* sync) {
    if (!sync) return ESP_ERR_INVALID_ARG;
    
    sync->count = 0;
    sync->head = 0;
    sync->next_sequence = 0;
    sync->drift_a_ppm_per_sec = 0.0f;
    sync->drift_b_us = 0;
    sync->model_ref_time_us = esp_timer_get_time();
    sync->model_valid = false;
    sync->exchanges_completed = 0;
    sync->exchanges_failed = 0;
    sync->last_rtt_us = 0.0f;
    
    ESP_LOGI(TAG, "Clock sync reset");
    return ESP_OK;
}