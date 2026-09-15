#include "triage_inference.h"
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

esp_err_t triage_inference_init(triage_inference_t* triage, const uint8_t* model_data, size_t model_size) {
    if (!triage || !model_data || model_size == 0) return ESP_ERR_INVALID_ARG;

    memset(triage, 0, sizeof(triage_inference_t));

    if (!s_error_reporter) {
        s_error_reporter = &s_micro_error_reporter;
    }

    const tflite::Model* model = tflite::GetModel(model_data);
    if (!model) {
        ESP_LOGE(TAG, "Failed to parse TFLite model");
        return ESP_ERR_INVALID_ARG;
    }

    if (model->version() != TFLITE_SCHEMA_VERSION) {
        ESP_LOGE(TAG, "Model schema version mismatch: %d != %d", model->version(), TFLITE_SCHEMA_VERSION);
        return ESP_ERR_INVALID_ARG;
    }

    static tflite::MicroMutableOpResolver<10> resolver;
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

    triage->arena = heap_caps_malloc(TRIAGE_MODEL_ARENA_SIZE, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!triage->arena) {
        triage->arena = heap_caps_malloc(TRIAGE_MODEL_ARENA_SIZE, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    }
    if (!triage->arena) {
        ESP_LOGE(TAG, "Failed to allocate TFLM arena (%d KB)", TRIAGE_MODEL_ARENA_SIZE / 1024);
        return ESP_ERR_NO_MEM;
    }
    triage->arena_size = TRIAGE_MODEL_ARENA_SIZE;

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
    ESP_LOGI(TAG, "  Input:  tensor=%d, dims=%d, type=%d", triage->input_tensor_idx, input->dims->size, input->type);
    ESP_LOGI(TAG, "  Output: tensor=%d, dims=%d, type=%d", triage->output_tensor_idx, output->dims->size, output->type);
    for (int i = 0; i < input->dims->size; i++) {
        ESP_LOGI(TAG, "  Input dim[%d] = %d", i, input->dims->data[i]);
    }

    triage->model_data = (void*)model_data;
    triage->initialized = true;
    return ESP_OK;
}

esp_err_t triage_inference_run(triage_inference_t* triage, const triage_input_t* input, triage_output_t* output) {
    if (!triage || !triage->initialized || !input || !output) return ESP_ERR_INVALID_ARG;

    if (input->feature_count > TRIAGE_MAX_FEATURES) {
        ESP_LOGE(TAG, "Too many features: %d > %d", input->feature_count, TRIAGE_MAX_FEATURES);
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
    }

    int copy_count = (input->feature_count < expected_features) ? input->feature_count : expected_features;

    if (input_tensor->type == kTfLiteFloat32) {
        float* input_data = input_tensor->data.f;
        for (int i = 0; i < copy_count; i++) {
            input_data[i] = input->features[i];
        }
        for (int i = copy_count; i < expected_features; i++) {
            input_data[i] = 0.0f;
        }
    } else if (input_tensor->type == kTfLiteInt8) {
        int8_t* input_data = input_tensor->data.int8;
        float scale = input_tensor->params.scale;
        int32_t zero_point = input_tensor->params.zero_point;
        for (int i = 0; i < copy_count; i++) {
            int32_t quantized = (int32_t)roundf(input->features[i] / scale) + zero_point;
            input_data[i] = (int8_t)(quantized > 127 ? 127 : (quantized < -128 ? -128 : quantized));
        }
        for (int i = copy_count; i < expected_features; i++) {
            input_data[i] = (int8_t)zero_point;
        }
    } else {
        ESP_LOGE(TAG, "Unsupported input tensor type: %d", input_tensor->type);
        return ESP_ERR_NOT_SUPPORTED;
    }

    TfLiteStatus status = triage->interpreter->Invoke();
    if (status != kTfLiteOk) {
        ESP_LOGE(TAG, "Invoke failed: %s", s_error_reporter->GetErrorMessage());
        return ESP_FAIL;
    }

    int64_t end_us = esp_timer_get_time();

    memset(output, 0, sizeof(triage_output_t));
    output->inference_time_us = end_us - start_us;
    output->valid = true;

    if (output_tensor->type == kTfLiteFloat32) {
        float* output_data = output_tensor->data.f;
        int output_size = 1;
        for (int i = 1; i < output_tensor->dims->size; i++) {
            output_size *= output_tensor->dims->data[i];
        }

        if (output_size >= 3) {
            output->stress_prob = output_data[0];
            output->sleep_prob = output_data[1];
            output->artifact_prob = output_data[2];
            output->stress_class = (output_data[0] > output_data[1] && output_data[0] > output_data[2]) ? 0 :
                                   (output_data[1] > output_data[2]) ? 1 : 2;
        } else if (output_size >= 5) {
            float max_val = output_data[0];
            int max_idx = 0;
            for (int i = 1; i < output_size; i++) {
                if (output_data[i] > max_val) {
                    max_val = output_data[i];
                    max_idx = i;
                }
            }
            output->sleep_stage = max_idx;
        }
    } else if (output_tensor->type == kTfLiteInt8) {
        int8_t* output_data = output_tensor->data.int8;
        float scale = output_tensor->params.scale;
        int32_t zero_point = output_tensor->params.zero_point;
        int output_size = 1;
        for (int i = 1; i < output_tensor->dims->size; i++) {
            output_size *= output_tensor->dims->data[i];
        }

        float max_val = (output_data[0] - zero_point) * scale;
        int max_idx = 0;
        for (int i = 1; i < output_size; i++) {
            float val = (output_data[i] - zero_point) * scale;
            if (val > max_val) {
                max_val = val;
                max_idx = i;
            }
        }

        if (output_size >= 3) {
            output->stress_prob = (output_data[0] - zero_point) * scale;
            output->sleep_prob = (output_data[1] - zero_point) * scale;
            output->artifact_prob = (output_data[2] - zero_point) * scale;
            output->stress_class = max_idx;
        } else if (output_size >= 5) {
            output->sleep_stage = max_idx;
        }
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

    if (model_size) *model_size = 0;
    if (arena_used && triage->interpreter) {
        *arena_used = triage->interpreter->arena_used_bytes();
    }
    return ESP_OK;
}