#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Clock Synchronization Protocol (BLE-based)
// Architecture.md §92: Tier 0 sync residual drift ≤10 ms (achieved ≤8 ms in Phase 0)
// BLE timestamp exchange every 1s with linear drift model updated every 60s

#define CLOCK_SYNC_EXCHANGE_INTERVAL_MS   1000   // 1 Hz timestamp exchange
#define CLOCK_SYNC_DRIFT_UPDATE_INTERVAL_S 60    // Linear drift model update every 60s
#define CLOCK_SYNC_MAX_HISTORY            120    // 2 minutes of history at 1Hz
#define CLOCK_SYNC_MIN_SAMPLES_FOR_DRIFT  10     // Minimum samples for drift estimate

typedef struct {
    uint32_t sequence;
    int64_t pod_send_us;      // Pod timestamp when sent
    int64_t hub_recv_us;      // Hub timestamp when received (from hub reply)
    int64_t hub_send_us;      // Hub timestamp when reply sent
    int64_t pod_recv_us;      // Pod timestamp when reply received
    int64_t offset_us;        // Estimated clock offset
    float drift_ppm;          // Estimated drift in parts per million
} clock_sync_entry_t;

typedef struct {
    // Configuration
    uint32_t exchange_interval_ms;
    uint32_t drift_update_interval_s;
    
    // State
    clock_sync_entry_t history[CLOCK_SYNC_MAX_HISTORY];
    uint8_t head;
    uint8_t count;
    uint32_t next_sequence;
    bool initialized;
    
    // Drift model (linear: offset = a * t + b)
    float drift_a_ppm_per_sec;  // Drift rate (ppm/s)
    float drift_b_us;           // Initial offset (us)
    int64_t model_ref_time_us;  // Reference time for model
    bool model_valid;
    
    // Statistics
    uint32_t exchanges_completed;
    uint32_t exchanges_failed;
    float last_rtt_us;
} clock_sync_t;

// Initialize clock sync
esp_err_t clock_sync_init(clock_sync_t* sync);

// Called when pod sends sync request (pod side)
esp_err_t clock_sync_pod_send_request(clock_sync_t* sync, int64_t* send_timestamp_us);

// Called when pod receives hub reply (pod side)
// hub_recv_us and hub_send_us come from hub's reply payload
esp_err_t clock_sync_pod_receive_reply(clock_sync_t* sync, uint32_t sequence, 
                                        int64_t hub_recv_us, int64_t hub_send_us, 
                                        int64_t pod_recv_us);

// Called on hub when receiving pod request (hub side)
// Returns the hub timestamps to send back to pod
esp_err_t clock_sync_hub_handle_request(clock_sync_t* sync, uint32_t sequence, 
                                         int64_t pod_send_us,
                                         int64_t* hub_recv_us, int64_t* hub_send_us);

// Get current drift model parameters
esp_err_t clock_sync_get_drift_model(const clock_sync_t* sync, float* drift_ppm, int64_t* offset_us);

// Correct a pod timestamp to hub clock domain using current drift model
esp_err_t clock_sync_correct_timestamp(const clock_sync_t* sync, int64_t pod_timestamp_us, int64_t* corrected_us);

// Get sync statistics
esp_err_t clock_sync_get_stats(const clock_sync_t* sync, uint32_t* exchanges, float* drift_ppm, int64_t* offset_us, float* last_rtt);

// Reset sync state
esp_err_t clock_sync_reset(clock_sync_t* sync);

#ifdef __cplusplus
}
#endif