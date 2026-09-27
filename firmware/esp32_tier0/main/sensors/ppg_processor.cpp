#include "ppg_processor.h"
#include "esp_log.h"
#include <string.h>
#include <math.h>

static const char* TAG = "ppg_processor";

static ppg_processor_ctx_t s_ctx = {0};

#define PEAK_MIN_DISTANCE_SAMPLES  (PPG_PROCESSOR_SAMPLE_RATE_HZ * 60 / 180)  // 180 BPM max = 300ms min = ~19 samples at 64Hz
#define PEAK_MAX_DISTANCE_SAMPLES  (PPG_PROCESSOR_SAMPLE_RATE_HZ * 60 / 30)   // 30 BPM min = 2000ms max = ~128 samples at 64Hz

static inline float fast_sqrtf(float x) { return sqrtf(x); }

esp_err_t ppg_processor_init(void) {
    memset(&s_ctx, 0, sizeof(ppg_processor_ctx_t));
    s_ctx.output_every = 6;  // 64/10 = 6.4, start with 6
    s_ctx.peak_threshold = 0.0f;
    ESP_LOGI(TAG, "PPG Processor initialized (window=%ds, output=%dHz)", PPG_PROCESSOR_WINDOW_SEC, PPG_PROCESSOR_OUTPUT_HZ);
    return ESP_OK;
}

static float compute_snr(const float* buffer, size_t count) {
    if (count < 10) return 0.0f;
    
    float mean = 0.0f;
    for (size_t i = 0; i < count; i++) mean += buffer[i];
    mean /= count;
    
    float signal_power = 0.0f, noise_power = 0.0f;
    for (size_t i = 0; i < count; i++) {
        float diff = buffer[i] - mean;
        signal_power += diff * diff;
    }
    signal_power /= count;
    
    // High-frequency noise estimate (diff of adjacent samples)
    for (size_t i = 1; i < count; i++) {
        float diff = buffer[i] - buffer[i-1];
        noise_power += diff * diff;
    }
    noise_power /= (count - 1);
    
    if (noise_power <= 0.0f) return 40.0f;  // Very clean
    
    float snr = 10.0f * log10f(signal_power / noise_power);
    return fmaxf(0.0f, fminf(snr, 50.0f));
}

static void adaptive_threshold_update(const float* buffer, size_t count) {
    // Dynamic threshold: mean + 0.5 * std of positive deflections
    float mean = 0.0f;
    for (size_t i = 0; i < count; i++) mean += buffer[i];
    mean /= count;
    
    float std_sum = 0.0f;
    int pos_count = 0;
    for (size_t i = 0; i < count; i++) {
        float diff = buffer[i] - mean;
        if (diff > 0) {
            std_sum += diff * diff;
            pos_count++;
        }
    }
    
    if (pos_count > 2) {
        float std_pos = fast_sqrtf(std_sum / pos_count);
        s_ctx.peak_threshold = mean + 0.4f * std_pos;
    } else {
        s_ctx.peak_threshold = mean;
    }
}

static int detect_peaks(const float* buffer, size_t count, int* peak_indices, int max_peaks) {
    int found = 0;
    adaptive_threshold_update(buffer, count);
    
    for (size_t i = 1; i < count - 1 && found < max_peaks; i++) {
        if (buffer[i] > buffer[i-1] && buffer[i] > buffer[i+1] && buffer[i] > s_ctx.peak_threshold) {
            // Check minimum distance from last peak
            if (found == 0 || (i - peak_indices[found-1]) >= PEAK_MIN_DISTANCE_SAMPLES) {
                peak_indices[found++] = i;
            }
        }
    }
    return found;
}

