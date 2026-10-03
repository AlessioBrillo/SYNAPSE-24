/**
 * @file synapse_signal_quality.h
 * @brief Synapse Band v1 - Signal Quality Assessment
 * 
 * Computes PPG SQI/MAP/PI, ECG quality, IMU motion/sleep probability.
 * Wraps firmware/common implementations (ppg_sqi, ppg_processor, imu_processor).
 */

#ifndef SYNAPSE_SIGNAL_QUALITY_H
#define SYNAPSE_SIGNAL_QUALITY_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "synapse_sensors.h"

#ifdef __cplusplus
extern "C" {
#endif

// PPG Signal Quality Metrics
typedef struct {
    float sqi;                    // Signal Quality Index (0.0-1.0)
    float perfusion_index;        // Perfusion Index (%)
    float motion_artifact_prob;   // Motion Artifact Probability (0.0-1.0)
    float snr_db;                 // Signal-to-Noise Ratio (dB)
    float hr_bpm;                 // Heart Rate (BPM)
    float rmssd_ms;               // RMSSD (ms)
    float sdnn_ms;                // SDNN (ms)
    uint16_t peak_count;          // Number of peaks in window
    bool valid;                   // Metrics valid
    int64_t timestamp_us;
} ppg_quality_t;

// ECG Quality Metrics
typedef struct {
    float quality;                // Overall quality (0.0-1.0)
    float r_peak_confidence;      // R-peak detection confidence
    float baseline_wander;        // Baseline wander level
    float noise_level;            // Estimated noise level
    bool lead_off;                // Lead-off detected
    int64_t timestamp_us;
} ecg_quality_t;

// IMU Quality Metrics
typedef struct {
    float motion_intensity;       // Motion intensity (g)
    float spectral_entropy;       // Spectral entropy
    float dominant_freq_hz;       // Dominant frequency (Hz)
    float sleep_probability;      // Sleep probability (0.0-1.0)
    bool is_stationary;           // Stationary state
    int64_t timestamp_us;
} imu_quality_t;

// Combined signal quality
typedef struct {
    ppg_quality_t ppg;
    ecg_quality_t ecg;
    imu_quality_t imu;
    float overall_quality;        // Weighted overall quality
} synapse_signal_quality_t;


// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize signal quality module
 * @return ESP_OK on success
 */
esp_err_t synapse_signal_quality_init(void);

/**
 * @brief Process new sensor samples and update quality metrics
 * Called periodically from acquisition FSM or sensor tasks
 * @param sample Sensor sample
 * @return ESP_OK on success
 */
esp_err_t synapse_signal_quality_process(const synapse_sensor_sample_t *sample);

/**
 * @brief Get latest PPG quality metrics
 * @param[out] quality PPG quality struct
 * @return ESP_OK on success
 */
esp_err_t synapse_signal_quality_get_ppg(ppg_quality_t *quality);

/**
 * @brief Get latest ECG quality metrics
 * @param[out] quality ECG quality struct
 * @return ESP_OK on success
 */
esp_err_t synapse_signal_quality_get_ecg(ecg_quality_t *quality);

/**
 * @brief Get latest IMU quality metrics
 * @param[out] quality IMU quality struct
 * @return ESP_OK on success
 */
esp_err_t synapse_signal_quality_get_imu(imu_quality_t *quality);

/**
 * @brief Get combined signal quality
 * @param[out] quality Combined quality struct
 * @return ESP_OK on success
 */
esp_err_t synapse_signal_quality_get_all(synapse_signal_quality_t *quality);

/**
 * @brief Check if PPG quality is sufficient for HRV analysis
 * @return true if SQI >= threshold and MAP <= threshold
 */
bool synapse_signal_quality_ppg_acceptable(void);

/**
 * @brief Check if ECG quality is sufficient for R-peak detection
 * @return true if quality >= threshold
 */
bool synapse_signal_quality_ecg_acceptable(void);

/**
 * @brief Check if IMU indicates stationary state (for Tier 1 promotion)
 * @return true if stationary
 */
bool synapse_signal_quality_imu_stationary(void);

/**
 * @brief Get sleep probability from IMU (for Tier 1 promotion)
 * @return Sleep probability (0.0-1.0)
 */
float synapse_signal_quality_get_sleep_probability(void);

/**
 * @brief Deinitialize signal quality module
 * @return ESP_OK on success
 */
esp_err_t synapse_signal_quality_deinit(void);

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_SIGNAL_QUALITY_H