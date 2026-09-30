/**
 * @file power_monitor.cpp
 * @brief Power Budget Monitor Implementation
 *
 * Energy model:
 * - mWh = mW * hours
 * - mAh = mWh / V_nominal (3.7V for LiPo)
 * - Tracks consumption per tier, enforces 24h target with reserve
 */

#include "power_monitor.h"
#include "driver/adc.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <cmath>

static const char* TAG = "POWER_MON";

// ============================================================================
// STATE
// ============================================================================

typedef struct {
    // Battery
    float hub_battery_mah;
    float pod_battery_mah;
    float reserve_mah;
    float usable_mah;

    // Power profiles (mW)
    float t0_avg_mw;
    float t1_avg_mw;
    float t2_avg_mw;
    float t1_max_h;
    float t2_max_min;

    // Target
    float target_lifetime_h;

    // Usage tracking
    float t0_total_h;
    float t1_total_h;
    float t2_total_h;
    int64_t tier_start_time_us;
    int current_tier;

    // ADC
    adc_oneshot_unit_handle_t adc_handle;
    adc_cali_handle_t adc_cali_handle;
    bool calibrated;

    SemaphoreHandle_t mutex;
    bool initialized;
} power_state_t;

static power_state_t s_state = {0};

// ============================================================================
// HELPERS
// ============================================================================

static inline float mah_for_power(float mw, float hours) {
    return mw * hours / SYNAPSE_NOMINAL_VOLTAGE_V;
}

static inline float hours_for_charge(float mah, float mw) {
    if (mw <= 0) return INFINITY;
    return mah * SYNAPSE_NOMINAL_VOLTAGE_V / mw;
}

// ============================================================================
// INITIALIZATION
// ============================================================================

esp_err_t power_monitor_init(void) {
    ESP_LOGI(TAG, "Initializing Power Monitor...");

    s_state.mutex = xSemaphoreCreateMutex();
    if (!s_state.mutex) return ESP_FAIL;

    // Configuration from Kconfig / compile-time constants
    s_state.hub_battery_mah = SYNAPSE_HUB_BATTERY_MAH;
    s_state.pod_battery_mah = SYNAPSE_POD_BATTERY_MAH;
    s_state.reserve_mah = SYNAPSE_RESERVE_MAH;
    s_state.usable_mah = s_state.hub_battery_mah - s_state.reserve_mah;

    s_state.t0_avg_mw = SYNAPSE_T0_AVG_MW;
    s_state.t1_avg_mw = SYNAPSE_T1_AVG_MW;
    s_state.t2_avg_mw = SYNAPSE_T2_AVG_MW;
    s_state.t1_max_h = SYNAPSE_T1_MAX_DURATION_H;
    s_state.t2_max_min = SYNAPSE_T2_MAX_BURST_MIN;
    s_state.target_lifetime_h = SYNAPSE_TARGET_LIFETIME_H;

    s_state.t0_total_h = 0.0f;
    s_state.t1_total_h = 0.0f;
    s_state.t2_total_h = 0.0f;
    s_state.tier_start_time_us = esp_timer_get_time();
    s_state.current_tier = 0;

    // Battery ADC
    adc_oneshot_unit_init_cfg_t adc_cfg = {
        .unit_id = ADC_UNIT_1,
        .ulp_mode = ADC_ULP_MODE_DISABLE
    };
    esp_err_t err = adc_oneshot_new_unit(&adc_cfg, &s_state.adc_handle);
    if (err == ESP_OK || err == ESP_ERR_INVALID_STATE) {
        adc_oneshot_chan_cfg_t chan_cfg = {
            .atten = ADC_ATTEN_DB_12,
            .bitwidth = ADC_BITWIDTH_12,
        };
        adc_oneshot_config_channel(s_state.adc_handle, SYNAPSE_BATTERY_ADC_PIN, &chan_cfg);

        adc_cali_line_fitting_config_t cali_cfg = {
            .unit_id = ADC_UNIT_1,
            .atten = ADC_ATTEN_DB_12,
            .bitwidth = ADC_BITWIDTH_12,
        };
        err = adc_cali_create_scheme_line_fitting(&cali_cfg, &s_state.adc_cali_handle);
        if (err == ESP_OK) {
            s_state.calibrated = true;
        }
    }

    s_state.initialized = true;
    ESP_LOGI(TAG, "Power Monitor initialized: Hub=%.0fmAh, Pod=%.0fmAh, Reserve=%.0fmAh, Target=%.1fh",
             s_state.hub_battery_mah, s_state.pod_battery_mah, s_state.reserve_mah, s_state.target_lifetime_h);
    return ESP_OK;
}

// ============================================================================
// TIER CHANGE HANDLING
// ============================================================================