static esp_err_t compute_features(const float* ir_buffer, size_t count, int64_t timestamp_us, 
                                  const ppg_sqi_result_t* sqi, ppg_features_t* features) {
    if (count < PPG_PROCESSOR_WINDOW_SAMPLES / 2) return ESP_ERR_INVALID_SIZE;
    
    int peak_indices[PPG_PROCESSOR_WINDOW_SAMPLES / 10];
    int peak_count = detect_peaks(ir_buffer, count, peak_indices, PPG_PROCESSOR_WINDOW_SAMPLES / 10);
    
    if (peak_count < PPG_PROCESSOR_MIN_PEAKS) {
        memset(features, 0, sizeof(ppg_features_t));
        features->valid = false;
        features->motion_artifact = true;
        features->peak_count = peak_count;
        features->timestamp_us = timestamp_us;
        if (sqi) {
            features->snr_db = sqi->perfusion_index > 0 ? 10.0f : 0.0f;
            features->pi_percent = sqi->perfusion_index;
        }
        return ESP_OK;
    }
    
    // Compute PPI intervals in samples
    int intervals[PPG_PROCESSOR_WINDOW_SAMPLES / 10];
    int interval_count = 0;
    float sum_ppi = 0.0f;
    
    for (int i = 1; i < peak_count; i++) {
        int interval = peak_indices[i] - peak_indices[i-1];
        if (interval >= PEAK_MIN_DISTANCE_SAMPLES && interval <= PEAK_MAX_DISTANCE_SAMPLES) {
            intervals[interval_count++] = interval;
            sum_ppi += interval;
        }
    }
    
    if (interval_count < 2) {
        memset(features, 0, sizeof(ppg_features_t));
        features->valid = false;
        features->motion_artifact = true;
        features->peak_count = peak_count;
        features->timestamp_us = timestamp_us;
        return ESP_OK;
    }
    
    float mean_ppi_samples = sum_ppi / interval_count;
    float mean_ppi_ms = (mean_ppi_samples / (float)PPG_PROCESSOR_SAMPLE_RATE_HZ) * 1000.0f;
    float hr_bpm = 60000.0f / mean_ppi_ms;
    
    // RMSSD: sqrt(mean of squared successive differences)
    float rmssd_sum = 0.0f;
    for (int i = 1; i < interval_count; i++) {
        float diff = ((float)intervals[i] - (float)intervals[i-1]) / (float)PPG_PROCESSOR_SAMPLE_RATE_HZ * 1000.0f;
        rmssd_sum += diff * diff;
    }
    float rmssd_ms = fast_sqrtf(rmssd_sum / (interval_count - 1));
    
    // SDNN: standard deviation of all NN intervals
    float sdnn_sum = 0.0f;
    for (int i = 0; i < interval_count; i++) {
        float ppi_ms = (float)intervals[i] / (float)PPG_PROCESSOR_SAMPLE_RATE_HZ * 1000.0f;
        float diff = ppi_ms - mean_ppi_ms;
        sdnn_sum += diff * diff;
    }
    float sdnn_ms = fast_sqrtf(sdnn_sum / interval_count);
    
    // SNR and PI from SQI
    float snr_db = compute_snr(ir_buffer, count);
    float pi_percent = sqi ? sqi->perfusion_index : 0.0f;
    
    // Motion artifact flag from SQI
    bool motion_artifact = sqi && (sqi->sqi < SYNAPSE_T0_PPG_SQI_MIN || sqi->motion_artifact_prob > SYNAPSE_T0_PPG_MAP_MAX);
    
    features->hr_bpm = hr_bpm;
    features->rmssd_ms = rmssd_ms;
    features->sdnn_ms = sdnn_ms;
    features->ppi_ms = mean_ppi_ms;
    features->snr_db = snr_db;
    features->pi_percent = pi_percent;
    features->valid = true;
    features->motion_artifact = motion_artifact;
    features->peak_count = peak_count;
    features->timestamp_us = timestamp_us;
    
    return ESP_OK;
}

esp_err_t ppg_processor_process_sample(float red, float ir, int64_t timestamp_us, const ppg_sqi_result_t* sqi, ppg_features_t* features_out) {
    if (!s_ctx.initialized) {
        ppg_processor_init();
    }
    
    // Add to circular buffer
    s_ctx.ir_buffer[s_ctx.write_idx] = ir;
    s_ctx.red_buffer[s_ctx.write_idx] = red;
    s_ctx.write_idx = (s_ctx.write_idx + 1) % PPG_PROCESSOR_WINDOW_SAMPLES;
    if (s_ctx.count < PPG_PROCESSOR_WINDOW_SAMPLES) {
        s_ctx.count++;
    }
    
    // Decimate output: 64Hz -> 10Hz (every 6.4 samples)
    s_ctx.samples_since_output++;
    int output_interval = (s_ctx.samples_since_output % 2 == 0) ? 7 : 6;  // Alternate 6,7 for average 6.4
    
    if (s_ctx.samples_since_output >= output_interval) {
        s_ctx.samples_since_output = 0;
        
        // Reconstruct linear buffer from circular
        float linear_ir[PPG_PROCESSOR_WINDOW_SAMPLES];
        for (size_t i = 0; i < s_ctx.count; i++) {
            size_t idx = (s_ctx.write_idx + i) % PPG_PROCESSOR_WINDOW_SAMPLES;
            linear_ir[i] = s_ctx.ir_buffer[idx];
        }
        
        // Use window center as timestamp
        int64_t window_center_ts = timestamp_us - ((int64_t)s_ctx.count / 2) * (1000000 / PPG_PROCESSOR_SAMPLE_RATE_HZ);
        
        esp_err_t ret = compute_features(linear_ir, s_ctx.count, window_center_ts, sqi, &s_ctx.last_features);
        if (ret == ESP_OK && features_out) {
            *features_out = s_ctx.last_features;
        }
        return ESP_OK;  // Features computed this call
    }
    
    return ESP_ERR_NOT_FINISHED;
}

esp_err_t ppg_processor_get_latest(ppg_features_t* features_out) {
    if (!features_out) return ESP_ERR_INVALID_ARG;
    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;
    
    *features_out = s_ctx.last_features;
    return ESP_OK;
}

esp_err_t ppg_processor_reset(void) {
    memset(&s_ctx, 0, sizeof(ppg_processor_ctx_t));
    s_ctx.output_every = 6;
    ESP_LOGI(TAG, "PPG Processor reset");
    return ESP_OK;
}