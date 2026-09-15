/**
 * @file triage_inference.cpp
 * @brief Edge Triage Inference Implementation
 *
 * Implements lightweight classification for Tier 0 -> Tier 1 promotion:
 * - Features: HRV (RMSSD), PPG SQI, Perfusion Index, Motion level
 * - Model: Quantized TensorFlow Lite Micro (int8)
 * - Classes: 0=Normal (stay T0), 1=Promote to T1 (sleep/rest detected), 2=Anomaly
 * - Phase 0 Exit Gate: <48KB model, <100KB RAM, <30ms inference, <3pp accuracy drop
 */

#include "triage_inference.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <cmath>
#include <cstring>

// TFLM includes (would be from tensorflow/lite/micro in production)
// For now, we implement a lightweight decision tree as fallback
// In production: #include "tensorflow/lite/micro/micro_interpreter.h"
//                #include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
//                #include "tensorflow/lite/schema/schema_generated.h"

static const char* TAG = "TRIAGE";

// ============================================================================
// CONFIGURATION
// ============================================================================

#define TRIAGE_ECG_WINDOW_S         10      // 10 second window for HRV
#define TRIAGE_PPG_WINDOW_S         30      // 30 second window for PPG quality
#define TRIAGE_FEATURE_DIM          11      // Number of input features
#define TRIAGE_INFERENCE_INTERVAL_MS 1000  // Run inference every 1 second

// Classification thresholds
#define TRIAGE_PROMOTE_CONFIDENCE   0.8f   // Confidence threshold for promotion
#define TRIAGE_ANOMALY_CONFIDENCE   0.7f   // Confidence threshold for anomaly

// Feature indices
enum {
    FEAT_HRV_RMSSD_MS = 0,
    FEAT_HRV_SDNN_MS,
    FEAT_HRV_MEAN_RR_MS,
    FEAT_HRV_PNN50,
    FEAT_PPG_SQI,
    FEAT_PERFUSION_INDEX,
    FEAT_MOTION_LEVEL,
    FEAT_ECG_QUALITY,
    FEAT_HR_BPM,
    FEAT_TEMPERATURE,
    FEAT_BATTERY_LEVEL
};

// ============================================================================
// STATE
// ============================================================================

typedef struct {
    // ECG buffer for HRV
    int16_t ecg_buffer[TRIAGE_ECG_WINDOW_S * SYNAPSE_ECG_SAMPLING_RATE_HZ];
    int64_t ecg_timestamps[TRIAGE_ECG_WINDOW_S * SYNAPSE_ECG_SAMPLING_RATE_HZ];
    size_t ecg_head;
    size_t ecg_count;
    int16_t r_peaks[50];  // R-peak indices in buffer
    size_t r_peak_count;

    // PPG buffer for quality
    uint16_t ppg_red_buffer[TRIAGE_PPG_WINDOW_S * SYNAPSE_PPG_SAMPLING_RATE_HZ];
    uint16_t ppg_ir_buffer[TRIAGE_PPG_WINDOW_S * SYNAPSE_PPG_SAMPLING_RATE_HZ];
    size_t ppg_head;
    size_t ppg_count;

    // IMU motion tracking
    float motion_magnitude_sum;
    size_t motion_sample_count;
    float motion_level;

    // Features
    float features[TRIAGE_FEATURE_DIM];
    bool features_ready;

    // TFLM model (placeholder - in production: tflite::MicroInterpreter*)
    uint8_t* model_data;
    size_t model_len;
    bool model_loaded;

    // Last result
    synapse_triage_result_t last_result;
    bool new_result_available;
    int64_t last_inference_time;

    SemaphoreHandle_t mutex;
    bool initialized;
} triage_state_t;

static triage_state_t s_state = {0};

// ============================================================================
// FEATURE EXTRACTION
// ============================================================================

