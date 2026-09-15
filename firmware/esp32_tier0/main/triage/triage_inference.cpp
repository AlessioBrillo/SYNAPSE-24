#include "triage_inference.h"
#include "model_data.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include <string.h>
#include <math.h>

static const char* TAG = "triage_inference";

#define TFLITE_SCHEMA_VERSION 3

extern "C" {
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "tensorflow/lite/micro/micro_error_reporter.h"
}

static tflite::ErrorReporter* s_error_reporter = nullptr;
static tflite::MicroErrorReporter s_micro_error_reporter;

// Input quantization parameters (from quantization_metrics.json calibration_stats)
// scale: 0.026773594319820404, zero_point: 8
#define TRIAGE_INPUT_SCALE 0.026773594319820404f
#define TRIAGE_INPUT_ZERO_POINT 8

// Output dequantization - approximate (will be refined from actual model)
// For softmax output, we use the raw logits and apply softmax
#define TRIAGE_OUTPUT_SCALE 1.0f
#define TRIAGE_OUTPUT_ZERO_POINT 0

esp_err_t triage_inference_init(triage_inference_t* triage) {
    if (!triage) return ESP_ERR_INVALID_ARG;

    memset(triage, 0, sizeof(triage_inference_t));

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
    resolver.AddFullyConnected();
    resolver.AddConv2D();  // Conv1D is converted to Conv2D in TFLite
    resolver.AddDepthwiseConv2D();
    resolver.AddAveragePool2D();  // GlobalAveragePooling1D
    resolver.AddMaxPool2D();
    resolver.AddSoftmax();
    resolver.AddReshape();
    resolver.AddQuantize();
    resolver.AddDequantize();
    resolver.AddLogistic();  // ReLU, etc.

    // Allocate arena (prefer PSRAM if available for ESP32-S3)
    triage->arena = heap_caps_malloc(TRIAGE_MODEL_ARENA_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!triage->arena) {
        triage->arena = heap_caps_malloc(TRIAGE_MODEL_ARENA_SIZE, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    if (!triage->arena) {
        ESP_LOGE(TAG, "Failed to allocate TFLM arena (%d KB)", TRIAGE_MODEL_ARENA_SIZE / 1024);
        return ESP_ERR_NO_MEM;
    }
    triage->arena_size = TRIAGE_MODEL_ARENA_SIZE;

    // Create interpreter
    triage->interpreter = new (triage->arena) tflite::MicroInterpreter(model, resolver, triage->arena, triage->arena_size, s_error_reporter);
    if (!triage->interpreter) {
        ESP_LOGE(TAG, "Failed to create MicroInterpreter");
        heap_caps_free(triage->arena);
        return ESP_ERR_NO_MEM;
    }

    TfLiteStatus status = triage->interpreter->AllocateTensors();
    if (status != kTfLiteOk) {
        ESP_LOGE(TAG, "AllocateTensors failed: %s", s_error_reporter->GetErrorMessage());
        triage->interpreter->~MicroInterpreter();
        heap_caps_free(triage->arena);
        return ESP_FAIL;
    }

    triage->input_tensor_idx = triage->interpreter->inputs()[0];
    triage->output_tensor_idx = triage->interpreter->outputs()[0];

    TfLiteTensor* input = triage->interpreter->input(triage->input_tensor_idx);
    TfLiteTensor* output = triage->interpreter->output(triage->output_tensor_idx);

    ESP_LOGI(TAG, "Triage inference initialized:");
    ESP_LOGI(TAG, "  Model size: %zu bytes", model_size);
    ESP_LOGI(TAG, "  Input:  tensor=%d, dims=%d, type=%d", triage->input_tensor_idx, input->dims->size, input->type);
    ESP_LOGI(TAG, "  Output: tensor=%d, dims=%d, type=%d", triage->output_tensor_idx, output->dims->size, output->type);
    for (int i = 0; i < input->dims->size; i++) {
        ESP_LOGI(TAG, "  Input dim[%d] = %d", i, input->dims->data[i]);
    }
    for (int i = 0; i < output->dims->size; i++) {
        ESP_LOGI(TAG, "  Output dim[%d] = %d", i, output->dims->data[i]);
    }
    ESP_LOGI(TAG, "  Arena used: %zu / %zu bytes", triage->interpreter->arena_used_bytes(), triage->arena_size);

    triage->model_data = (void*)model_data;
    triage->initialized = true;
    return ESP_OK;
}

esp_err_t triage_inference_run(triage_inference_t* triage, const triage_input_t* input, triage_output_t* output) {
    if (!triage || !triage->initialized || !input || !output) return ESP_ERR_INVALID_ARG;

    if (input->feature_count != TRIAGE_NUM_FEATURES) {
        ESP_LOGE(TAG, "Feature count mismatch: got %d, expected %d", input->feature_count, TRIAGE_NUM_FEATURES);
        return ESP_ERR_INVALID_SIZE;
    }

    int64_t start_us = esp_timer_get_time();

    TfLiteTensor* input_tensor = triage->interpreter->input(triage->input_tensor_idx);
    TfLiteTensor* output_tensor = triage->interpreter->output(triage->output_tensor_idx);

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
            input_data[i] = (int8_t)(quantized > 127 ? 127 : (quantized < -128 ? -128 : quantized));
        }
    } else if (input_tensor->type == kTfLiteFloat32) {
        float* input_data = input_tensor->data.f;
        for (int i = 0; i < TRIAGE_NUM_FEATURES; i++) {
            input_data[i] = input->features[i];
        }
    } else {
        ESP_LOGE(TAG, "Unsupported input tensor type: %d", input_tensor->type);
        return ESP_ERR_NOT_SUPPORTED;
    }

    // Run inference
    TfLiteStatus status = triage->interpreter->Invoke();
    if (status != kTfLiteOk) {
        ESP_LOGE(TAG, "Invoke failed: %s", s_error_reporter->GetErrorMessage());
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

        // For INT8 softmax output, we need to dequantize and apply softmax
        // The model outputs logits, so we dequantize then softmax
        float logits[TRIAGE_NUM_CLASSES];
        for (int i = 0; i < TRIAGE_NUM_CLASSES; i++) {
            logits[i] = (output_data[i] - zero_point) * scale;
        }

        // Softmax
        float max_logit = logits[0];
        for (int i = 1; i < TRIAGE_NUM_CLASSES; i++) {
            if (logits[i] > max_logit) max_logit = logits[i];
        }

        float sum_exp = 0.0f;
        for (int i = 0; i < TRIAGE_NUM_CLASSES; i++) {
            logits[i] = expf(logits[i] - max_logit);
            sum_exp += logits[i];
        }

        output->baseline_prob = logits[0] / sum_exp;
        output->stress_prob = logits[1] / sum_exp;
        output->artifact_prob = logits[2] / sum_exp;

        // Predicted class
        float max_prob = output->baseline_prob;
        output->predicted_class = 0;
        if (output->stress_prob > max_prob) {
            max_prob = output->stress_prob;
            output->predicted_class = 1;
        }
        if (output->artifact_prob > max_prob) {
            output->predicted_class = 2;
        }
    } else if (output_tensor->type == kTfLiteFloat32) {
        float* output_data = output_tensor->data.f;
        // Already probabilities from softmax
        output->baseline_prob = output_data[0];
        output->stress_prob = output_data[1];
        output->artifact_prob = output_data[2];

        float max_prob = output->baseline_prob;
        output->predicted_class = 0;
        if (output->stress_prob > max_prob) {
            max_prob = output->stress_prob;
            output->predicted_class = 1;
        }
        if (output->artifact_prob > max_prob) {
            output->predicted_class = 2;
        }
    } else {
        ESP_LOGE(TAG, "Unsupported output tensor type: %d", output_tensor->type);
        return ESP_ERR_NOT_SUPPORTED;
    }

    return ESP_OK;
}

esp_err_t triage_inference_deinit(triage_inference_t* triage) {
    if (!triage) return ESP_ERR_INVALID_ARG;

    if (triage->initialized && triage->interpreter) {
        triage->interpreter->~MicroInterpreter();
        triage->interpreter = nullptr;
    }

    if (triage->arena) {
        heap_caps_free(triage->arena);
        triage->arena = nullptr;
    }

    triage->initialized = false;
    ESP_LOGI(TAG, "Triage inference deinitialized");
    return ESP_OK;
}

esp_err_t triage_inference_get_model_info(triage_inference_t* triage, size_t* model_size, size_t* arena_used) {
    if (!triage || !triage->initialized) return ESP_ERR_INVALID_STATE;

    if (model_size) *model_size = synapse_triage_tflite_len;
    if (arena_used && triage->interpreter) {
        *arena_used = triage->interpreter->arena_used_bytes();
    }
    return ESP_OK;
}