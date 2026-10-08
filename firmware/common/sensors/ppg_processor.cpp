#include "ppg_processor.h"
#include "ppg_dsp.h"
#include "esp_log.h"
#include <string.h>
#include <math.h>

static const char* TAG = "ppg_processor";

static ppg_processor_ctx_t s_ctx = {};


esp_err_t ppg_processor_init(void) {
    memset(&s_ctx, 0, sizeof(ppg_processor_ctx_t));
    s_ctx.output_every = PPG_PROCESSOR_SAMPLE_RATE_HZ / PPG_PROCESSOR_OUTPUT_HZ;  // 50/10 = 5
    s_ctx.peak_threshold = 0.0f;
    s_ctx.initialized = true;  // was never set: every sample re-ran init and wiped the window
    ESP_LOGI(TAG, "PPG Processor initialized (window=%ds, output=%dHz)", PPG_PROCESSOR_WINDOW_SEC, PPG_PROCESSOR_OUTPUT_HZ);
    return ESP_OK;
}

static esp_err_t compute_features(const float* ir_buffer, size_t count, int64_t timestamp_us,
                                  const ppg_sqi_result_t* sqi, ppg_features_t* features) {
    if (!ir_buffer || !features) return ESP_ERR_INVALID_ARG;
    if (count < PPG_PROCESSOR_WINDOW_SAMPLES / 2) return ESP_ERR_INVALID_SIZE;

    ppg_dsp_result_t r;
    if (ppg_dsp_analyse(ir_buffer, (int)count, &r) != 0) return ESP_ERR_INVALID_ARG;

    memset(features, 0, sizeof(ppg_features_t));
    features->peak_count = r.peak_count;
    features->timestamp_us = timestamp_us;
    features->pi_percent = sqi ? sqi->perfusion_index : 0.0f;
    features->snr_db = r.snr_db;
    if (!r.valid || r.peak_count < PPG_PROCESSOR_MIN_PEAKS) {
        features->valid = false;
        features->motion_artifact = true;
        return ESP_OK;
    }
    features->hr_bpm = r.hr_bpm;
    features->rmssd_ms = r.rmssd_ms;
    features->sdnn_ms = r.sdnn_ms;
    features->ppi_ms = r.ppi_ms;
    features->valid = true;
    features->motion_artifact = sqi && (sqi->sqi < SYNAPSE_T0_PPG_SQI_MIN || sqi->motion_artifact_prob > SYNAPSE_T0_PPG_MAP_MAX);
    return ESP_OK;
}

bool ppg_processor_rate_nominal(void) {
    // The motion gate drops PPG to 16 Hz while moving; the DSP is designed for 50 Hz.
    return !g_sensor_scheduler_ptr ||
           g_sensor_scheduler_ptr->rates_hz[SENSOR_TYPE_PPG] == PPG_PROCESSOR_SAMPLE_RATE_HZ;
}

esp_err_t ppg_processor_process_sample(float red, float ir, int64_t timestamp_us, const ppg_sqi_result_t* sqi, ppg_features_t* features_out) {
    if (!s_ctx.initialized) {
        ppg_processor_init();
    }

    if (!ppg_processor_rate_nominal()) {  // off-rate samples would time-warp the window
        if (s_ctx.count) ppg_processor_reset();
        return ESP_ERR_NOT_FINISHED;
    }
    
    // Add to circular buffer
    s_ctx.ir_buffer[s_ctx.write_idx] = ir;
    s_ctx.red_buffer[s_ctx.write_idx] = red;
    s_ctx.write_idx = (s_ctx.write_idx + 1) % PPG_PROCESSOR_WINDOW_SAMPLES;
    if (s_ctx.count < PPG_PROCESSOR_WINDOW_SAMPLES) {
        s_ctx.count++;
    }
    
    // Decimate output: 50 Hz -> 10 Hz (every 5th sample)
    s_ctx.samples_since_output++;

    if (s_ctx.samples_since_output >= s_ctx.output_every) {
        s_ctx.samples_since_output = 0;
        
        // Reconstruct linear buffer from circular (oldest first; before the ring is
        // full the oldest sample is at index 0, not write_idx)
        float linear_ir[PPG_PROCESSOR_WINDOW_SAMPLES];
        size_t oldest = (s_ctx.count < PPG_PROCESSOR_WINDOW_SAMPLES) ? 0 : s_ctx.write_idx;
        for (size_t i = 0; i < s_ctx.count; i++) {
            size_t idx = (oldest + i) % PPG_PROCESSOR_WINDOW_SAMPLES;
            linear_ir[i] = s_ctx.ir_buffer[idx];
        }
        
        // Use window center as timestamp
        int64_t window_center_ts = timestamp_us - ((int64_t)s_ctx.count / 2) * (1000000 / PPG_PROCESSOR_SAMPLE_RATE_HZ);
        
        esp_err_t ret = compute_features(linear_ir, s_ctx.count, window_center_ts, sqi, &s_ctx.last_features);
        if (ret != ESP_OK) {  // window not half full yet (INVALID_SIZE) or bad args: nothing written
            return ret == ESP_ERR_INVALID_SIZE ? ESP_ERR_NOT_FINISHED : ret;
        }
        if (features_out) *features_out = s_ctx.last_features;
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
    s_ctx.output_every = PPG_PROCESSOR_SAMPLE_RATE_HZ / PPG_PROCESSOR_OUTPUT_HZ;
    s_ctx.initialized = true;
    ESP_LOGD(TAG, "PPG Processor reset");
    return ESP_OK;
}