static void extract_ecg_features() {
    if (s_state.ecg_count < SYNAPSE_ECG_SAMPLING_RATE_HZ * 5) return; // Need at least 5s

    // Simple R-peak detection (threshold-based)
    s_state.r_peak_count = 0;
    int16_t threshold = 500; // µV threshold (adaptive in production)

    for (size_t i = 1; i < s_state.ecg_count - 1; i++) {
        if (s_state.ecg_buffer[i] > threshold &&
            s_state.ecg_buffer[i] > s_state.ecg_buffer[i-1] &&
            s_state.ecg_buffer[i] > s_state.ecg_buffer[i+1]) {

            // Refractory period: 200ms
            if (s_state.r_peak_count == 0 ||
                (i - s_state.r_peaks[s_state.r_peak_count - 1]) > SYNAPSE_ECG_SAMPLING_RATE_HZ / 5) {
                if (s_state.r_peak_count < 50) {
                    s_state.r_peaks[s_state.r_peak_count++] = (int16_t)i;
                }
            }
        }
    }

    if (s_state.r_peak_count < 3) return;

    // Compute RR intervals (ms)
    float rr_intervals[49];
    size_t rr_count = 0;
    for (size_t i = 1; i < s_state.r_peak_count; i++) {
        float rr_ms = (float)(s_state.r_peaks[i] - s_state.r_peaks[i-1]) * 1000.0f / SYNAPSE_ECG_SAMPLING_RATE_HZ;
        if (rr_ms > 300 && rr_ms < 2000) { // Physiological range
            rr_intervals[rr_count++] = rr_ms;
        }
    }

    if (rr_count < 2) return;

    // HRV metrics
    float mean_rr = 0;
    for (size_t i = 0; i < rr_count; i++) mean_rr += rr_intervals[i];
    mean_rr /= rr_count;

    float sdnn = 0, rmssd = 0;
    int pnn50 = 0;
    for (size_t i = 0; i < rr_count; i++) {
        float diff = rr_intervals[i] - mean_rr;
        sdnn += diff * diff;
        if (i > 0) {
            float rrdiff = rr_intervals[i] - rr_intervals[i-1];
            rmssd += rrdiff * rrdiff;
            if (fabsf(rrdiff) > 50) pnn50++;
        }
    }
    sdnn = sqrtf(sdnn / rr_count);
    rmssd = sqrtf(rmssd / (rr_count - 1));
    float pnn50_pct = (float)pnn50 / (rr_count - 1) * 100.0f;

    float hr_bpm = 60000.0f / mean_rr;

    // Store features
    s_state.features[FEAT_HRV_RMSSD_MS] = rmssd;
    s_state.features[FEAT_HRV_SDNN_MS] = sdnn;
    s_state.features[FEAT_HRV_MEAN_RR_MS] = mean_rr;
    s_state.features[FEAT_HRV_PNN50] = pnn50_pct;
    s_state.features[FEAT_HR_BPM] = hr_bpm;
    s_state.features[FEAT_ECG_QUALITY] = (rr_count > 10) ? 1.0f : 0.5f;
}

