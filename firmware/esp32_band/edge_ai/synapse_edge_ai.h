/**
 * @file synapse_edge_ai.h
 * @brief Synapse Band v1 - Edge AI (TFLM Inference)
 * 
 * Stress triage (3-class: baseline/stress/artifact) + Motion classifier
 * Wraps firmware/common/triage_inference and triage_features
 */

#ifndef SYNAPSE_EDGE_AI_H
#define SYNAPSE_EDGE_AI_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "triage_inference.h"
#include "triage_features.h"

#ifdef __cplusplus
extern "C" {
#endif

// Model types
typedef enum {
    EDGE_AI_MODEL_STRESS_TRIAGE = 0,  // 3-class: baseline, stress, artifact
    EDGE_AI_MODEL_MOTION_CLASSIFIER,  // Motion type classification
    EDGE_AI_MODEL_COUNT
} edge_ai_model_t;

// Stress triage output
typedef struct {
    float baseline_prob;
    float stress_prob;
    float artifact_prob;
    int8_t predicted_class;       // 0=baseline, 1=stress, 2=artifact
    int64_t inference_time_us;
    bool valid;
} stress_triage_t;

// Motion classifier output
typedef struct {
    float walk_prob;
    float run_prob;
    float cycle_prob;
    float stationary_prob;
    float sleep_prob;
    int8_t predicted_class;       // 0=walk, 1=run, 2=cycle, 3=stationary, 4=sleep
    int64_t inference_time_us;
    bool valid;
} motion_classifier_t;

// Edge AI state
typedef struct {
    // Stress triage
    triage_inference_t stress_triage;
    triage_features_t stress_features;
    
    // Motion classifier (separate model)
    triage_inference_t motion_classifier;
    triage_features_t motion_features;
    
    // Latest outputs
    stress_triage_t latest_stress;
    motion_classifier_t latest_motion;
    
    bool initialized;
    bool models_loaded;
} synapse_edge_ai_t;


// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize Edge AI module
 * Loads TFLM models from embedded model_data.h
 * @return ESP_OK on success
 */
esp_err_t synapse_edge_ai_init(void);

/**
 * @brief Run stress triage inference
 * Computes features from IMU/PPG ring buffers, runs TFLM inference
 * @param[out] result Stress triage result
 * @return ESP_OK on success
 */
esp_err_t synapse_edge_ai_run_stress_triage(stress_triage_t *result);

/**
 * @brief Run motion classification inference
 * @param[out] result Motion classifier result
 * @return ESP_OK on success
 */
esp_err_t synapse_edge_ai_run_motion_classifier(motion_classifier_t *result);

/**
 * @brief Get latest stress triage result (non-blocking)
 * @param[out] result Stress triage result
 * @return ESP_OK if new result available, ESP_ERR_INVALID_STATE if not
 */
esp_err_t synapse_edge_ai_get_latest_stress(stress_triage_t *result);

/**
 * @brief Get latest motion classifier result (non-blocking)
 * @param[out] result Motion classifier result
 * @return ESP_OK if new result available, ESP_ERR_INVALID_STATE if not
 */
esp_err_t synapse_edge_ai_get_latest_motion(motion_classifier_t *result);

/**
 * @brief Check if stress indicates high stress level
 * @return true if stress_prob > threshold
 */
bool synapse_edge_ai_is_high_stress(void);

/**
 * @brief Check if artifact detected (poor signal quality)
 * @return true if artifact_prob > threshold
 */
bool synapse_edge_ai_is_artifact(void);

/**
 * @brief Get model info (size, arena usage)
 * @param model Model type
 * @param[out] model_size Model size in bytes
 * @param[out] arena_used Arena usage in bytes
 * @return ESP_OK on success
 */
esp_err_t synapse_edge_ai_get_model_info(edge_ai_model_t model, size_t *model_size, size_t *arena_used);

/**
 * @brief Deinitialize Edge AI
 * @return ESP_OK on success
 */
esp_err_t synapse_edge_ai_deinit(void);

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_EDGE_AI_H