/**
 * @file power_monitor.h
 * @brief Power Budget Monitor Interface
 *
 * Energy model per Architecture.md §55-62 and config/hardware.yaml power_budget
 * - mWh = mW * hours
 * - mAh = mWh / V_nominal (3.7V for LiPo)
 * - Tracks consumption per tier, enforces 24h target with reserve
 */

#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

// Battery configuration (matches config/hardware.yaml power_budget section)
#define SYNAPSE_NOMINAL_VOLTAGE_V       3.7f
#define SYNAPSE_HUB_BATTERY_MAH         3000.0f
#define SYNAPSE_POD_BATTERY_MAH         200.0f
#define SYNAPSE_RESERVE_MAH             300.0f
#define SYNAPSE_TARGET_LIFETIME_H       24.0f
#define SYNAPSE_T0_AVG_MW               5.0f
#define SYNAPSE_T1_AVG_MW               50.0f
#define SYNAPSE_T2_AVG_MW               100.0f
#define SYNAPSE_T1_MAX_DURATION_H       10.0f
#define SYNAPSE_T2_MAX_BURST_MIN        30.0f

// Battery ADC
#define SYNAPSE_BATTERY_ADC_PIN         ADC_CHANNEL_0  // GPIO3 (ADC2_CH0)
#define SYNAPSE_BATTERY_VOLTAGE_DIVIDER_RATIO 2.0f  // 1:1 voltage divider

// Power status structure
typedef struct {
    float battery_remaining_mah;
    float battery_capacity_mah;
    float estimated_remaining_h;
    float tier0_h_used;
    float tier1_h_used;
    float tier2_h_used;
    bool can_afford_tier1;
    bool can_afford_tier2;
    float power_draw_mw;
} synapse_power_status_t;

// Initialize power monitor
esp_err_t power_monitor_init(void);

// Called on tier change
void power_monitor_on_tier_change(int from_tier, int to_tier);

// Periodic update (call from main loop)
void power_monitor_update(void);

// Get current power status
void power_monitor_get_status(synapse_power_status_t* status);

// Affordability checks
bool power_monitor_can_afford_tier1(float duration_h);
bool power_monitor_can_afford_tier2(float duration_min);

// Record completed sessions
void power_monitor_record_tier1_session(float actual_duration_h);
void power_monitor_record_tier2_session(float actual_duration_min);

// Read battery voltage
uint16_t power_monitor_read_battery_mv(void);

// Deinitialize
void power_monitor_deinit(void);

#ifdef __cplusplus
}
#endif