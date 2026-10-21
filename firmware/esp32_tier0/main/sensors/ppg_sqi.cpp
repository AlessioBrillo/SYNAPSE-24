#include "ppg_sqi.h"
#include "esp_log.h"
#include <string.h>
#include <math.h>

static const char* TAG = "ppg_sqi";

static ppg_sqi_ctx_t s_ctx = {};

#define PPG_SQI_PERFUSION_WEIGHT    0.25f
#define PPG_SQI_ENTROPY_WEIGHT      0.25f
#define PPG_SQI_SNR_WEIGHT          0.30f
#define PPG_SQI_PEAK_REG_WEIGHT     0.20f

// Sampling rate of the PPG stream the SQI is computed on (config/hardware.yaml:
// the MAX30102 runs at 50 Hz). It sets the frequency resolution of the
// periodogram, and therefore which bins belong to the cardiac band.
#define PPG_SQI_INPUT_RATE_HZ       50.0f

// Cardiac band used by the spectral SNR feature (Hz).
#define PPG_SQI_BAND_LOW_HZ         0.5f
#define PPG_SQI_BAND_HIGH_HZ        8.0f

// Size of the periodogram (must be a power of two) and its number of bins.
#define PPG_SQI_FFT_SIZE             64
#define PPG_SQI_FFT_BINS             (PPG_SQI_FFT_SIZE / 2)

#define PI_VALUE 3.14159265359f

static inline float fast_sqrtf(float x) {
    return sqrtf(x);
}

static inline float fast_log2f(float x) {
    return log2f(x);
}

static inline float clamp01f(float x) {
    if (x < 0.0f) return 0.0f;
    if (x > 1.0f) return 1.0f;
    return x;
}

/**
 * @brief Periodogram of the last @p n_fft samples (Hann window, simple DFT).
 *
 * @return false when there is not enough data (the caller then reports the
 *         "not measurable" value of its feature).
 */
static bool compute_power_spectrum(const float* buffer,
                                    size_t count,
                                    float* power,
                                    int n_fft) {
    if (count < (size_t)n_fft) return false;

    float real[PPG_SQI_FFT_SIZE];
    float imag[PPG_SQI_FFT_SIZE];

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
    return true;
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

    const int n_fft = PPG_SQI_FFT_SIZE;  // Must be power of 2, <= count
    if (count < n_fft) return 1.0f;

    float power[PPG_SQI_FFT_BINS];
    if (!compute_power_spectrum(buffer, count, power, n_fft)) return 1.0f;

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

/**
 * @brief Spectral SNR: the fraction of the power that lives in the cardiac band.
 *
 * A clean pulse train concentrates its power in 0.5-8 Hz and scores near 1;
 * a noise buffer spreads it over the whole spectrum and scores near 0.
 *
 * This feature replaces the kurtosis one, which scored a *Gaussian* noise
 * buffer 1.0 (a Gaussian has zero excess kurtosis): it rewarded the absence
 * of a signal. The host implementation (src/synapse24/signal_quality/ppg.py)
 * uses the same definition, so the device and the host agree.
 */
static float compute_spectral_snr(const float* buffer, size_t count) {
    if (count < 32) return 0.0f;

    const int n_fft = PPG_SQI_FFT_SIZE;
    if (count < n_fft) return 0.0f;

    float power[PPG_SQI_FFT_BINS];
    if (!compute_power_spectrum(buffer, count, power, n_fft)) return 0.0f;

    float in_band = 0.0f;
    float total = 0.0f;
    for (int k = 0; k < n_fft / 2; k++) {
        const float freq = (float)k * PPG_SQI_INPUT_RATE_HZ / (float)n_fft;
        total += power[k];
        if (freq >= PPG_SQI_BAND_LOW_HZ && freq <= PPG_SQI_BAND_HIGH_HZ) {
            in_band += power[k];
        }
    }

    if (total <= 0.0f) return 0.0f;
    return clamp01f(in_band / total);
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
    // The flag was never set: every sample re-ran init and wiped the window,
    // so the SQI never had enough data and the motion gate never saw a value.
    s_ctx.initialized = true;
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
            float snr = compute_spectral_snr(s_ctx.ir_buffer, s_ctx.count);
            float peak_reg = compute_peak_regularity(s_ctx.ir_buffer, s_ctx.count);
            
            // Weighted combination (Karlen 2013 inspired)
            // Higher is better for all components
            float sqi = PPG_SQI_PERFUSION_WEIGHT * clamp01f(pi / 10.0f) +  // Normalize PI: 10% = 1.0
                        PPG_SQI_ENTROPY_WEIGHT * (1.0f - entropy) +        // Lower entropy = better
                        PPG_SQI_SNR_WEIGHT * snr +                        // Power in the cardiac band
                        PPG_SQI_PEAK_REG_WEIGHT * peak_reg;
            
            // Clamp to [0, 1]
            if (sqi < 0.0f) sqi = 0.0f;
            if (sqi > 1.0f) sqi = 1.0f;
            
            // Motion artifact probability: inverse of quality components
            float map = 1.0f - (PPG_SQI_ENTROPY_WEIGHT * (1.0f - entropy) + 
                                PPG_SQI_SNR_WEIGHT * snr +
                                PPG_SQI_PEAK_REG_WEIGHT * peak_reg);
            map = clamp01f(map);
            
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