void power_monitor_on_tier_change(int from_tier, int to_tier) {
    if (!s_state.initialized) return;

    int64_t now = esp_timer_get_time();
    float elapsed_h = (float)(now - s_state.tier_start_time_us) / 3.6e9f; // µs to hours

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        // Accumulate previous tier usage
        switch (from_tier) {
            case 0: s_state.t0_total_h += elapsed_h; break;
            case 1: s_state.t1_total_h += elapsed_h; break;
            case 2: s_state.t2_total_h += elapsed_h; break;
        }

        s_state.current_tier = to_tier;
        s_state.tier_start_time_us = now;

        ESP_LOGI(TAG, "Tier change: %d -> %d, elapsed=%.3fh, T0=%.2fh, T1=%.2fh, T2=%.2fh",
                 from_tier, to_tier, elapsed_h, s_state.t0_total_h, s_state.t1_total_h, s_state.t2_total_h);

        xSemaphoreGive(s_state.mutex);
    }
}

// ============================================================================
// PERIODIC UPDATE
// ============================================================================

void power_monitor_update(void) {
    if (!s_state.initialized) return;

    // Read battery voltage
    uint16_t battery_mv = power_monitor_read_battery_mv();

    // Log status every update
    synapse_power_status_t status;
    power_monitor_get_status(&status);
    ESP_LOGI(TAG, "Battery: %dmV (%.0fmAh, %.1fh), Power: %.1fmW, T0:%.2fh T1:%.2fh T2:%.2fh",
             battery_mv, status.battery_remaining_mah, status.estimated_remaining_h,
             status.power_draw_mw, status.tier0_h_used, status.tier1_h_used, status.tier2_h_used);
}

// ============================================================================
// GET STATUS
// ============================================================================

void power_monitor_get_status(synapse_power_status_t* status) {
    if (!s_state.initialized || !status) return;

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        // Current consumption
        float consumed_mah = 0.0f;
        consumed_mah += mah_for_power(s_state.t0_avg_mw, s_state.t0_total_h);
        consumed_mah += mah_for_power(s_state.t1_avg_mw, s_state.t1_total_h);
        consumed_mah += mah_for_power(s_state.t2_avg_mw, s_state.t2_total_h);

        // Current session
        int64_t now = esp_timer_get_time();
        float current_h = (float)(now - s_state.tier_start_time_us) / 3.6e9f;
        float current_mw = (s_state.current_tier == 0) ? s_state.t0_avg_mw :
                           (s_state.current_tier == 1) ? s_state.t1_avg_mw : s_state.t2_avg_mw;
        consumed_mah += mah_for_power(current_mw, current_h);

        float remaining_mah = s_state.usable_mah - consumed_mah;
        if (remaining_mah < 0) remaining_mah = 0;

        float total_elapsed_h = s_state.t0_total_h + s_state.t1_total_h + s_state.t2_total_h + current_h;
        float remaining_target_h = s_state.target_lifetime_h - total_elapsed_h;
        if (remaining_target_h < 0) remaining_target_h = 0;

        float estimated_remaining_h = hours_for_charge(remaining_mah, current_mw);

        // Can afford checks
        float tier1_needed_mah = mah_for_power(s_state.t1_avg_mw, 1.0f);
        float tier0_for_remaining_mah = mah_for_power(s_state.t0_avg_mw, remaining_target_h);
        bool can_t1 = (consumed_mah + tier1_needed_mah + tier0_for_remaining_mah) <= s_state.usable_mah;

        float tier2_needed_mah = mah_for_power(s_state.t2_avg_mw, 10.0f / 60.0f);
        bool can_t2 = (consumed_mah + tier2_needed_mah + tier0_for_remaining_mah) <= s_state.usable_mah;

        // Max duration checks
        if (s_state.t1_total_h >= s_state.t1_max_h) can_t1 = false;
        if (s_state.t2_total_h >= s_state.t2_max_min / 60.0f) can_t2 = false;

        status->battery_remaining_mah = remaining_mah;
        status->battery_capacity_mah = s_state.hub_battery_mah;
        status->estimated_remaining_h = estimated_remaining_h;
        status->tier0_h_used = s_state.t0_total_h + ((s_state.current_tier == 0) ? current_h : 0);
        status->tier1_h_used = s_state.t1_total_h + ((s_state.current_tier == 1) ? current_h : 0);
        status->tier2_h_used = s_state.t2_total_h + ((s_state.current_tier == 2) ? current_h : 0);
        status->can_afford_tier1 = can_t1;
        status->can_afford_tier2 = can_t2;
        status->power_draw_mw = current_mw;

        xSemaphoreGive(s_state.mutex);
    }
}

// ============================================================================
// AFFORDABILITY CHECKS
// ============================================================================

