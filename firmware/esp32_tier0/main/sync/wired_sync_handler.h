#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "driver/gpio.h"
#include "esp_timer.h"

#ifdef __cplusplus
extern "C" {
#endif

// Wired GPIO sync configuration (matches Python config/hardware_bringup.yaml)
#define SYNAPSE_WIRED_SYNC_GPIO           21
#define SYNAPSE_WIRED_SYNC_PULSE_WIDTH_US 10
#define SYNAPSE_WIRED_SYNC_PULL_UP        true

typedef enum {
    SYNAPSE_SYNC_ROLE_HUB   = 0,  // Hub drives the sync pulse (output)
    SYNAPSE_SYNC_ROLE_POD   = 1,  // Pod receives sync pulse (input with interrupt)
} synapse_sync_role_t;

typedef struct {
    gpio_num_t gpio_pin;
    synapse_sync_role_t role;
    uint32_t pulse_width_us;
    bool pull_up;
    bool initialized;
    
    // Callback for sync pulse received (pod side)
    void (*on_sync_pulse)(int64_t timestamp_us, void* user_ctx);
    void* user_ctx;
    
    // Statistics
    uint32_t pulses_sent;
    uint32_t pulses_received;
    int64_t last_pulse_timestamp_us;
    float avg_interval_ms;
} synapse_wired_sync_t;

// Initialize wired sync hardware
esp_err_t synapse_wired_sync_init(synapse_wired_sync_t* sync, synapse_sync_role_t role);

// Hub: Send sync pulse to all pods
esp_err_t synapse_wired_sync_send_pulse(synapse_wired_sync_t* sync);

// Pod: Get last sync pulse timestamp
int64_t synapse_wired_sync_get_last_pulse(const synapse_wired_sync_t* sync);

// Get sync statistics
void synapse_wired_sync_get_stats(const synapse_wired_sync_t* sync,
                                   uint32_t* sent, uint32_t* received,
                                   float* avg_interval_ms);

// Deinitialize wired sync
esp_err_t synapse_wired_sync_deinit(synapse_wired_sync_t* sync);

#ifdef __cplusplus
}
#endif