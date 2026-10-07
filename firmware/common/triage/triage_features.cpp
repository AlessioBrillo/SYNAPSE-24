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
static float s_bvp[TRIAGE_PPG_WIN];
static int s_imu_head, s_imu_count;
static int s_bvp_head, s_bvp_count;
static triage_bvp_filter_t s_bvp_filter;  // PPG task only
static bool s_inited;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

// Linearised copies for the DSP (static: keeps the triage task stack small).
static float s_imu_lin[TRIAGE_IMU_WIN * 6];
static float s_bvp_lin[TRIAGE_PPG_WIN];

esp_err_t triage_features_init(uint32_t imu_rate_hz, uint32_t ppg_rate_hz) {
    if (imu_rate_hz != TRIAGE_IMU_FS_HZ || ppg_rate_hz != TRIAGE_PPG_FS_HZ) {
        ESP_LOGE(TAG, "Rate mismatch: imu=%u ppg=%u, features need %d/%d Hz",
                 (unsigned)imu_rate_hz, (unsigned)ppg_rate_hz, TRIAGE_IMU_FS_HZ, TRIAGE_PPG_FS_HZ);
        return ESP_ERR_INVALID_ARG;
    }
    portENTER_CRITICAL(&s_lock);
    s_imu_head = s_imu_count = s_bvp_head = s_bvp_count = 0;
    memset(&s_bvp_filter, 0, sizeof(s_bvp_filter));
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
    float bvp = triage_bvp_filter_step(&s_bvp_filter, ir);  // outside the lock: PPG task owns the filter
    portENTER_CRITICAL(&s_lock);
    s_bvp[s_bvp_head] = bvp;
    s_bvp_head = (s_bvp_head + 1) % TRIAGE_PPG_WIN;
    if (s_bvp_count < TRIAGE_PPG_WIN) s_bvp_count++;
    portEXIT_CRITICAL(&s_lock);
}

esp_err_t triage_features_compute_live(triage_features_t* features_out) {
    if (!features_out) return ESP_ERR_INVALID_ARG;
    if (!s_inited) return ESP_ERR_INVALID_STATE;

    int n_imu, n_bvp;
    portENTER_CRITICAL(&s_lock);
    n_imu = s_imu_count;
    n_bvp = s_bvp_count;
    if (n_imu < TRIAGE_IMU_WIN) {  // wait for the full 4 s window the model was trained on
        portEXIT_CRITICAL(&s_lock);
        memset(features_out, 0, sizeof(*features_out));
        return ESP_ERR_INVALID_SIZE;
    }
    // Oldest -> newest. When the buffer is full, head points at the oldest sample.
    for (int i = 0; i < n_imu; i++) {
        memcpy(&s_imu_lin[i * 6], s_imu[(s_imu_head + i) % TRIAGE_IMU_WIN], 6 * sizeof(float));
    }
    int bvp_start = (s_bvp_count < TRIAGE_PPG_WIN) ? 0 : s_bvp_head;
    for (int i = 0; i < n_bvp; i++) {
        s_bvp_lin[i] = s_bvp[(bvp_start + i) % TRIAGE_PPG_WIN];
    }
    portEXIT_CRITICAL(&s_lock);

    triage_features_from_window(s_imu_lin, n_imu, s_bvp_lin, n_bvp,
                                (float)TRIAGE_IMU_FS_HZ, features_out->features);
    features_out->feature_count = TRIAGE_NUM_FEATURES;
    features_out->timestamp_us = esp_timer_get_time();
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
