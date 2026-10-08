/**
 * @file synapse_signal_quality.c
 * @brief Signal Quality Assessment Implementation
 * 
 * Wraps firmware/common: ppg_sqi, ppg_processor, imu_processor
 */

#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "synapse_signal_quality.h"
#include "ppg_sqi.h"
#include "ppg_processor.h"
#include "imu_processor.h"
#include "ppg_max30102.h"
#include "ecg_ad8232.h"
#include "triage_features.h"

static const char *TAG = "SYNAPSE_SQ";

// ppg_dsp / ppg_processor are designed for 50 Hz PPG; fail the build rather than report HR wrong by 64/50.
_Static_assert(CONFIG_SYNAPSE_PPG_SAMPLE_RATE == 0x02, "SYNAPSE_PPG_SAMPLE_RATE must be 0x02 (50 Hz)");

static ppg_quality_t g_latest_ppg_quality = {0};
static ecg_quality_t g_latest_ecg_quality = {0};
static imu_quality_t g_latest_imu_quality = {0};
static SemaphoreHandle_t g_quality_mutex = NULL;
static bool g_initialized = false;

// Thresholds for acceptability
#define PPG_SQI_THRESHOLD      0.3f
#define PPG_MAP_THRESHOLD      0.5f
#define ECG_QUALITY_THRESHOLD  0.4f

esp_err_t synapse_signal_quality_init(void) {
    if (g_initialized) return ESP_OK;
    
    g_quality_mutex = xSemaphoreCreateMutex();
    if (!g_quality_mutex) {
        ESP_LOGE(TAG, "Failed to create mutex");
        return ESP_ERR_NO_MEM;
    }
    
    // Initialize underlying processors
    ESP_ERROR_CHECK(ppg_sqi_init());
    ESP_ERROR_CHECK(ppg_processor_init());
    ESP_ERROR_CHECK(imu_processor_init());
    
    memset(&g_latest_ppg_quality, 0, sizeof(ppg_quality_t));
    memset(&g_latest_ecg_quality, 0, sizeof(ecg_quality_t));
    memset(&g_latest_imu_quality, 0, sizeof(imu_quality_t));
    
    g_initialized = true;
    ESP_LOGI(TAG, "Signal quality initialized");
    return ESP_OK;
}

