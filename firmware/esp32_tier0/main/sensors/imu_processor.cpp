#include "imu_processor.h"
#include "esp_log.h"
#include <string.h>
#include <math.h>

static const char* TAG = "imu_processor";

static imu_processor_ctx_t s_ctx = {0};

#define PI_VALUE 3.14159265359f

// Sleep/wake logistic regression coefficients (trained on literature data)
// Features: [motion_intensity, spectral_entropy, dominant_freq, tilt_variance]
// These are placeholder values - will be calibrated from real data
static const float SLEEP_WEIGHTS[5] = {
    -3.2f,    // bias
    -12.0f,   // motion_intensity (higher motion -> less sleep)
    8.5f,     // spectral_entropy (lower entropy -> more sleep)
    -4.0f,    // dominant_freq (lower freq -> more sleep)
    -2.0f     // tilt_variance (lower variance -> more sleep)
};

static inline float fast_sqrtf(float x) { return sqrtf(x); }

esp_err_t imu_processor_init(void) {
    memset(&s_ctx, 0, sizeof(imu_processor_ctx_t));
    s_ctx.samples_since_output = 0;
    ESP_LOGI(TAG, "IMU Processor initialized (window=%ds, output=%dHz)", IMU_PROCESSOR_WINDOW_SEC, IMU_PROCESSOR_OUTPUT_HZ);
    return ESP_OK;
}

static float compute_rms(const float* buffer, size_t count) {
    if (count == 0) return 0.0f;
    float sum_sq = 0.0f;
    for (size_t i = 0; i < count; i++) {
        sum_sq += buffer[i] * buffer[i];
    }
    return fast_sqrtf(sum_sq / count);
}

static float compute_mean(const float* buffer, size_t count) {
    if (count == 0) return 0.0f;
    float sum = 0.0f;
    for (size_t i = 0; i < count; i++) sum += buffer[i];
    return sum / count;
}

static float compute_std(const float* buffer, size_t count, float mean) {
    if (count < 2) return 0.0f;
    float sum_sq = 0.0f;
    for (size_t i = 0; i < count; i++) {
        float diff = buffer[i] - mean;
        sum_sq += diff * diff;
    }
    return fast_sqrtf(sum_sq / count);
}

static float compute_spectral_entropy(const float* buffer, size_t count, float fs) {
    if (count < 32) return 1.0f;
    
    const int n_fft = 64;
    if (count < n_fft) return 1.0f;
    
    float real[n_fft];
    float imag[n_fft];
    float power[n_fft / 2];
    
    // Apply Hann window and copy last n_fft samples
    for (int i = 0; i < n_fft; i++) {
        size_t idx = (count - n_fft + i) % count;
        float window = 0.5f * (1.0f - cosf(2.0f * PI_VALUE * i / (n_fft - 1)));
        real[i] = buffer[idx] * window;
        imag[i] = 0.0f;
    }
    
    // Simple DFT
    for (int k = 0; k < n_fft / 2; k++) {
        float re = 0.0f, im = 0.0f;
        for (int n = 0; n < n_fft; n++) {
            float angle = 2.0f * PI_VALUE * k * n / n_fft;
            re += real[n] * cosf(angle) + imag[n] * sinf(angle);
            im += -real[n] * sinf(angle) + imag[n] * cosf(angle);
        }
        power[k] = re * re + im * im;
    }
    
    float total_power = 0.0f;
    for (int k = 0; k < n_fft / 2; k++) total_power += power[k];
    
    if (total_power <= 0.0f) return 1.0f;
    
    float entropy = 0.0f;
    for (int k = 0; k < n_fft / 2; k++) {
        float p = power[k] / total_power;
        if (p > 0.0f) entropy -= p * log2f(p);
    }
    
    float max_entropy = log2f(n_fft / 2);
    return entropy / max_entropy;
}

static float compute_dominant_frequency(const float* buffer, size_t count, float fs) {
    if (count < 32) return 0.0f;
    
    const int n_fft = 64;
    if (count < n_fft) return 0.0f;
    
    float real[n_fft];
    float imag[n_fft];
    float power[n_fft / 2];
    
    for (int i = 0; i < n_fft; i++) {
        size_t idx = (count - n_fft + i) % count;
        float window = 0.5f * (1.0f - cosf(2.0f * PI_VALUE * i / (n_fft - 1)));
        real[i] = buffer[idx] * window;
        imag[i] = 0.0f;
    }
    
    for (int k = 0; k < n_fft / 2; k++) {
        float re = 0.0f, im = 0.0f;
        for (int n = 0; n < n_fft; n++) {
            float angle = 2.0f * PI_VALUE * k * n / n_fft;
            re += real[n] * cosf(angle) + imag[n] * sinf(angle);
            im += -real[n] * sinf(angle) + imag[n] * cosf(angle);
        }
        power[k] = re * re + im * im;
    }
    
    // Find peak in 0.5-3 Hz range (sleep-relevant frequencies)
    int min_bin = (int)(0.5f * n_fft / fs);
    int max_bin = (int)(3.0f * n_fft / fs);
    min_bin = fmaxf(1, min_bin);
    max_bin = fminf(n_fft / 2 - 1, max_bin);
    
    float max_power = 0.0f;
    int max_bin_idx = 0;
    for (int k = min_bin; k <= max_bin; k++) {
        if (power[k] > max_power) {
            max_power = power[k];
            max_bin_idx = k;
        }
    }
    
    return (float)max_bin_idx * fs / n_fft;
}

