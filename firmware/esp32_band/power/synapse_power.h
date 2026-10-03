/**
 * @file synapse_power.h
 * @brief Synapse Band v1 - Power Management
 * 
 * Battery monitoring, tier affordability, 24h target enforcement.
 * Wraps firmware/common/power_monitor.
 */

#ifndef SYNAPSE_POWER_H
#define SYNAPSE_POWER_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Power status
typedef struct {
    float battery_remaining_mah;   // Estimated remaining capacity
    float battery_capacity_mah;    // Total capacity
    float estimated_remaining_h;   // Estimated hours at current draw
    float voltage_v;               // Current battery voltage
    float current_ma;              // Current draw (estimated)
    float tier0_h_used;            // Tier 0 hours used today
    float tier1_h_used;            // Tier 1 hours used today
    float tier2_min_used;          // Tier 2 minutes used today
    bool can_afford_tier1;         // Can afford Tier 1 session
    bool can_afford_tier2;         // Can afford Tier 2 session
    bool charging;                 // Currently charging
    uint8_t battery_level_pct;     // Battery percentage (0-100)
} synapse_power_status_t;


// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize power management
 * Sets up ADC for battery voltage monitoring
 * @return ESP_OK on success
 */
esp_err_t synapse_power_init(void);

/**
 * @brief Periodic update (call from main loop or acquisition FSM)
 * Updates battery estimate, checks tier affordability
 */
void synapse_power_tick(void);

/**
 * @brief Get current power status
 * @param[out] status Power status struct
 * @return ESP_OK on success
 */
esp_err_t synapse_power_get_status(synapse_power_status_t *status);

/**
 * @brief Notify power module of tier change
 * @param from_tier Previous tier
 * @param to_tier New tier
 */
void synapse_power_on_tier_change(int from_tier, int to_tier);

/**
 * @brief Check if Tier 1 session is affordable
 * @param duration_h Requested duration in hours
 * @return true if affordable
 */
bool synapse_power_can_afford_tier1(float duration_h);

/**
 * @brief Check if Tier 2 session is affordable
 * @param duration_min Requested duration in minutes
 * @return true if affordable
 */
bool synapse_power_can_afford_tier2(float duration_min);

/**
 * @brief Record completed Tier 1 session
 * @param actual_duration_h Actual duration in hours
 */
void synapse_power_record_tier1_session(float actual_duration_h);

/**
 * @brief Record completed Tier 2 session
 * @param actual_duration_min Actual duration in minutes
 */
void synapse_power_record_tier2_session(float actual_duration_min);

/**
 * @brief Set charging state
 * @param charging true if charging
 */
void synapse_power_set_charging(bool charging);

/**
 * @brief Read raw battery voltage (mV)
 * @return Battery voltage in mV
 */
uint16_t synapse_power_read_battery_mv(void);

/**
 * @brief Deinitialize power management
 * @return ESP_OK on success
 */
esp_err_t synapse_power_deinit(void);

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_POWER_H