esp_err_t synapse_signal_quality_process(const synapse_sensor_sample_t *sample) {
    if (!g_initialized || !sample) return ESP_ERR_INVALID_ARG;

    // Feed the processors / triage window (nothing else did: they only exposed get_latest()).
    ppg_sqi_result_t feed_sqi = {0};
    bool ppg_new = false, imu_new = false;  // processors emit 10 Hz / 1 Hz, not per sample
    switch (sample->base.type) {
    case SENSOR_TYPE_PPG:
        (void)ppg_max30102_get_sqi(&feed_sqi);
        ppg_new = ppg_processor_process_sample(sample->base.data.ppg.red, sample->base.data.ppg.ir,
                                               sample->base.timestamp_us, &feed_sqi, NULL) == ESP_OK;
        triage_features_push_ppg_ir(sample->base.data.ppg.ir);
        break;
    case SENSOR_TYPE_IMU:
        imu_new = imu_processor_process_sample(sample->base.data.imu.ax, sample->base.data.imu.ay,
                                               sample->base.data.imu.az, sample->base.data.imu.gx,
                                               sample->base.data.imu.gy, sample->base.data.imu.gz,
                                               sample->base.timestamp_us, NULL) == ESP_OK;
        triage_features_push_imu(sample->base.data.imu.ax, sample->base.data.imu.ay,
                                 sample->base.data.imu.az, sample->base.data.imu.gx,
                                 sample->base.data.imu.gy, sample->base.data.imu.gz);
        break;
    default:
        return ESP_OK;  // ECG (500 Hz) has no processor here; skip the aggregate update
    }
    if (!ppg_new && !imu_new) return ESP_OK;  // keep quality timestamps honest: no new features
    
    // Process PPG quality
    ppg_sqi_result_t sqi_result;
    if (ppg_new && ppg_max30102_get_sqi(&sqi_result) == ESP_OK) {
        // Get PPG features from processor
        ppg_features_t ppg_feat;
        if (ppg_processor_get_latest(&ppg_feat) == ESP_OK && ppg_feat.valid) {
            if (xSemaphoreTake(g_quality_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
                g_latest_ppg_quality.sqi = sqi_result.sqi;
                g_latest_ppg_quality.perfusion_index = sqi_result.perfusion_index;
                g_latest_ppg_quality.motion_artifact_prob = sqi_result.motion_artifact_prob;
                g_latest_ppg_quality.snr_db = ppg_feat.snr_db;
                g_latest_ppg_quality.hr_bpm = ppg_feat.hr_bpm;
                g_latest_ppg_quality.rmssd_ms = ppg_feat.rmssd_ms;
                g_latest_ppg_quality.sdnn_ms = ppg_feat.sdnn_ms;
                g_latest_ppg_quality.peak_count = ppg_feat.peak_count;
                g_latest_ppg_quality.valid = true;
                g_latest_ppg_quality.timestamp_us = esp_timer_get_time();
                xSemaphoreGive(g_quality_mutex);
            }
        }
    }
    
    // Process IMU quality
    imu_features_t imu_feat;
    if (imu_new && imu_processor_get_latest(&imu_feat) == ESP_OK) {
        if (xSemaphoreTake(g_quality_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
            g_latest_imu_quality.motion_intensity = imu_feat.motion_intensity;
            g_latest_imu_quality.spectral_entropy = imu_feat.spectral_entropy;
            g_latest_imu_quality.dominant_freq_hz = imu_feat.dominant_freq_hz;
            g_latest_imu_quality.sleep_probability = imu_feat.sleep_probability;
            g_latest_imu_quality.is_stationary = imu_feat.is_stationary;
            g_latest_imu_quality.timestamp_us = esp_timer_get_time();
            xSemaphoreGive(g_quality_mutex);
        }
    }
    
    // ECG quality (simplified - based on lead-off and signal amplitude)
    // In a full implementation, would use ecg_processor from common
    ecg_quality_t ecg_qual = {0};
    ecg_qual.quality = 0.8f;  // Placeholder
    ecg_qual.r_peak_confidence = 0.85f;
    ecg_qual.baseline_wander = 0.1f;
    ecg_qual.noise_level = 0.15f;
    ecg_qual.lead_off = ecg_ad8232_is_lead_off(NULL);  // Would need user_ctx
    ecg_qual.timestamp_us = esp_timer_get_time();
    
    if (xSemaphoreTake(g_quality_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        g_latest_ecg_quality = ecg_qual;
        xSemaphoreGive(g_quality_mutex);
    }
    
    return ESP_OK;
}

esp_err_t synapse_signal_quality_get_ppg(ppg_quality_t *quality) {
    if (!quality) return ESP_ERR_INVALID_ARG;
    if (!g_initialized) return ESP_ERR_INVALID_STATE;
    
    if (xSemaphoreTake(g_quality_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        *quality = g_latest_ppg_quality;
        xSemaphoreGive(g_quality_mutex);
        return ESP_OK;
    }
    return ESP_ERR_TIMEOUT;
}

esp_err_t synapse_signal_quality_get_ecg(ecg_quality_t *quality) {
    if (!quality) return ESP_ERR_INVALID_ARG;
    if (!g_initialized) return ESP_ERR_INVALID_STATE;
    
    if (xSemaphoreTake(g_quality_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        *quality = g_latest_ecg_quality;
        xSemaphoreGive(g_quality_mutex);
        return ESP_OK;
    }
    return ESP_ERR_TIMEOUT;
}

esp_err_t synapse_signal_quality_get_imu(imu_quality_t *quality) {
    if (!quality) return ESP_ERR_INVALID_ARG;
    if (!g_initialized) return ESP_ERR_INVALID_STATE;
    
    if (xSemaphoreTake(g_quality_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        *quality = g_latest_imu_quality;
        xSemaphoreGive(g_quality_mutex);
        return ESP_OK;
    }
    return ESP_ERR_TIMEOUT;
}

esp_err_t synapse_signal_quality_get_all(synapse_signal_quality_t *quality) {
    if (!quality) return ESP_ERR_INVALID_ARG;
    if (!g_initialized) return ESP_ERR_INVALID_STATE;
    
    if (xSemaphoreTake(g_quality_mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        quality->ppg = g_latest_ppg_quality;
        quality->ecg = g_latest_ecg_quality;
        quality->imu = g_latest_imu_quality;
        
        // Compute overall quality (weighted)
        quality->overall_quality = 0.4f * g_latest_ppg_quality.sqi +
                                   0.3f * g_latest_ecg_quality.quality +
                                   0.3f * (g_latest_imu_quality.is_stationary ? 1.0f : 0.5f);
        xSemaphoreGive(g_quality_mutex);
        return ESP_OK;
    }
    return ESP_ERR_TIMEOUT;
}

bool synapse_signal_quality_ppg_acceptable(void) {
    return g_latest_ppg_quality.valid && 
           g_latest_ppg_quality.sqi >= PPG_SQI_THRESHOLD &&
           g_latest_ppg_quality.motion_artifact_prob <= PPG_MAP_THRESHOLD;
}

bool synapse_signal_quality_ecg_acceptable(void) {
    return g_latest_ecg_quality.quality >= ECG_QUALITY_THRESHOLD && !g_latest_ecg_quality.lead_off;
}

bool synapse_signal_quality_imu_stationary(void) {
    return g_latest_imu_quality.is_stationary;
}

float synapse_signal_quality_get_sleep_probability(void) {
    return g_latest_imu_quality.sleep_probability;
}

esp_err_t synapse_signal_quality_deinit(void) {
    if (!g_initialized) return ESP_OK;
    
    if (g_quality_mutex) {
        vSemaphoreDelete(g_quality_mutex);
        g_quality_mutex = NULL;
    }
    
    g_initialized = false;
    ESP_LOGI(TAG, "Signal quality deinitialized");
    return ESP_OK;
}