#include "ppg_sqi.h"
#include "esp_log.h"
#include <string.h>
#include <math.h>

static const char* TAG = "ppg_sqi";

static ppg_sqi_ctx_t s_ctx = {0};

#define PPG_SQI_PERFUSION_WEIGHT    0.3f
#define PPG_SQI_ENTROPY_WEIGHT      0.25f
#define PPG_SQI_KURTOSIS_WEIGHT     0.25f
#define PPG_SQI_PEAK_REG_WEIGHT     0.2f

#define PI_VALUE 3.14159265359f

static inline float fast_sqrtf(float x) {
    return sqrtf(x);
}

static inline float fast_log2f(float x) {
    return log2f(x);
}

static float compute_perfusion_index(const float* buffer, size_t count) {
    if (count < 10) return 0.0f;
    
    float sum = 0.0f;
    float min_val = buffer[0];
    float max_val = buffer[0];
    
    for (size_t i = 0; i < count; i++) {
        sum += buffer[i];
        if (buffer[i] < min_val) min_val = buffer[i];
        if (buffer[i] > max_val) max_val = buffer[i];
    }
    
    float dc = sum / count;
    float ac = (max_val - min_val) / 2.0f;
    
    if (dc <= 0.0f) return 0.0f;
    
    float pi = (ac / dc) * 100.0f;  // Percentage
    return pi;
}

static float compute_spectral_entropy(const float* buffer, size_t count) {
    if (count < 32) return 1.0f;  // High entropy = poor quality
    
    // Simple periodogram using autocorrelation
    // We'll use a simplified approach: compute power in bands
    const int n_fft = 64;  // Must be power of 2, <= count
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
    
    // Simple DFT (not FFT, but n_fft=64 is small enough)
    for (int k = 0; k < n_fft / 2; k++) {
        float re = 0.0f, im = 0.0f;
        for (int n = 0; n < n_fft; n++) {
            float angle = 2.0f * PI_VALUE * k * n / n_fft;
            re += real[n] * cosf(angle) + imag[n] * sinf(angle);
            im += -real[n] * sinf(angle) + imag[n] * cosf(angle);
        }
        power[k] = re * re + im * im;
    }
    
    // Compute spectral entropy
    float total_power = 0.0f;
    for (int k = 0; k < n_fft / 2; k++) {
        total_power += power[k];
    }
    
    if (total_power <= 0.0f) return 1.0f;
    
    float entropy = 0.0f;
    for (int k = 0; k < n_fft / 2; k++) {
        float p = power[k] / total_power;
        if (p > 0.0f) {
            entropy -= p * fast_log2f(p);
        }
    }
    
    // Normalize: max entropy for n_fft/2 bins is log2(n_fft/2)
    float max_entropy = fast_log2f(n_fft / 2);
    float normalized_entropy = entropy / max_entropy;
    
    return normalized_entropy;  // 0 = tonal (good), 1 = noise-like (bad)
}

static float compute_kurtosis(const float* buffer, size_t count) {
    if (count < 10) return 0.0f;
    
    float sum = 0.0f, sum2 = 0.0f, sum4 = 0.0f;
    for (size_t i = 0; i < count; i++) {
        sum += buffer[i];
    }
    float mean = sum / count;
    
    for (size_t i = 0; i < count; i++) {
        float diff = buffer[i] - mean;
        float diff2 = diff * diff;
        sum2 += diff2;
        sum4 += diff2 * diff2;
    }
    
    float variance = sum2 / count;
    if (variance <= 0.0f) return 0.0f;
    
    float kurtosis = (sum4 / count) / (variance * variance) - 3.0f;  // Excess kurtosis
    
    // Normalize: Gaussian = 0, impulsive = high
    // Map to [0, 1] where 1 = high quality (Gaussian-like)
    float normalized = 1.0f / (1.0f + fabsf(kurtosis) * 0.5f);
    return normalized;
}

static float compute_peak_regularity(const float* buffer, size_t count) {
    if (count < 50) return 0.0f;
    
    // Find peaks in the IR signal (more stable)
    int peaks[10];
    int peak_count = 0;
    
    for (size_t i = 1; i < count - 1 && peak_count < 10; i++) {
        if (buffer[i] > buffer[i-1] && buffer[i] > buffer[i+1]) {
            peaks[peak_count++] = i;
        }
    }
    
    if (peak_count < 3) return 0.0f;
    
    // Compute intervals between peaks
    float intervals[9];
    int interval_count = 0;
    for (int i = 1; i < peak_count; i++) {
        intervals[interval_count++] = (float)(peaks[i] - peaks[i-1]);
    }
    
    // Compute coefficient of variation of intervals
    float sum = 0.0f;
    for (int i = 0; i < interval_count; i++) {
        sum += intervals[i];
    }
    float mean_interval = sum / interval_count;
    
    float sum_sq = 0.0f;
    for (int i = 0; i < interval_count; i++) {
        float diff = intervals[i] - mean_interval;
        sum_sq += diff * diff;
    }
    float std_interval = fast_sqrtf(sum_sq / interval_count);
    
    if (mean_interval <= 0.0f) return 0.0f;
    
    float cv = std_interval / mean_interval;
    
    // Regular peaks: CV < 0.1 is good, > 0.3 is bad
    float regularity = 1.0f / (1.0f + cv * 10.0f);
    return regularity;
}

