#include "triage_inference.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "esp_err.h"
#include <string.h>
#include <math.h>

static const char* TAG = "triage_inference";

#define TFLITE_SCHEMA_VERSION 3

// Input quantization parameters (from quantization_metrics.json calibration_stats)
// scale: 0.026773594319820404, zero_point: 8
#define TRIAGE_INPUT_SCALE 0.026773594319820404f
#define TRIAGE_INPUT_ZERO_POINT 8

#if defined(__has_include)
#if __has_include("tensorflow/lite/micro/micro_interpreter.h")
#define SYNAPSE_HAS_TFLM 1
#endif
#endif
#ifndef SYNAPSE_HAS_TFLM
#define SYNAPSE_HAS_TFLM 0
#endif

#if SYNAPSE_HAS_TFLM
#include "model_data.h"
#include <new>
extern "C" {
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "tensorflow/lite/micro/micro_error_reporter.h"
}

static tflite::ErrorReporter* s_error_reporter = nullptr;
static tflite::MicroErrorReporter s_micro_error_reporter;

static inline tflite::MicroInterpreter* to_interpreter(void* p) {
    return static_cast<tflite::MicroInterpreter*>(p);
}
#else
#include <stddef.h>
#endif

esp_err_t triage_inference_init(triage_inference_t* triage) {
    if (!triage) return ESP_ERR_INVALID_ARG;

    memset(triage, 0, sizeof(triage_inference_t));

#if !SYNAPSE_HAS_TFLM
    // TFLM not linked (default for MVP bringup builds). The streaming + sync
    // pipeline must still boot; triage runs as a lightweight heuristic until
    // the INT8 model component is added to the IDF build.
    ESP_LOGW(TAG, "TFLM not linked: triage uses heuristic fallback (streaming unaffected)");
    triage->initialized = true;
    return ESP_OK;
#else
    if (!s_error_reporter) {
        s_error_reporter = &s_micro_error_reporter;
    }

    // Use embedded model data
    const uint8_t* model_data = synapse_triage_tflite;
    size_t model_size = synapse_triage_tflite_len;

    const tflite::Model* model = tflite::GetModel(model_data);
    if (!model) {
        ESP_LOGE(TAG, "Failed to parse TFLite model");
        return ESP_ERR_INVALID_ARG;
    }

    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "Model schema version mismatch: %d != %d", model->version(), TFLITE_SCHEMA_VERSION);
        return ESP_ERR_INVALID_ARG;
    }

    // Op resolver - register only ops used by our CNN model
    static tflite::MicroMutableOpResolver<10> resolver;
    static bool resolver_init = false;
    if (!resolver_init) {
        resolver.AddFullyConnected();
        resolver.AddConv2D();
        resolver.AddDepthwiseConv2D();
        resolver.AddAveragePool2D();
        resolver.AddMaxPool2D();
        resolver.AddSoftmax();
        resolver.AddReshape();
        resolver.AddQuantize();
        resolver.AddDequantize();
        resolver.AddLogistic();
        resolver_init = true;
    }

    // Allocate arena (prefer PSRAM if available for ESP32-S3)
    triage->arena = heap_caps_malloc(TRIAGE_MODEL_ARENA_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!triage->arena) {
        triage->arena = heap_caps_malloc(TRIAGE_MODEL_ARENA_SIZE, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    if (!triage->arena) {
        ESP_LOGE(TAG, "Failed to allocate TFLM arena (%u KB)", (unsigned)(TRIAGE_MODEL_ARENA_SIZE / 1024));
        return ESP_ERR_NO_MEM;
    }
    triage->arena_size = TRIAGE_MODEL_ARENA_SIZE;

    // Create interpreter (placement new inside arena)
    void* interp_mem = triage->arena;
    tflite::MicroInterpreter* interp = new (interp_mem) tflite::MicroInterpreter(
        model, resolver, static_cast<uint8_t*>(triage->arena), triage->arena_size, s_error_reporter);
    if (!interp) {
        ESP_LOGE(TAG, "Failed to create MicroInterpreter");
        heap_caps_free(triage->arena);
        triage->arena = NULL;
        return ESP_ERR_NO_MEM;
    }
    triage->interpreter = interp;

    TfLiteStatus status = interp->AllocateTensors();
    if (status != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors failed");
        interp->~MicroInterpreter();
        heap_caps_free(triage->arena);
        triage->arena = NULL;
        triage->interpreter = NULL;
        return ESP_FAIL;
    }

    triage->input_tensor_idx = interp->inputs()[0];
    triage->output_tensor_idx = interp->outputs()[0];

    TfLiteTensor* input = interp->input(triage->input_tensor_idx);
    TfLiteTensor* output = interp->output(triage->output_tensor_idx);

    ESP_LOGI(TAG, "Triage inference initialized:");
    ESP_LOGI(TAG, "  Model size: %u bytes", (unsigned)model_size);
    ESP_LOGI(TAG, "  Input:  tensor=%d, dims=%d, type=%d", triage->input_tensor_idx, input->dims->size, input->type);
    ESP_LOGI(TAG, "  Output: tensor=%d, dims=%d, type=%d", triage->output_tensor_idx, output->dims->size, output->type);
    for (int i = 0; i < input->dims->size; i++) {
        ESP_LOGI(TAG, "  Input dim[%d] = %d", i, input->dims->data[i]);
    }
    for (int i = 0; i < output->dims->size; i++) {
        ESP_LOGI(TAG, "  Output dim[%d] = %d", i, output->dims->data[i]);
    }
    ESP_LOGI(TAG, "  Arena used: %u / %u bytes", (unsigned)interp->arena_used_bytes(), (unsigned)triage->arena_size);

    triage->model_data = (void*)model_data;
    triage->initialized = true;
    return ESP_OK;
#endif
}

#if SYNAPSE_HAS_TFLM
static void softmax3(const float* logits, float* probs) {
    float max_logit = logits[0];
    for (int i = 1; i < TRIAGE_NUM_CLASSES; i++) {
        if (logits[i] > max_logit) max_logit = logits[i];
    }
    float sum_exp = 0.0f;
    float tmp[TRIAGE_NUM_CLASSES];
    for (int i = 0; i < TRIAGE_NUM_CLASSES; i++) {
        tmp[i] = expf(logits[i] - max_logit);
        sum_exp += tmp[i];
    }
    for (int i = 0; i < TRIAGE_NUM_CLASSES; i++) {
        probs[i] = tmp[i] / sum_exp;
    }
}
#endif

esp_err_t triage_inference_run(triage_inference_t* triage, const triage_input_t* input, triage_output_t* output) {
    if (!triage || !triage->initialized || !input || !output) return ESP_ERR_INVALID_ARG;

    if (input->feature_count != TRIAGE_NUM_FEATURES) {
        ESP_LOGE(TAG, "Feature count mismatch: got %d, expected %d", input->feature_count, TRIAGE_NUM_FEATURES);
        return ESP_ERR_INVALID_SIZE;
    }

    int64_t start_us = esp_timer_get_time();

#if !SYNAPSE_HAS_TFLM
    // Heuristic fallback: high feature energy -> artifact; else baseline.
    // Keeps the 5 Hz triage task meaningful before the INT8 model lands.
    float energy = 0.0f;
    for (int i = 0; i < TRIAGE_NUM_FEATURES; i++) {
        float v = input->features[i];
        energy += v * v;
    }
    energy /= (float)TRIAGE_NUM_FEATURES;
    float artifact = energy > 2.0f ? 0.8f : (energy > 0.5f ? 0.3f : 0.05f);
    float stress = (1.0f - artifact) * 0.3f;
    float baseline = 1.0f - artifact - stress;

    memset(output, 0, sizeof(triage_output_t));
    output->baseline_prob = baseline;
    output->stress_prob = stress;
    output->artifact_prob = artifact;
    output->predicted_class = artifact > 0.5f ? 2 : (stress > baseline ? 1 : 0);
    output->inference_time_us = esp_timer_get_time() - start_us;
    output->valid = true;
    return ESP_OK;
#else
    tflite::MicroInterpreter* interp = to_interpreter(triage->interpreter);
    if (!interp) return ESP_ERR_INVALID_STATE;

    TfLiteTensor* input_tensor = interp->input(triage->input_tensor_idx);
    TfLiteTensor* output_tensor = interp->output(triage->output_tensor_idx);

    int expected_features = 1;
    for (int i = 1; i < input_tensor->dims->size; i++) {
        expected_features *= input_tensor->dims->data[i];
    }

    if (input->feature_count != expected_features) {
        ESP_LOGW(TAG, "Feature count mismatch: got %d, expected %d", input->feature_count, expected_features);
        return ESP_ERR_INVALID_SIZE;
    }

    // Quantize input: float -> int8
    if (input_tensor->type == kTfLiteInt8) {
        int8_t* input_data = input_tensor->data.int8;
        for (int i = 0; i < TRIAGE_NUM_FEATURES; i++) {
            float val = input->features[i];
            int32_t quantized = (int32_t)roundf(val / TRIAGE_INPUT_SCALE + TRIAGE_INPUT_ZERO_POINT);
            if (quantized > 127) quantized = 127;
            if (quantized < -128) quantized = -128;
            input_data[i] = (int8_t)quantized;
        }
    } else if (input_tensor->type == kTfLiteFloat32) {
        float* input_data = input_tensor->data.f;
        for (int i = 0; i < TRIAGE_NUM_FEATURES; i++) {
            input_data[i] = input->features[i];
        }
    } else {
        ESP_LOGE(TAG, "Unsupported input tensor type: %d", (int)input_tensor->type);
        return ESP_ERR_NOT_SUPPORTED;
    }

    // Run inference
    TfLiteStatus status = interp->Invoke();
    if (status != kTfLiteOk) {
        ESP_LOGE(TAG, "Invoke failed");
        return ESP_FAIL;
    }

    int64_t end_us = esp_timer_get_time();

    memset(output, 0, sizeof(triage_output_t));
    output->inference_time_us = end_us - start_us;
    output->valid = true;

    // Dequantize output and apply softmax for probabilities
    if (output_tensor->type == kTfLiteInt8) {
        int8_t* output_data = output_tensor->data.int8;
        float scale = output_tensor->params.scale;
        int32_t zero_point = output_tensor->params.zero_point;

        float logits[TRIAGE_NUM_CLASSES];
        for (int i = 0; i < TRIAGE_NUM_CLASSES; i++) {
            logits[i] = ((float)output_data[i] - (float)zero_point) * scale;
        }

        float probs[TRIAGE_NUM_CLASSES];
        softmax3(logits, probs);
        output->baseline_prob = probs[0];
        output->stress_prob = probs[1];
        output->artifact_prob = probs[2];
    } else if (output_tensor->type == kTfLiteFloat32) {
        float* output_data = output_tensor->data.f;
        output->baseline_prob = output_data[0];
        output->stress_prob = output_data[1];
        output->artifact_prob = output_data[2];
    } else {
        ESP_LOGE(TAG, "Unsupported output tensor type: %d", (int)output_tensor->type);
        return ESP_ERR_NOT_SUPPORTED;
    }

    float max_prob = output->baseline_prob;
    output->predicted_class = 0;
    if (output->stress_prob > max_prob) {
        max_prob = output->stress_prob;
        output->predicted_class = 1;
    }
    if (output->artifact_prob > max_prob) {
        output->predicted_class = 2;
    }

    return ESP_OK;
#endif
}

esp_err_t triage_inference_deinit(triage_inference_t* triage) {
    if (!triage) return ESP_ERR_INVALID_ARG;

#if SYNAPSE_HAS_TFLM
    if (triage->initialized && triage->interpreter) {
        to_interpreter(triage->interpreter)->~MicroInterpreter();
        triage->interpreter = NULL;
    }

    if (triage->arena) {
        heap_caps_free(triage->arena);
        triage->arena = NULL;
    }
#else
    triage->interpreter = NULL;
    triage->arena = NULL;
#endif

    triage->initialized = false;
    ESP_LOGI(TAG, "Triage inference deinitialized");
    return ESP_OK;
}

esp_err_t triage_inference_get_model_info(triage_inference_t* triage, size_t* model_size, size_t* arena_used) {
    if (!triage || !triage->initialized) return ESP_ERR_INVALID_STATE;

#if SYNAPSE_HAS_TFLM
    extern const unsigned char synapse_triage_tflite[];
    extern const unsigned int synapse_triage_tflite_len;
    if (model_size) *model_size = synapse_triage_tflite_len;
    if (arena_used) {
        tflite::MicroInterpreter* interp = to_interpreter(triage->interpreter);
        *arena_used = interp ? interp->arena_used_bytes() : 0;
    }
#else
    if (model_size) *model_size = 0;
    if (arena_used) *arena_used = 0;
#endif
    return ESP_OK;
}
