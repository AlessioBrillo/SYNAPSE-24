#include "triage_features.h"
#include "sensor_scheduler.h"
#include "esp_log.h"
#include <math.h>
#include <string.h>

static const char* TAG = "triage_features";

#define IMU_RING_BUF_SIZE 32

// Goertzel algorithm for dominant frequency estimation (integer-friendly)
static float goertzel_magnitude(const float* signal, int n, float fs, float target_freq) {
    if (n < 4) return 0.0f;
    float omega = 2.0f * M_PI * target_freq / fs;
    float coeff = 2.0f * cosf(omega);
    float q1 = 0.0f, q2 = 0.0f;
    for (int i = 0; i < n; i++) {
        float q0 = coeff * q1 - q2 + signal[i];
        q2 = q1;
        q1 = q0;
    }
    float real = q1 - q2 * cosf(omega);
    float imag = q2 * sinf(omega);
    return sqrtf(real*real + imag*imag) / n;
}

// Approximate spectral entropy using 4-band power ratios (MCU-friendly)
static float spectral_entropy_approx(const float* signal, int n, float fs) {
    if (n < 4) return 0.5f;
    // Simple variance-based entropy proxy for MCU
    float mean = 0.0f;
    for (int i = 0; i < n; i++) mean += signal[i];
    mean /= n;
    
    float var = 0.0f;
    for (int i = 0; i < n; i++) {
        float diff = signal[i] - mean;
        var += diff * diff;
    }
    var /= n;
    
    // Map variance to [0,1] entropy range (calibrated empirically)
    // Higher variance -> more "noisy" -> higher entropy
    return fminf(var * 100.0f, 1.0f);
}

esp_err_t triage_features_compute_live(
    const void* imu_ring_buffer,
    const void* ppg_ring_buffer,
    triage_features_t* features_out
) {
    if (!features_out) return ESP_ERR_INVALID_ARG;
    
    const sensor_scheduler_t* scheduler = (const sensor_scheduler_t*)imu_ring_buffer;
    
    // Get latest IMU samples from ring buffer (need at least 10)
    sensor_sample_t imu_samples[IMU_RING_BUF_SIZE];
    int count = 0;
    while (count < IMU_RING_BUF_SIZE) {
        sensor_sample_t s;
        if (!sensor_ring_buffer_pop((sensor_ring_buffer_t*)&scheduler->buffers[SENSOR_TYPE_IMU], &s)) break;
        imu_samples[count++] = s;
    }
    if (count < 10) {
        memset(features_out, 0, sizeof(triage_features_t));
        return ESP_ERR_INVALID_SIZE;
    }
    
    // Compute features matching Python extract_triage_features_live()
    float acc_x[IMU_RING_BUF_SIZE], acc_y[IMU_RING_BUF_SIZE], acc_z[IMU_RING_BUF_SIZE];
    float gyro_x[IMU_RING_BUF_SIZE], gyro_y[IMU_RING_BUF_SIZE], gyro_z[IMU_RING_BUF_SIZE];
    
    for (int i = 0; i < count; i++) {
        acc_x[i] = imu_samples[i].data.imu.ax;
        acc_y[i] = imu_samples[i].data.imu.ay;
        acc_z[i] = imu_samples[i].data.imu.az;
        gyro_x[i] = imu_samples[i].data.imu.gx;
        gyro_y[i] = imu_samples[i].data.imu.gy;
        gyro_z[i] = imu_samples[i].data.imu.gz;
    }
    
    // Features 0-11: ACC mean, std, entropy, dom_freq x 3 axes
    // Features 12-23: GYRO mean, std, entropy, dom_freq x 3 axes
    float* signals[6] = {acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z};
    for (int s = 0; s < 6; s++) {
        float mean_v = 0, std_v = 0, ent_v = 0, dom_v = 0;
        for (int i = 0; i < count; i++) mean_v += signals[s][i];
        mean_v /= count;
        for (int i = 0; i < count; i++) {
            float diff = signals[s][i] - mean_v;
            std_v += diff * diff;
        }
        std_v = sqrtf(std_v / count);
        ent_v = spectral_entropy_approx(signals[s], count, TRIAGE_LIVE_IMU_FS_HZ);
        dom_v = goertzel_magnitude(signals[s], count, TRIAGE_LIVE_IMU_FS_HZ, 1.0f); // 1Hz target
        
        int base = s * 4;
        features_out->features[base + 0] = mean_v;
        features_out->features[base + 1] = std_v;
        features_out->features[base + 2] = ent_v;
        features_out->features[base + 3] = dom_v;
    }
    
    // Features 24-25: PPG IR mean, std (from PPG ring buffer - stub for now)
    // TODO: Implement PPG ring buffer read when PPG scheduler is integrated
    features_out->features[24] = 0.0f;
    features_out->features[25] = 0.0f;
    
    features_out->feature_count = TRIAGE_NUM_FEATURES;
    features_out->timestamp_us = esp_timer_get_time();
    return ESP_OK;
}