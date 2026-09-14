/**
 * @file ecg_ad8232.cpp
 * @brief AD8232 Single-Lead ECG Driver Implementation
 *
 * AD8232 Configuration:
 * - Single-lead (Lead I equivalent: RA-LA)
 * - Bandpass: 0.5-40 Hz (hardware)
 * - Gain: 1000x (typical)
 * - Reference: 1.65V (VDD/2 for 3.3V)
 * - Output: 0-3.3V centered at 1.65V
 * - Sampling: 500 Hz via ADC1_CH0 (GPIO36)
 */

#include "ecg_ad8232.h"
#include "driver/adc.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cmath>

static const char* TAG = "ECG_AD8232";

// ADC handles
static adc_oneshot_unit_handle_t s_adc_handle = NULL;
static adc_cali_handle_t s_adc_cali_handle = NULL;
static bool s_calibrated = false;
static int16_t s_baseline = 0;  // Calibrated baseline (mV * 1000)
static uint32_t s_sample_count = 0;

// Lead-off detection GPIO
static bool s_lead_off_plus_init = false;
static bool s_lead_off_minus_init = false;

// ============================================================================
// INITIALIZATION
// ============================================================================

esp_err_t ecg_ad8232_init(void) {
    ESP_LOGI(TAG, "Initializing AD8232 ECG sensor...");

    // ADC unit already initialized in main, get handle
    // Note: In production, we'd store the handle globally
    // For now, re-initialize (idempotent)
    adc_oneshot_unit_init_cfg_t init_cfg = {
        .unit_id = ADC_UNIT_1,
        .ulp_mode = ADC_ULP_MODE_DISABLE
    };
    esp_err_t err = adc_oneshot_new_unit(&init_cfg, &s_adc_handle);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "ADC unit init failed: %s", esp_err_to_name(err));
        return err;
    }

    // Configure ECG channel
    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,    // 0-3.3V range
        .bitwidth = ADC_BITWIDTH_12, // 12-bit = 0-4095
    };
    err = adc_oneshot_config_channel(s_adc_handle, SYNAPSE_ECG_ADC_PIN, &chan_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADC channel config failed: %s", esp_err_to_name(err));
        return err;
    }

    // Configure calibration (line fitting for better accuracy)
    adc_cali_line_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    err = adc_cali_create_scheme_line_fitting(&cali_cfg, &s_adc_cali_handle);
    if (err == ESP_OK) {
        s_calibrated = true;
        ESP_LOGI(TAG, "ADC calibration enabled");
    } else {
        ESP_LOGW(TAG, "ADC calibration unavailable, using raw values: %s", esp_err_to_name(err));
        s_calibrated = false;
    }

    // Configure lead-off detection pins (input with pull-up)
    gpio_config_t lo_conf = {
        .pin_bit_mask = (1ULL << SYNAPSE_ECG_LO_PLUS_PIN) | (1ULL << SYNAPSE_ECG_LO_MINUS_PIN),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE
    };
    gpio_config(&lo_conf);
    s_lead_off_plus_init = true;
    s_lead_off_minus_init = true;

    // Calibrate baseline
    ecg_ad8232_calibrate();

    ESP_LOGI(TAG, "AD8232 initialized: pin=%d, rate=%d Hz, calibrated=%s",
             SYNAPSE_ECG_ADC_PIN, SYNAPSE_ECG_SAMPLING_RATE_HZ, s_calibrated ? "yes" : "no");

    return ESP_OK;
}

// ============================================================================
// SAMPLE READING
// ============================================================================

esp_err_t ecg_ad8232_read_sample(synapse_ecg_sample_t* sample) {
    if (!sample || !s_adc_handle) {
        return ESP_ERR_INVALID_ARG;
    }

    int raw_value = 0;
    esp_err_t err = adc_oneshot_read(s_adc_handle, SYNAPSE_ECG_ADC_PIN, &raw_value);
    if (err != ESP_OK) {
        return err;
    }

    // Convert to mV * 1000 (µV)
    int mV = 0;
    if (s_calibrated && s_adc_cali_handle) {
        adc_cali_raw_to_voltage(s_adc_cali_handle, raw_value, &mV);
    } else {
        // Approximate: 12-bit ADC, 3.3V reference
        mV = (raw_value * 3300) / 4095;
    }

    // Center around baseline and convert to µV
    sample->value_mv = (int16_t)((mV - s_baseline) * 1000);
    sample->timestamp_us = esp_timer_get_time();
    sample->lead_off = ecg_ad8232_check_lead_off();

    s_sample_count++;

    return ESP_OK;
}

// ============================================================================
// CALIBRATION
// ============================================================================

void ecg_ad8232_calibrate(void) {
    ESP_LOGI(TAG, "Calibrating ECG baseline...");

    const int num_samples = 1000;
    int64_t sum = 0;
    int valid_samples = 0;

    for (int i = 0; i < num_samples; i++) {
        int raw = 0;
        if (adc_oneshot_read(s_adc_handle, SYNAPSE_ECG_ADC_PIN, &raw) == ESP_OK) {
            int mV = 0;
            if (s_calibrated && s_adc_cali_handle) {
                adc_cali_raw_to_voltage(s_adc_cali_handle, raw, &mV);
            } else {
                mV = (raw * 3300) / 4095;
            }
            sum += mV;
            valid_samples++;
        }
        vTaskDelay(pdMS_TO_TICKS(2)); // ~500 Hz equivalent spacing
    }

    if (valid_samples > 0) {
        s_baseline = (int16_t)(sum / valid_samples);
        ESP_LOGI(TAG, "ECG baseline calibrated: %d mV (from %d samples)", s_baseline, valid_samples);
    } else {
        ESP_LOGW(TAG, "ECG calibration failed, using default baseline");
        s_baseline = 1650; // Mid-supply (1.65V)
    }
}

// ============================================================================
// LEAD-OFF DETECTION
// ============================================================================

bool ecg_ad8232_check_lead_off(void) {
    if (!s_lead_off_plus_init || !s_lead_off_minus_init) {
        return false;
    }

    // AD8232: LO+ and LO- go HIGH when leads are off
    bool lo_plus = gpio_get_level(SYNAPSE_ECG_LO_PLUS_PIN);
    bool lo_minus = gpio_get_level(SYNAPSE_ECG_LO_MINUS_PIN);

    return lo_plus || lo_minus;
}

// ============================================================================
// DEINITIALIZATION
// ============================================================================

void ecg_ad8232_deinit(void) {
    if (s_adc_cali_handle) {
        adc_cali_delete_scheme_line_fitting(s_adc_cali_handle);
        s_adc_cali_handle = NULL;
    }
    if (s_adc_handle) {
        adc_oneshot_del_unit(s_adc_handle);
        s_adc_handle = NULL;
    }
    s_calibrated = false;
    ESP_LOGI(TAG, "AD8232 deinitialized");
}