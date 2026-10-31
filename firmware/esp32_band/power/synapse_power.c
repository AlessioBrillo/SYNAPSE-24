/**
 * @file synapse_power.c
 * @brief Power Management Implementation
 * 
 * Band v1 power monitor (INA219 + battery ADC)
 */

#include <string.h>
#include <math.h>
#include <stdlib.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "synapse_power.h"
#include "synapse_config.h"
#include "synapse_adc1.h"
#ifdef CONFIG_SYNAPSE_FEATURE_POWER_MONITOR
#include "ina219.h"
#endif

static const char *TAG = "SYNAPSE_POWER";

static synapse_power_status_t g_status = {0};
static adc_oneshot_unit_handle_t g_adc_handle = NULL;
static uint16_t battery_mv_unchecked(void);
static adc_cali_handle_t g_cali_handle = NULL;
static adc_channel_t g_bat_adc_channel = ADC_CHANNEL_3; // ADC1 CH3 = GPIO4 on ESP32-S3
static float g_voltage_divider = 2.0f;
static bool g_charging = false;
static bool g_initialized = false;
static synapse_power_telemetry_sink_t g_telemetry_sink = NULL;

#ifdef CONFIG_SYNAPSE_FEATURE_POWER_MONITOR
static bool g_ina_present = false;
#endif

// Battery voltage to percentage mapping (LiPo 3.7V)
static const struct {
    float voltage_v;
    uint8_t percentage;
} bat_lut[] = {
    {4.20, 100}, {4.15, 95}, {4.10, 90}, {4.05, 85}, {4.00, 80},
    {3.95, 75}, {3.90, 70}, {3.85, 65}, {3.80, 60}, {3.75, 55},
    {3.70, 50}, {3.65, 45}, {3.60, 40}, {3.55, 35}, {3.50, 30},
    {3.45, 25}, {3.40, 20}, {3.35, 15}, {3.30, 10}, {3.20, 5},
    {3.00, 0}
};

static uint8_t voltage_to_percentage(float voltage_v) {
    for (size_t i = 0; i < sizeof(bat_lut)/sizeof(bat_lut[0]) - 1; i++) {
        if (voltage_v >= bat_lut[i+1].voltage_v && voltage_v <= bat_lut[i].voltage_v) {
            float t = (voltage_v - bat_lut[i+1].voltage_v) / (bat_lut[i].voltage_v - bat_lut[i+1].voltage_v);
            return bat_lut[i+1].percentage + (uint8_t)(t * (bat_lut[i].percentage - bat_lut[i+1].percentage));
        }
    }
    if (voltage_v > bat_lut[0].voltage_v) return 100;
    return 0;
}

esp_err_t synapse_power_init(void) {
    if (g_initialized) return ESP_OK;
    
    // Load hardware config
    synapse_hw_config_t hw_cfg;
    ESP_ERROR_CHECK(synapse_config_get(&hw_cfg));
    g_bat_adc_channel = (adc_channel_t)hw_cfg.bat_adc_channel;
    g_voltage_divider = hw_cfg.bat_voltage_divider;

    // Initialize ADC oneshot unit
    // ADC1 is shared with the ECG channel: borrow the single handle.
    ESP_ERROR_CHECK(synapse_adc1_get(&g_adc_handle));

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    ESP_ERROR_CHECK(adc_oneshot_config_channel(g_adc_handle, g_bat_adc_channel, &chan_cfg));

    // ADC calibration (line fitting; falls back to raw scaling if eFuse missing)
    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    esp_err_t cali_ret = adc_cali_create_scheme_curve_fitting(&cali_cfg, &g_cali_handle);
    if (cali_ret != ESP_OK) {
        ESP_LOGW(TAG, "ADC calibration not available (%s), using raw scaling",
                 esp_err_to_name(cali_ret));
        g_cali_handle = NULL;
    }

#ifdef CONFIG_SYNAPSE_FEATURE_POWER_MONITOR
    // INA219 on the shared I2C bus: real current, not a tier-model estimate.
    // A missing breakout is not an error — the rig runs, and the status says
    // the current figure is a model rather than a measurement.
    ina219_config_t ina_cfg = {
        .i2c_port = CONFIG_SYNAPSE_INA219_I2C_PORT,
        .i2c_addr = CONFIG_SYNAPSE_INA219_I2C_ADDR,
        .shunt_ohm = CONFIG_SYNAPSE_INA219_SHUNT_MOHM / 1000.0f,
        .max_expected_a = CONFIG_SYNAPSE_INA219_MAX_CURRENT_MA / 1000.0f,
    };
    esp_err_t ina_ret = ina219_init(&ina_cfg);
    if (ina_ret == ESP_OK) {
        g_ina_present = true;
        ESP_LOGI(TAG, "INA219 power monitor active: measured current telemetry enabled");
    } else {
        ESP_LOGW(TAG, "INA219 not available (%s): current draw stays a tier-model estimate",
                 esp_err_to_name(ina_ret));
    }
#endif
    
    // Initialize status from config
    g_status.battery_capacity_mah = hw_cfg.hub_battery_mah;
    g_status.battery_remaining_mah = hw_cfg.hub_battery_mah;
    g_status.tier0_h_used = 0;
    g_status.tier1_h_used = 0;
    g_status.tier2_min_used = 0;
    g_status.can_afford_tier1 = true;
    g_status.can_afford_tier2 = true;
    g_status.charging = false;
    
    // Initial voltage read (g_initialized is still false here: use the unchecked
    // read, or the first reading is always 0 V)
    uint16_t mv = battery_mv_unchecked();
    g_status.voltage_v = mv / 1000.0f;
    g_status.battery_level_pct = voltage_to_percentage(g_status.voltage_v);
    g_status.estimated_remaining_h = g_status.battery_remaining_mah / (hw_cfg.t0_avg_mw / 3.7f);
    
    g_initialized = true;
    ESP_LOGI(TAG, "Power management initialized (bat: %.2fV, %d%%, cap: %.0f mAh)",
             g_status.voltage_v, g_status.battery_level_pct, g_status.battery_capacity_mah);
    return ESP_OK;
}

