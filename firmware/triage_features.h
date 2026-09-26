#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <math.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TRIAGE_NUM_FEATURES 26
#define TRIAGE_FEATURE_CHEST_ACC_MEAN_X     0
#define TRIAGE_FEATURE_CHEST_ACC_STD_X      1
#define TRIAGE_FEATURE_CHEST_ACC_ENTROPY_X  2
#define TRIAGE_FEATURE_CHEST_ACC_DOMFREQ_X  3
#define TRIAGE_FEATURE_CHEST_ACC_MEAN_Y     4
#define TRIAGE_FEATURE_CHEST_ACC_STD_Y      5
#define TRIAGE_FEATURE_CHEST_ACC_ENTROPY_Y  6
#define TRIAGE_FEATURE_CHEST_ACC_DOMFREQ_Y  7
#define TRIAGE_FEATURE_CHEST_ACC_MEAN_Z     8
#define TRIAGE_FEATURE_CHEST_ACC_STD_Z      9
#define TRIAGE_FEATURE_CHEST_ACC_ENTROPY_Z  10
#define TRIAGE_FEATURE_CHEST_ACC_DOMFREQ_Z  11
#define TRIAGE_FEATURE_WRIST_ACC_MEAN_X     12
#define TRIAGE_FEATURE_WRIST_ACC_STD_X      13
#define TRIAGE_FEATURE_WRIST_ACC_ENTROPY_X  14
#define TRIAGE_FEATURE_WRIST_ACC_DOMFREQ_X  15
#define TRIAGE_FEATURE_WRIST_ACC_MEAN_Y     16
#define TRIAGE_FEATURE_WRIST_ACC_STD_Y      17
#define TRIAGE_FEATURE_WRIST_ACC_ENTROPY_Y  18
#define TRIAGE_FEATURE_WRIST_ACC_DOMFREQ_Y  19
#define TRIAGE_FEATURE_WRIST_ACC_MEAN_Z     20
#define TRIAGE_FEATURE_WRIST_ACC_STD_Z      21
#define TRIAGE_FEATURE_WRIST_ACC_ENTROPY_Z  22
#define TRIAGE_FEATURE_WRIST_ACC_DOMFREQ_Z  23
#define TRIAGE_FEATURE_WRIST_BVP_MEAN       24
#define TRIAGE_FEATURE_WRIST_BVP_STD        25

#define TRIAGE_LIVE_IMU_FS_HZ     100
#define TRIAGE_LIVE_PPG_FS_HZ     64

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
