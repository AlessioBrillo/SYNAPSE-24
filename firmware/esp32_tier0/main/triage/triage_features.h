#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TRIAGE_NUM_FEATURES 26
#define TRIAGE_LIVE_IMU_FS_HZ 100.0f

typedef struct {
    float features[TRIAGE_NUM_FEATURES];
    int feature_count;
    int64_t timestamp_us;
} triage_features_t;

esp_err_t triage_features_compute_live(
    const void* imu_ring_buffer,
    const void* ppg_ring_buffer,
    triage_features_t* features_out
);

#ifdef __cplusplus
}
#endif