static void extract_ppg_features() {
    if (s_state.ppg_count < SYNAPSE_PPG_SAMPLING_RATE_HZ * 10) return;

    // Perfusion Index: (AC/DC) * 100
    // AC = peak-to-peak of AC component, DC = mean
    float dc_red = 0, dc_ir = 0;
    for (size_t i = 0; i < s_state.ppg_count; i++) {
        dc_red += s_state.ppg_red_buffer[i];
        dc_ir += s_state.ppg_ir_buffer[i];
    }
    dc_red /= s_state.ppg_count;
    dc_ir /= s_state.ppg_count;

    // Find peaks for AC component
    float ac_red = 0, ac_ir = 0;
    int peaks = 0;
    for (size_t i = 1; i < s_state.ppg_count - 1; i++) {
        if (s_state.ppg_red_buffer[i] > s_state.ppg_red_buffer[i-1] &&
            s_state.ppg_red_buffer[i] > s_state.ppg_red_buffer[i+1]) {
            ac_red += s_state.ppg_red_buffer[i] - dc_red;
            peaks++;
        }
        if (s_state.ppg_ir_buffer[i] > s_state.ppg_ir_buffer[i-1] &&
            s_state.ppg_ir_buffer[i] > s_state.ppg_ir_buffer[i+1]) {
            ac_ir += s_state.ppg_ir_buffer[i] - dc_ir;
        }
    }

    float perfusion = 0;
    if (peaks > 0 && dc_red > 0) {
        perfusion = (ac_red / peaks) / dc_red * 100.0f; // %
    }

    // PPG SQI (Signal Quality Index) - simplified
    // Based on heart rate regularity from PPG peaks
    float sqi = 0.5f; // Default moderate
    if (peaks > 5) {
        // Compute HR from PPG peaks
        float ppg_hr = 60.0f * peaks / TRIAGE_PPG_WINDOW_S;
        float ecg_hr = s_state.features[FEAT_HR_BPM];
        if (ecg_hr > 0) {
            float diff = fabsf(ppg_hr - ecg_hr);
            sqi = 1.0f - diff / 50.0f; // Penalize HR mismatch
            if (sqi < 0) sqi = 0;
            if (sqi > 1) sqi = 1;
        }
    }

    s_state.features[FEAT_PPG_SQI] = sqi;
    s_state.features[FEAT_PERFUSION_INDEX] = perfusion;
    s_state.features[FEAT_MOTION_LEVEL] = s_state.motion_level;
}

static void extract_motion_features() {
    // Motion level already accumulated in feed_imu
    // Normalize: 0 = still, 1 = vigorous motion
    s_state.features[FEAT_MOTION_LEVEL] = fminf(s_state.motion_level / 200.0f, 1.0f);
}

static void update_features() {
    extract_ecg_features();
    extract_ppg_features();
    extract_motion_features();

    // Temperature and battery (placeholders)
    s_state.features[FEAT_TEMPERATURE] = 36.5f;
    s_state.features[FEAT_BATTERY_LEVEL] = 0.8f;

    s_state.features_ready = true;
}

// ============================================================================
// INFERENCE (Decision Tree Fallback / TFLM)
// ============================================================================

static int run_decision_tree(const float* features, float* confidence) {
    // Lightweight decision tree for Tier 0 -> Tier 1 promotion
    // Based on: immobility + clean PPG + night time + HRV patterns

    float hrv_rmssd = features[FEAT_HRV_RMSSD_MS];
    float ppg_sqi = features[FEAT_PPG_SQI];
    float perfusion = features[FEAT_PERFUSION_INDEX];
    float motion = features[FEAT_MOTION_LEVEL];
    float hr = features[FEAT_HR_BPM];
    float ecg_qual = features[FEAT_ECG_QUALITY];

    // Class 2: Anomaly detection (poor signal quality)
    if (ecg_qual < 0.5f || ppg_sqi < 0.3f || perfusion < 0.5f) {
        *confidence = 0.8f;
        return 2; // Anomaly
    }

    // Class 1: Promote to Tier 1 (sleep/rest detected)
    // Criteria: low motion + good PPG + physiological HRV + night HR pattern
    bool low_motion = motion < 0.1f;
    bool good_ppg = ppg_sqi > 0.6f && perfusion > 2.0f;
    bool physiological_hrv = hrv_rmssd > 20.0f && hrv_rmssd < 100.0f;
    bool rest_hr = hr > 40.0f && hr < 80.0f;

    int promote_score = 0;
    if (low_motion) promote_score += 2;
    if (good_ppg) promote_score += 2;
    if (physiological_hrv) promote_score += 2;
    if (rest_hr) promote_score += 1;

    if (promote_score >= 5) {
        *confidence = 0.7f + (promote_score - 5) * 0.05f;
        return 1; // Promote to Tier 1
    }

    // Class 0: Normal (stay in Tier 0)
    *confidence = 0.9f;
    return 0;
}