static float compute_tilt_angles(const float* ax, const float* ay, const float* az, 
                                 size_t count, float* tilt_x, float* tilt_y, float* tilt_z) {
    // Static tilt from accelerometer (when stationary)
    float mean_ax = compute_mean(ax, count);
    float mean_ay = compute_mean(ay, count);
    float mean_az = compute_mean(az, count);
    
    float norm = fast_sqrtf(mean_ax*mean_ax + mean_ay*mean_ay + mean_az*mean_az);
    if (norm <= 0.01f) return 0.0f;
    
    *tilt_x = atan2f(mean_ay, mean_az) * 180.0f / PI_VALUE;
    *tilt_y = atan2f(-mean_ax, fast_sqrtf(mean_ay*mean_ay + mean_az*mean_az)) * 180.0f / PI_VALUE;
    *tilt_z = atan2f(mean_ay, mean_ax) * 180.0f / PI_VALUE;
    
    // Compute tilt variance (stationarity indicator)
    float tilt_x_vals[IMU_PROCESSOR_WINDOW_SAMPLES];
    float tilt_y_vals[IMU_PROCESSOR_WINDOW_SAMPLES];
    for (size_t i = 0; i < count; i++) {
        float n = fast_sqrtf(ax[i]*ax[i] + ay[i]*ay[i] + az[i]*az[i]);
        if (n > 0.01f) {
            tilt_x_vals[i] = atan2f(ay[i], az[i]) * 180.0f / PI_VALUE;
            tilt_y_vals[i] = atan2f(-ax[i], fast_sqrtf(ay[i]*ay[i] + az[i]*az[i])) * 180.0f / PI_VALUE;
        } else {
            tilt_x_vals[i] = *tilt_x;
            tilt_y_vals[i] = *tilt_y;
        }
    }
    
    float var_x = compute_std(tilt_x_vals, count, *tilt_x);
    float var_y = compute_std(tilt_y_vals, count, *tilt_y);
    
    return fast_sqrtf(var_x * var_x + var_y * var_y);  // Tilt variance magnitude
}

float imu_sleep_wake_classify(const imu_features_t* features) {
    if (!features) return 0.5f;
    
    // Logistic regression: p = 1 / (1 + exp(-(bias + sum(w_i * x_i))))
    float logit = SLEEP_WEIGHTS[0]  // bias
                + SLEEP_WEIGHTS[1] * features->motion_intensity
                + SLEEP_WEIGHTS[2] * features->spectral_entropy
                + SLEEP_WEIGHTS[3] * features->dominant_freq_hz
                + SLEEP_WEIGHTS[4] * features->tilt_z_deg;  // Using tilt_z as proxy for variance
    
    float prob = 1.0f / (1.0f + expf(-logit));
    return fmaxf(0.0f, fminf(1.0f, prob));
}

