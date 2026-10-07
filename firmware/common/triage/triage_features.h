#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "triage_dsp.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    float features[TRIAGE_NUM_FEATURES];
    int feature_count;
    int64_t timestamp_us;
} triage_features_t;

// Fails with ESP_ERR_INVALID_ARG unless both rates equal TRIAGE_*_FS_HZ (the rates the
// features are computed for; see feature_extraction.TRIAGE_IMU_FS_HZ).
esp_err_t triage_features_init(uint32_t imu_rate_hz, uint32_t ppg_rate_hz);

// Producers: call from the feature tasks that already pop the scheduler ring buffers.
// Triage never touches the ring buffers itself (avoids starving them / being starved).
void triage_features_push_imu(float ax, float ay, float az, float gx, float gy, float gz);
void triage_features_push_ppg_ir(float ir);

// ESP_ERR_INVALID_STATE before init, ESP_ERR_INVALID_SIZE until the 4 s IMU window is full.
esp_err_t triage_features_compute_live(triage_features_t* features_out);

float triage_compute_motion_intensity(const triage_features_t* features);

#ifdef __cplusplus
}
#endif
