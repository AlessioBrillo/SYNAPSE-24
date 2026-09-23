#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

// PPG Signal Quality Index configuration
// Karlen et al. 2013: wearable PPG SQI >= 0.5 acceptable
// Architecture.md §74: motion gate requires 2 consecutive clean assessments

#define PPG_SQI_WINDOW_SIZE       256   // 4 seconds at 64Hz
#define PPG_SQI_OUTPUT_RATE_HZ    100   // Output SQI at IMU rate (100Hz) for motion gate

typedef struct {
    float sqi;                    // Signal Quality Index [0, 1]
    float perfusion_index;        // AC/DC * 100%
    float motion_artifact_prob;   // Motion Artifact Probability [0, 1]
    int64_t timestamp_us;
} ppg_sqi_result_t;

typedef struct {
    float red_buffer[PPG_SQI_WINDOW_SIZE];
    float ir_buffer[PPG_SQI_WINDOW_SIZE];
    size_t write_idx;
    size_t count;
    bool initialized;
    
    // Cached results for 100Hz output
    ppg_sqi_result_t last_result;
    int samples_since_output;
    int output_every;  // 64Hz / 100Hz = 0.64 -> output every sample, interpolate
} ppg_sqi_ctx_t;

esp_err_t ppg_sqi_init(void);
esp_err_t ppg_sqi_process_sample(float red, float ir, int64_t timestamp_us, ppg_sqi_result_t* result);
esp_err_t ppg_sqi_get_latest(ppg_sqi_result_t* result);
esp_err_t ppg_sqi_reset(void);

#ifdef __cplusplus
}
#endif