esp_err_t imu_processor_process_sample(float ax, float ay, float az, 
                                       float gx, float gy, float gz,
                                       int64_t timestamp_us, imu_features_t* features_out) {
    if (!s_ctx.initialized) {
        imu_processor_init();
    }
    
    // Add to circular buffers
    s_ctx.ax_buffer[s_ctx.write_idx] = ax;
    s_ctx.ay_buffer[s_ctx.write_idx] = ay;
    s_ctx.az_buffer[s_ctx.write_idx] = az;
    s_ctx.gx_buffer[s_ctx.write_idx] = gx;
    s_ctx.gy_buffer[s_ctx.write_idx] = gy;
    s_ctx.gz_buffer[s_ctx.write_idx] = gz;
    s_ctx.write_idx = (s_ctx.write_idx + 1) % IMU_PROCESSOR_WINDOW_SAMPLES;
    if (s_ctx.count < IMU_PROCESSOR_WINDOW_SAMPLES) {
        s_ctx.count++;
    }
    
    // Output at 1Hz (every 100 samples)
    s_ctx.samples_since_output++;
    if (s_ctx.samples_since_output >= IMU_PROCESSOR_SAMPLE_RATE_HZ / IMU_PROCESSOR_OUTPUT_HZ) {
        s_ctx.samples_since_output = 0;
        
        if (s_ctx.count < IMU_PROCESSOR_WINDOW_SAMPLES / 2) {
            return ESP_ERR_NOT_FINISHED;
        }
        
        // Reconstruct linear buffers from circular
        float ax_lin[IMU_PROCESSOR_WINDOW_SAMPLES];
        float ay_lin[IMU_PROCESSOR_WINDOW_SAMPLES];
        float az_lin[IMU_PROCESSOR_WINDOW_SAMPLES];
        float gx_lin[IMU_PROCESSOR_WINDOW_SAMPLES];
        float gy_lin[IMU_PROCESSOR_WINDOW_SAMPLES];
        float gz_lin[IMU_PROCESSOR_WINDOW_SAMPLES];
        
        for (size_t i = 0; i < s_ctx.count; i++) {
            size_t idx = (s_ctx.write_idx + i) % IMU_PROCESSOR_WINDOW_SAMPLES;
            ax_lin[i] = s_ctx.ax_buffer[idx];
            ay_lin[i] = s_ctx.ay_buffer[idx];
            az_lin[i] = s_ctx.az_buffer[idx];
            gx_lin[i] = s_ctx.gx_buffer[idx];
            gy_lin[i] = s_ctx.gy_buffer[idx];
            gz_lin[i] = s_ctx.gz_buffer[idx];
        }
        
        // Compute features
        imu_features_t features = {0};
        features.timestamp_us = timestamp_us - ((int64_t)s_ctx.count / 2) * (1000000 / IMU_PROCESSOR_SAMPLE_RATE_HZ);
        
        // RMS acceleration and gyro
        features.acc_rms_x = compute_rms(ax_lin, s_ctx.count);
        features.acc_rms_y = compute_rms(ay_lin, s_ctx.count);
        features.acc_rms_z = compute_rms(az_lin, s_ctx.count);
        features.gx_rms = compute_rms(gx_lin, s_ctx.count);
        features.gy_rms = compute_rms(gy_lin, s_ctx.count);
        features.gz_rms = compute_rms(gz_lin, s_ctx.count);
        
        // Total motion intensity (vector magnitude RMS)
        float vm_rms_sum = 0.0f;
        for (size_t i = 0; i < s_ctx.count; i++) {
            float vm = fast_sqrtf(ax_lin[i]*ax_lin[i] + ay_lin[i]*ay_lin[i] + az_lin[i]*az_lin[i]);
            vm_rms_sum += vm * vm;
        }
        features.motion_intensity = fast_sqrtf(vm_rms_sum / s_ctx.count);
        
        // Tilt angles and variance
        float tilt_var = compute_tilt_angles(ax_lin, ay_lin, az_lin, s_ctx.count,
                                             &features.tilt_x_deg, &features.tilt_y_deg, &features.tilt_z_deg);
        
        // Spectral entropy (on acceleration magnitude)
        float acc_mag[IMU_PROCESSOR_WINDOW_SAMPLES];
        for (size_t i = 0; i < s_ctx.count; i++) {
            acc_mag[i] = fast_sqrtf(ax_lin[i]*ax_lin[i] + ay_lin[i]*ay_lin[i] + az_lin[i]*az_lin[i]);
        }
        features.spectral_entropy = compute_spectral_entropy(acc_mag, s_ctx.count, IMU_PROCESSOR_SAMPLE_RATE_HZ);
        
        // Dominant frequency in 0.5-3 Hz band
        features.dominant_freq_hz = compute_dominant_frequency(acc_mag, s_ctx.count, IMU_PROCESSOR_SAMPLE_RATE_HZ);
        
        // Sleep probability
        features.sleep_probability = imu_sleep_wake_classify(&features);
        
        // Stationary detection: low motion intensity AND low tilt variance
        features.is_stationary = (features.motion_intensity < 0.05f) && (tilt_var < 2.0f);
        
        s_ctx.last_features = features;
        if (features_out) *features_out = features;
        
        ESP_LOGD(TAG, "IMU features: motion=%.3fg, entropy=%.3f, dom_freq=%.2fHz, sleep_prob=%.3f, stationary=%d",
                 features.motion_intensity, features.spectral_entropy, features.dominant_freq_hz,
                 features.sleep_probability, features.is_stationary);
        
        return ESP_OK;
    }
    
    return ESP_ERR_NOT_FINISHED;
}

esp_err_t imu_processor_get_latest(imu_features_t* features_out) {
    if (!features_out) return ESP_ERR_INVALID_ARG;
    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;
    
    *features_out = s_ctx.last_features;
    return ESP_OK;
}

esp_err_t imu_processor_reset(void) {
    memset(&s_ctx, 0, sizeof(imu_processor_ctx_t));
    ESP_LOGI(TAG, "IMU Processor reset");
    return ESP_OK;
}