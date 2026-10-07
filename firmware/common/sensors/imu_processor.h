#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

// IMU Feature Processor Configuration
// Architecture.md §34: Tier 0 continuous H24 - on-device feature extraction
// 10-second sliding window, 1 Hz feature output rate (for sleep/wake triage)

#define IMU_PROCESSOR_WINDOW_SEC      10
#define IMU_PROCESSOR_OUTPUT_HZ       1
#define IMU_PROCESSOR_SAMPLE_RATE_HZ  50
#define IMU_PROCESSOR_WINDOW_SAMPLES  (IMU_PROCESSOR_WINDOW_SEC * IMU_PROCESSOR_SAMPLE_RATE_HZ)

// Sleep/wake classification thresholds (calibrated from literature)
// See: "Sleep/wake classification from wrist accelerometry" - Cole et al.

typedef struct {
    float motion_intensity;       // RMS acceleration magnitude (g)
    float acc_rms_x, acc_rms_y, acc_rms_z;  // Per-axis RMS
    float gyro_rms_x, gyro_rms_y, gyro_rms_z;  // Per-axis gyro RMS
    float tilt_x_deg, tilt_y_deg, tilt_z_deg;  // Static tilt angles (degrees)
    float spectral_entropy;       // Spectral entropy (0=pure tone, 1=noise)
    float dominant_freq_hz;       // Dominant frequency in 0.5-3 Hz band
    float sleep_probability;      // Sleep probability [0,1] (IMU-only triage)
    bool  is_stationary;          // Stationary detection (for Tier 1 promotion)
    int64_t timestamp_us;
} imu_features_t;

typedef struct {
    float ax_buffer[IMU_PROCESSOR_WINDOW_SAMPLES];
    float ay_buffer[IMU_PROCESSOR_WINDOW_SAMPLES];
    float az_buffer[IMU_PROCESSOR_WINDOW_SAMPLES];
    float gx_buffer[IMU_PROCESSOR_WINDOW_SAMPLES];
    float gy_buffer[IMU_PROCESSOR_WINDOW_SAMPLES];
    float gz_buffer[IMU_PROCESSOR_WINDOW_SAMPLES];
    size_t write_idx;
    size_t count;
    bool initialized;
    
    // Output decimation: 100Hz -> 1Hz
    int samples_since_output;
    
    // Cached latest features
    imu_features_t last_features;
} imu_processor_ctx_t;

// Initialize IMU processor
esp_err_t imu_processor_init(void);

// Process IMU sample (called from IMU driver at 100Hz)
esp_err_t imu_processor_process_sample(float ax, float ay, float az, 
                                       float gx, float gy, float gz,
                                       int64_t timestamp_us, imu_features_t* features_out);

// Get latest computed features
esp_err_t imu_processor_get_latest(imu_features_t* features_out);

// Reset processor state
esp_err_t imu_processor_reset(void);

// Sleep/wake classifier (logistic regression, TFLM-ready)
// Returns sleep probability [0,1]
float imu_sleep_wake_classify(const imu_features_t* features);

#ifdef __cplusplus
}
#endif