void synapse_power_tick(void) {
    if (!g_initialized) return;

    // The main loop calls this every 10 ms, but the battery averaging (64 ADC
    // reads), the INA219 read, the coulomb counter and the POWER stream are 1 Hz
    // affairs: ungated, the stream ran at 100 Hz (~2.8 kB/s against a 64 B/s budget).
    static int64_t last_run_us = 0;
    int64_t run_us = esp_timer_get_time();
    if (last_run_us != 0 && (run_us - last_run_us) < 1000000) return;
    last_run_us = run_us;
    
    // Read battery voltage
    uint16_t mv = battery_mv_unchecked();
    float voltage_v = mv / 1000.0f;
    
    // Low-pass filter
    g_status.voltage_v = 0.9f * g_status.voltage_v + 0.1f * voltage_v;
    g_status.battery_level_pct = voltage_to_percentage(g_status.voltage_v);
    
    bool current_measured = false;
#ifdef CONFIG_SYNAPSE_FEATURE_POWER_MONITOR
    // Measured current replaces the tier-model estimate (Architecture.md §55-62:
    // the budget is validated against what the pod actually draws, and the
    // per-session figure is streamed with the recording).
    if (g_ina_present) {
        ina219_data_t meas = {0};
        if (ina219_read_once(&meas) == ESP_OK) {
            g_status.voltage_v = meas.bus_voltage_v;
            g_status.current_ma = meas.current_a * 1000.0f;  // A -> mA
            current_measured = true;
            g_status.battery_level_pct = voltage_to_percentage(g_status.voltage_v);
            if (g_telemetry_sink != NULL) {
                g_telemetry_sink(g_status.voltage_v * 1000.0f, g_status.current_ma,
                                 (float)g_status.battery_level_pct);
            }
        }
    }
#endif

    // Estimate current draw based on current tier
    synapse_hw_config_t hw_cfg;
    synapse_config_get(&hw_cfg);
    
    float current_mw = hw_cfg.t0_avg_mw;  // Default Tier 0
    // In a full implementation, would get current tier from acquisition FSM
    
    if (!current_measured) {
        g_status.current_ma = (current_mw / g_status.voltage_v);  // mA, tier model
    }    
    // Update remaining capacity (simple coulomb counting)
    static int64_t last_tick = 0;
    int64_t now = esp_timer_get_time();
    if (last_tick > 0) {
        float dt_h = (now - last_tick) / 3600000000.0f;  // hours
        if (!g_charging) {
            float consumed_mah = g_status.current_ma * dt_h;
            g_status.battery_remaining_mah -= consumed_mah;
            if (g_status.battery_remaining_mah < 0) g_status.battery_remaining_mah = 0;
        } else {
            // Charging - simple model
            g_status.battery_remaining_mah += 100.0f * dt_h;  // Assume 100mA charge
            if (g_status.battery_remaining_mah > g_status.battery_capacity_mah) {
                g_status.battery_remaining_mah = g_status.battery_capacity_mah;
            }
        }
    }
    last_tick = now;
    
    // Estimate remaining time at current draw
    if (g_status.current_ma > 0) {
        g_status.estimated_remaining_h = g_status.battery_remaining_mah / g_status.current_ma;
    } else {
        g_status.estimated_remaining_h = 999.0f;
    }
    
    // Check tier affordability
    float reserve_mah = 300.0f;  // From config
    float available_mah = g_status.battery_remaining_mah - reserve_mah;
    if (available_mah < 0) available_mah = 0;
    
    g_status.can_afford_tier1 = (available_mah >= (hw_cfg.t1_avg_mw / 3.7f) * 1.0f);  // 1h Tier 1
    g_status.can_afford_tier2 = (available_mah >= (hw_cfg.t2_avg_mw / 3.7f) * 0.5f);  // 30min Tier 2
}