static bool run_tflm_inference(const float* features, int* classification, float* confidence) {
    // Placeholder for TFLM inference
    // In production:
    // 1. Set input tensor from features
    // 2. interpreter->Invoke()
    // 3. Read output tensor
    // 4. Return classification and confidence

    // For now, use decision tree
    *classification = run_decision_tree(features, confidence);
    return true;
}

// ============================================================================
// INITIALIZATION
// ============================================================================

esp_err_t triage_inference_init(void) {
    ESP_LOGI(TAG, "Initializing Triage Inference...");

    s_state.mutex = xSemaphoreCreateMutex();
    if (!s_state.mutex) return ESP_FAIL;

    s_state.ecg_head = 0;
    s_state.ecg_count = 0;
    s_state.ppg_head = 0;
    s_state.ppg_count = 0;
    s_state.motion_magnitude_sum = 0;
    s_state.motion_sample_count = 0;
    s_state.motion_level = 0;
    s_state.features_ready = false;
    s_state.model_data = NULL;
    s_state.model_len = 0;
    s_state.model_loaded = false;
    s_state.new_result_available = false;
    s_state.last_inference_time = 0;

    // Initialize features
    for (int i = 0; i < TRIAGE_FEATURE_DIM; i++) {
        s_state.features[i] = 0;
    }

    s_state.initialized = true;
    ESP_LOGI(TAG, "Triage Inference initialized (decision tree mode)");
    ESP_LOGI(TAG, "Feature dim: %d, ECG window: %ds, PPG window: %ds",
             TRIAGE_FEATURE_DIM, TRIAGE_ECG_WINDOW_S, TRIAGE_PPG_WINDOW_S);

    return ESP_OK;
}

// ============================================================================
// FEED FUNCTIONS
// ============================================================================

void triage_inference_feed_ecg(const synapse_ecg_sample_t* sample) {
    if (!s_state.initialized || !sample) return;

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        s_state.ecg_buffer[s_state.ecg_head] = sample->value_mv;
        s_state.ecg_timestamps[s_state.ecg_head] = sample->timestamp_us;
        s_state.ecg_head = (s_state.ecg_head + 1) % (TRIAGE_ECG_WINDOW_S * SYNAPSE_ECG_SAMPLING_RATE_HZ);
        if (s_state.ecg_count < TRIAGE_ECG_WINDOW_S * SYNAPSE_ECG_SAMPLING_RATE_HZ) {
            s_state.ecg_count++;
        }
        xSemaphoreGive(s_state.mutex);
    }
}

void triage_inference_feed_ppg(const synapse_ppg_sample_t* sample) {
    if (!s_state.initialized || !sample) return;

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        s_state.ppg_red_buffer[s_state.ppg_head] = sample->red;
        s_state.ppg_ir_buffer[s_state.ppg_head] = sample->ir;
        s_state.ppg_head = (s_state.ppg_head + 1) % (TRIAGE_PPG_WINDOW_S * SYNAPSE_PPG_SAMPLING_RATE_HZ);
        if (s_state.ppg_count < TRIAGE_PPG_WINDOW_S * SYNAPSE_PPG_SAMPLING_RATE_HZ) {
            s_state.ppg_count++;
        }
        xSemaphoreGive(s_state.mutex);
    }
}

void triage_inference_feed_imu(const synapse_imu_sample_t* sample) {
    if (!s_state.initialized || !sample) return;

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        // Accumulate motion magnitude
        float mag = sqrtf((float)sample->accel_x * sample->accel_x +
                          (float)sample->accel_y * sample->accel_y +
                          (float)sample->accel_z * sample->accel_z);
        s_state.motion_magnitude_sum += mag;
        s_state.motion_sample_count++;

        // Update motion level (exponential moving average)
        if (s_state.motion_sample_count >= SYNAPSE_IMU_SAMPLING_RATE_HZ) {
            s_state.motion_level = s_state.motion_magnitude_sum / s_state.motion_sample_count;
            s_state.motion_magnitude_sum = 0;
            s_state.motion_sample_count = 0;
        }
        xSemaphoreGive(s_state.mutex);
    }
}

