#pragma once

#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TRIAGE_NUM_CLASSES 3
#define TRIAGE_NUM_FEATURES 26
#define TRIAGE_INPUT_DIM_0 1
#define TRIAGE_INPUT_DIM_1 1
#define TRIAGE_INPUT_DIM_2 26
#define TRIAGE_MODEL_ARENA_SIZE (64 * 1024)

typedef struct {
    float features[TRIAGE_NUM_FEATURES];
    int feature_count;
    int64_t timestamp_us;
} triage_input_t;

typedef struct {
    float baseline_prob;
    float stress_prob;
    float artifact_prob;
    int8_t predicted_class;  // 0=baseline, 1=stress, 2=artifact
    int64_t inference_time_us;
    bool valid;
} triage_output_t;

typedef struct {
    void* interpreter;
    void* model_data;
    void* arena;
    size_t arena_size;
    int input_tensor_idx;
    int output_tensor_idx;
    bool initialized;
} triage_inference_t;

esp_err_t triage_inference_init(triage_inference_t* triage);
esp_err_t triage_inference_run(triage_inference_t* triage, const triage_input_t* input, triage_output_t* output);
esp_err_t triage_inference_deinit(triage_inference_t* triage);
esp_err_t triage_inference_get_model_info(triage_inference_t* triage, size_t* model_size, size_t* arena_used);

#ifdef __cplusplus
}
#endif