esp_err_t ppg_sqi_init(void) {
    memset(&s_ctx, 0, sizeof(ppg_sqi_ctx_t));
    s_ctx.output_every = 1;  // Process every sample, output at 100Hz via decimation
    ESP_LOGI(TAG, "PPG SQI initialized (window=%d samples, output_rate=%dHz)", 
             PPG_SQI_WINDOW_SIZE, PPG_SQI_OUTPUT_RATE_HZ);
    return ESP_OK;
}

esp_err_t ppg_sqi_process_sample(float red, float ir, int64_t timestamp_us, ppg_sqi_result_t* result) {
    if (!s_ctx.initialized) {
        ppg_sqi_init();
    }
    
    // Add to circular buffer
    s_ctx.red_buffer[s_ctx.write_idx] = red;
    s_ctx.ir_buffer[s_ctx.write_idx] = ir;
    s_ctx.write_idx = (s_ctx.write_idx + 1) % PPG_SQI_WINDOW_SIZE;
    if (s_ctx.count < PPG_SQI_WINDOW_SIZE) {
        s_ctx.count++;
    }
    
    // We have enough data to compute SQI
    if (s_ctx.count >= PPG_SQI_WINDOW_SIZE / 2) {  // Minimum 2s of data
        s_ctx.samples_since_output++;
        
        // Output at 100Hz (every 0.64 samples at 64Hz -> decimate)
        if (s_ctx.samples_since_output >= 1) {  // Every PPG sample for now
            s_ctx.samples_since_output = 0;
            
            // Use IR channel for quality metrics (more stable)
            float pi = compute_perfusion_index(s_ctx.ir_buffer, s_ctx.count);
            float entropy = compute_spectral_entropy(s_ctx.ir_buffer, s_ctx.count);
            float kurtosis = compute_kurtosis(s_ctx.ir_buffer, s_ctx.count);
            float peak_reg = compute_peak_regularity(s_ctx.ir_buffer, s_ctx.count);
            
            // Weighted combination (Karlen 2013 inspired)
            // Higher is better for all components
            float sqi = PPG_SQI_PERFUSION_WEIGHT * (pi / 10.0f) +  // Normalize PI: 10% = 1.0
                        PPG_SQI_ENTROPY_WEIGHT * (1.0f - entropy) +   // Lower entropy = better
                        PPG_SQI_KURTOSIS_WEIGHT * kurtosis +
                        PPG_SQI_PEAK_REG_WEIGHT * peak_reg;
            
            // Clamp to [0, 1]
            if (sqi < 0.0f) sqi = 0.0f;
            if (sqi > 1.0f) sqi = 1.0f;
            
            // Motion artifact probability: inverse of quality components
            float map = 1.0f - (PPG_SQI_ENTROPY_WEIGHT * (1.0f - entropy) + 
                                PPG_SQI_PEAK_REG_WEIGHT * peak_reg);
            if (map < 0.0f) map = 0.0f;
            if (map > 1.0f) map = 1.0f;
            
            s_ctx.last_result.sqi = sqi;
            s_ctx.last_result.perfusion_index = pi;
            s_ctx.last_result.motion_artifact_prob = map;
            s_ctx.last_result.timestamp_us = timestamp_us;
        }
    } else {
        // Not enough data yet
        s_ctx.last_result.sqi = 0.0f;
        s_ctx.last_result.perfusion_index = 0.0f;
        s_ctx.last_result.motion_artifact_prob = 1.0f;
        s_ctx.last_result.timestamp_us = timestamp_us;
    }
    
    if (result) {
        *result = s_ctx.last_result;
    }
    
    return ESP_OK;
}

esp_err_t ppg_sqi_get_latest(ppg_sqi_result_t* result) {
    if (!result) return ESP_ERR_INVALID_ARG;
    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;
    
    *result = s_ctx.last_result;
    return ESP_OK;
}

esp_err_t ppg_sqi_reset(void) {
    memset(&s_ctx, 0, sizeof(ppg_sqi_ctx_t));
    s_ctx.output_every = 1;
    ESP_LOGI(TAG, "PPG SQI reset");
    return ESP_OK;
}