esp_err_t synapse_power_get_status(synapse_power_status_t *status) {
    if (!status) return ESP_ERR_INVALID_ARG;
    if (!g_initialized) return ESP_ERR_INVALID_STATE;
    
    *status = g_status;
    return ESP_OK;
}

void synapse_power_on_tier_change(int from_tier, int to_tier) {
    if (!g_initialized) return;
    
    synapse_hw_config_t hw_cfg;
    synapse_config_get(&hw_cfg);
    
    // Log tier change
    ESP_LOGI(TAG, "Tier change: %d -> %d", from_tier, to_tier);
}

void synapse_power_set_telemetry_sink(synapse_power_telemetry_sink_t sink) {
    g_telemetry_sink = sink;
    ESP_LOGI(TAG, "power telemetry sink %s", sink ? "attached" : "detached");
}

bool synapse_power_has_measured_current(void) {
#ifdef CONFIG_SYNAPSE_FEATURE_POWER_MONITOR
    return g_ina_present;
#else
    return false;
#endif
}

bool synapse_power_can_afford_tier1(float duration_h) {
    if (!g_initialized) return false;
    
    synapse_hw_config_t hw_cfg;
    synapse_config_get(&hw_cfg);
    
    float required_mah = (hw_cfg.t1_avg_mw / 3.7f) * duration_h;
    float available_mah = g_status.battery_remaining_mah - 300.0f;  // reserve
    
    return available_mah >= required_mah;
}

bool synapse_power_can_afford_tier2(float duration_min) {
    if (!g_initialized) return false;
    
    synapse_hw_config_t hw_cfg;
    synapse_config_get(&hw_cfg);
    
    float required_mah = (hw_cfg.t2_avg_mw / 3.7f) * (duration_min / 60.0f);
    float available_mah = g_status.battery_remaining_mah - 300.0f;  // reserve
    
    return available_mah >= required_mah;
}

void synapse_power_record_tier1_session(float actual_duration_h) {
    if (!g_initialized) return;
    g_status.tier1_h_used += actual_duration_h;
    ESP_LOGI(TAG, "Recorded Tier 1 session: %.2f h (total today: %.2f h)", 
             actual_duration_h, g_status.tier1_h_used);
}

void synapse_power_record_tier2_session(float actual_duration_min) {
    if (!g_initialized) return;
    g_status.tier2_min_used += actual_duration_min;
    ESP_LOGI(TAG, "Recorded Tier 2 session: %.1f min (total today: %.1f min)", 
             actual_duration_min, g_status.tier2_min_used);
}

void synapse_power_set_charging(bool charging) {
    g_charging = charging;
    g_status.charging = charging;
    ESP_LOGI(TAG, "Charging: %s", charging ? "YES" : "NO");
}

static uint16_t battery_mv_unchecked(void) {
    if (!g_adc_handle) return 0;

    int adc_raw = 0;
    const int samples = 64;
    int acc = 0;
    for (int i = 0; i < samples; i++) {
        if (adc_oneshot_read(g_adc_handle, g_bat_adc_channel, &adc_raw) == ESP_OK) {
            acc += adc_raw;
        }
    }
    acc /= samples;

    int voltage_mv = 0;
    if (g_cali_handle) {
        if (adc_cali_raw_to_voltage(g_cali_handle, acc, &voltage_mv) != ESP_OK) {
            voltage_mv = (acc * 3300) / 4095; // fallback raw scaling
        }
    } else {
        voltage_mv = (acc * 3300) / 4095; // fallback raw scaling
    }
    return (uint16_t)(voltage_mv * g_voltage_divider);
}

uint16_t synapse_power_read_battery_mv(void) {
    return g_initialized ? battery_mv_unchecked() : 0;
}

esp_err_t synapse_power_deinit(void) {
    if (!g_initialized) return ESP_OK;

    if (g_cali_handle) {
        adc_cali_delete_scheme_curve_fitting(g_cali_handle);
        g_cali_handle = NULL;
    }
    g_adc_handle = NULL;  // borrowed from synapse_adc1, not ours to delete

    g_initialized = false;
    ESP_LOGI(TAG, "Power management deinitialized");
    return ESP_OK;
}