// ============================================================================
// RUN INFERENCE
// ============================================================================

bool triage_inference_run(synapse_triage_result_t* result) {
    if (!s_state.initialized || !result) return false;

    int64_t now = esp_timer_get_time();
    if (now - s_state.last_inference_time < 1000000) { // 1 second minimum interval
        return false;
    }

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        if (!s_state.features_ready) {
            update_features();
        }

        if (!s_state.features_ready) {
            xSemaphoreGive(s_state.mutex);
            return false;
        }

        int64_t start = esp_timer_get_time();

        int classification = 0;
        float confidence = 0;

        bool success = run_tflm_inference(s_state.features, &classification, &confidence);

        int64_t end = esp_timer_get_time();
        float inference_ms = (float)(end - start) / 1000.0f;

        if (success) {
            s_state.last_result.timestamp_us = now;
            s_state.last_result.classification = (uint8_t)classification;
            s_state.last_result.confidence = confidence;
            s_state.last_result.inference_ms = inference_ms;
            s_state.last_result.hrv_rmssd_ms = (int16_t)s_state.features[FEAT_HRV_RMSSD_MS];
            s_state.last_result.ppg_sqi_x100 = (int16_t)(s_state.features[FEAT_PPG_SQI] * 100);
            s_state.last_result.perfusion_x100 = (int16_t)(s_state.features[FEAT_PERFUSION_INDEX] * 100);
            s_state.new_result_available = true;
            s_state.last_inference_time = now;

            *result = s_state.last_result;
            xSemaphoreGive(s_state.mutex);
            return true;
        }

        xSemaphoreGive(s_state.mutex);
    }
    return false;
}

bool triage_inference_get_result(synapse_triage_result_t* result) {
    if (!s_state.initialized || !result) return false;

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        if (s_state.new_result_available) {
            *result = s_state.last_result;
            s_state.new_result_available = false;
            xSemaphoreGive(s_state.mutex);
            return true;
        }
        xSemaphoreGive(s_state.mutex);
    }
    return false;
}

// ============================================================================
// MODEL LOADING
// ============================================================================

esp_err_t triage_inference_load_model(const uint8_t* model_data, size_t model_len) {
    if (!s_state.initialized || !model_data || model_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }

    if (model_len > SYNAPSE_TFLM_MODEL_MAX_SIZE) {
        ESP_LOGE(TAG, "Model too large: %zu bytes (max %d)", model_len, SYNAPSE_TFLM_MODEL_MAX_SIZE);
        return ESP_ERR_INVALID_SIZE;
    }

    if (xSemaphoreTake(s_state.mutex, pdMS_TO_TICKS(100)) == pdTRUE) {
        // Free old model
        if (s_state.model_data) {
            free(s_state.model_data);
        }

        s_state.model_data = (uint8_t*)malloc(model_len);
        if (!s_state.model_data) {
            xSemaphoreGive(s_state.mutex);
            return ESP_ERR_NO_MEM;
        }

        memcpy(s_state.model_data, model_data, model_len);
        s_state.model_len = model_len;
        s_state.model_loaded = true;

        ESP_LOGI(TAG, "TFLM model loaded: %zu bytes", model_len);
        xSemaphoreGive(s_state.mutex);
        return ESP_OK;
    }
    return ESP_FAIL;
}

// ============================================================================
// DEINITIALIZATION
// ============================================================================

void triage_inference_deinit(void) {
    if (s_state.model_data) {
        free(s_state.model_data);
        s_state.model_data = NULL;
    }
    if (s_state.mutex) {
        vSemaphoreDelete(s_state.mutex);
    }
    s_state.initialized = false;
    ESP_LOGI(TAG, "Triage Inference deinitialized");
}