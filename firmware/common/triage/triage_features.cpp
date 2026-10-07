#include "triage_features.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include <math.h>
#include <string.h>

static const char* TAG = "triage_features";

// 4 s sliding windows owned by this module, filled by the IMU/PPG feature tasks.
static float s_imu[TRIAGE_IMU_WIN][6];
static float s_ir[TRIAGE_PPG_WIN];  // raw IR; band-passed per window to match Python exactly
static int s_imu_head, s_imu_count;
static int s_ir_head, s_ir_count;
static bool s_inited, s_busy;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

// Linearised copies for the DSP (static: keeps the triage task stack small).
static float s_imu_lin[TRIAGE_IMU_WIN * 6];
static float s_ir_lin[TRIAGE_PPG_WIN];
static float s_bvp_lin[TRIAGE_PPG_WIN];

esp_err_t triage_features_init(uint32_t imu_rate_hz, uint32_t ppg_rate_hz) {
    if (imu_rate_hz != TRIAGE_IMU_FS_HZ || ppg_rate_hz != TRIAGE_PPG_FS_HZ) {
        ESP_LOGE(TAG, "Rate mismatch: imu=%u ppg=%u, features need %d/%d Hz",
                 (unsigned)imu_rate_hz, (unsigned)ppg_rate_hz, TRIAGE_IMU_FS_HZ, TRIAGE_PPG_FS_HZ);
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_lock);
    s_imu_head = s_imu_count = s_ir_head = s_ir_count = 0;
    s_busy = false;
    s_inited = true;
    portEXIT_CRITICAL(&s_lock);
    return ESP_OK;
}

void triage_features_push_imu(float ax, float ay, float az, float gx, float gy, float gz) {
    portENTER_CRITICAL(&s_lock);
    float* row = s_imu[s_imu_head];
    row[0] = ax; row[1] = ay; row[2] = az;
    row[3] = gx; row[4] = gy; row[5] = gz;
    s_imu_head = (s_imu_head + 1) % TRIAGE_IMU_WIN;
    if (s_imu_count < TRIAGE_IMU_WIN) s_imu_count++;
    portEXIT_CRITICAL(&s_lock);
}

void triage_features_push_ppg_ir(float ir) {
    portENTER_CRITICAL(&s_lock);
    s_ir[s_ir_head] = ir;
    s_ir_head = (s_ir_head + 1) % TRIAGE_PPG_WIN;
    if (s_ir_count < TRIAGE_PPG_WIN) s_ir_count++;
    portEXIT_CRITICAL(&s_lock);
}

// Copies a ring (oldest -> newest) into dst as at most two contiguous memcpy.
static void ring_linearise(float* dst, const float* ring, int head, int win, int row) {
    size_t tail_rows = (size_t)(win - head);
    memcpy(dst, ring + (size_t)head * row, tail_rows * row * sizeof(float));
    memcpy(dst + tail_rows * row, ring, (size_t)head * row * sizeof(float));
}

esp_err_t triage_features_compute_live(triage_features_t* features_out) {
    if (!features_out) return ESP_ERR_INVALID_ARG;
    if (!s_inited) return ESP_ERR_INVALID_STATE;

    portENTER_CRITICAL(&s_lock);
    // Both windows must be full: the model was trained on complete 4 s IMU and BVP windows.
    if (s_busy) {  // DSP + linearised scratch are shared; never run two computes at once
        portEXIT_CRITICAL(&s_lock);
        return ESP_ERR_NOT_FINISHED;
    }
    if (s_imu_count < TRIAGE_IMU_WIN || s_ir_count < TRIAGE_PPG_WIN) {
        portEXIT_CRITICAL(&s_lock);
        memset(features_out, 0, sizeof(*features_out));
        return ESP_ERR_INVALID_SIZE;
    }
    s_busy = true;
    // Full ring: head points at the oldest sample.
    ring_linearise(s_imu_lin, &s_imu[0][0], s_imu_head, TRIAGE_IMU_WIN, 6);
    ring_linearise(s_ir_lin, s_ir, s_ir_head, TRIAGE_PPG_WIN, 1);
    portEXIT_CRITICAL(&s_lock);

    triage_bvp_from_ir(s_ir_lin, TRIAGE_PPG_WIN, s_bvp_lin);
    triage_features_from_window(s_imu_lin, TRIAGE_IMU_WIN, s_bvp_lin, TRIAGE_PPG_WIN,
                                (float)TRIAGE_IMU_FS_HZ, features_out->features);
    features_out->feature_count = TRIAGE_NUM_FEATURES;
    features_out->timestamp_us = esp_timer_get_time();

    portENTER_CRITICAL(&s_lock);
    s_busy = false;
    portEXIT_CRITICAL(&s_lock);
    return ESP_OK;
}

float triage_compute_motion_intensity(const triage_features_t* features) {
    if (!features) return 0.0f;
    // Combine standard deviation of Acc X, Y, Z (indices 1, 5, 9)
    // Empirically, Acc std can be high. Let's normalize by a typical motion threshold.
    float intensity = (features->features[1] + features->features[5] + features->features[9]) / 3.0f;
    // Normalize to [0,1] assuming > 2.0g standard deviation is "high motion"
    return fminf(intensity / 2.0f, 1.0f);
}