bool power_monitor_can_afford_tier1(float duration_h) {
    if (!s_state.initialized) return false;
    if (duration_h > s_state.t1_max_h) return false;

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        float consumed_mah = mah_for_power(s_state.t0_avg_mw, s_state.t0_total_h) +
                            mah_for_power(s_state.t1_avg_mw, s_state.t1_total_h) +
                            mah_for_power(s_state.t2_avg_mw, s_state.t2_total_h);

        int64_t now = esp_timer_get_time();
        float current_h = (float)(now - s_state.tier_start_time_us) / 3.6e9f;
        float current_mw = (s_state.current_tier == 0) ? s_state.t0_avg_mw :
                           (s_state.current_tier == 1) ? s_state.t1_avg_mw : s_state.t2_avg_mw;
        consumed_mah += mah_for_power(current_mw, current_h);

        float projected_mah = consumed_mah + mah_for_power(s_state.t1_avg_mw, duration_h);
        float remaining_target_h = s_state.target_lifetime_h -
                                  (s_state.t0_total_h + s_state.t1_total_h + s_state.t2_total_h + current_h);
        float tier0_needed_mah = mah_for_power(s_state.t0_avg_mw, remaining_target_h);

        bool can = (projected_mah + tier0_needed_mah) <= s_state.usable_mah;
        xSemaphoreGive(s_state.mutex);
        return can;
    }
    return false;
}

bool power_monitor_can_afford_tier2(float duration_min) {
    if (!s_state.initialized) return false;
    float duration_h = duration_min / 60.0f;
    if (duration_h > s_state.t2_max_min / 60.0f) return false;

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        float consumed_mah = mah_for_power(s_state.t0_avg_mw, s_state.t0_total_h) +
                            mah_for_power(s_state.t1_avg_mw, s_state.t1_total_h) +
                            mah_for_power(s_state.t2_avg_mw, s_state.t2_total_h);

        int64_t now = esp_timer_get_time();
        float current_h = (float)(now - s_state.tier_start_time_us) / 3.6e9f;
        float current_mw = (s_state.current_tier == 0) ? s_state.t0_avg_mw :
                           (s_state.current_tier == 1) ? s_state.t1_avg_mw : s_state.t2_avg_mw;
        consumed_mah += mah_for_power(current_mw, current_h);

        float projected_mah = consumed_mah + mah_for_power(s_state.t2_avg_mw, duration_h);
        float remaining_target_h = s_state.target_lifetime_h -
                                  (s_state.t0_total_h + s_state.t1_total_h + s_state.t2_total_h + current_h);
        float tier0_needed_mah = mah_for_power(s_state.t0_avg_mw, remaining_target_h);

        bool can = (projected_mah + tier0_needed_mah) <= s_state.usable_mah;
        xSemaphoreGive(s_state.mutex);
        return can;
    }
    return false;
}

// ============================================================================
// RECORD SESSIONS
// ============================================================================

void power_monitor_record_tier1_session(float actual_duration_h) {
    if (!s_state.initialized) return;
    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        s_state.t1_total_h += actual_duration_h;
        xSemaphoreGive(s_state.mutex);
    }
}

void power_monitor_record_tier2_session(float actual_duration_min) {
    if (!s_state.initialized) return;
    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        s_state.t2_total_h += actual_duration_min / 60.0f;
        xSemaphoreGive(s_state.mutex);
    }
}

// ============================================================================
// BATTERY READING
// ============================================================================

uint16_t power_monitor_read_battery_mv(void) {
    if (!s_state.initialized || !s_state.adc_handle) return 0;

    int raw = 0;
    if (adc_oneshot_read(s_state.adc_handle, SYNAPSE_BATTERY_ADC_PIN, &raw) != ESP_OK) {
        return 0;
    }

    int mv = 0;
    if (s_state.calibrated && s_state.adc_cali_handle) {
        adc_cali_raw_to_voltage(s_state.adc_cali_handle, raw, &mv);
    } else {
        mv = (raw * 3300) / 4095;
    }

    // Apply voltage divider ratio
    return (uint16_t)(mv * SYNAPSE_BATTERY_VOLTAGE_DIVIDER_RATIO);
}

// ============================================================================
// DEINITIALIZATION
// ============================================================================

void power_monitor_deinit(void) {
    if (s_state.adc_cali_handle) {
        adc_cali_delete_scheme_line_fitting(s_state.adc_cali_handle);
    }
    if (s_state.adc_handle) {
        adc_oneshot_del_unit(s_state.adc_handle);
    }
    if (s_state.mutex) {
        vSemaphoreDelete(s_state.mutex);
    }
    s_state.initialized = false;
    ESP_LOGI(TAG, "Power Monitor deinitialized");
}