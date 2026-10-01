#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "sensor_scheduler.h"
#include "ppg_sqi.h"

#ifdef __cplusplus
extern "C" {
#endif

// PPG Feature Processor Configuration
// Architecture.md §34: Tier 0 continuous H24 - on-device feature extraction
// 5-second sliding window, 10 Hz feature output rate

#define PPG_PROCESSOR_WINDOW_SEC      5
#define PPG_PROCESSOR_OUTPUT_HZ       10
#define PPG_PROCESSOR_SAMPLE_RATE_HZ  50   // Tier 0: 50 Hz (changed from 64)
#define PPG_PROCESSOR_WINDOW_SAMPLES  (PPG_PROCESSOR_WINDOW_SEC * PPG_PROCESSOR_SAMPLE_RATE_HZ)
#define PPG_PROCESSOR_MIN_PEAKS       3

// PPG Feature output structure (compressed for BLE transmission)
typedef struct {
    float hr_bpm;                 // Heart rate in BPM
    float rmssd_ms;               // RMSSD in milliseconds
    float sdnn_ms;                // SDNN in milliseconds
    float ppi_ms;                 // Mean PPI (pulse-to-pulse interval)
    float snr_db;                 // Signal-to-noise ratio estimate
    float pi_percent;             // Perfusion index (%)
    bool  valid;                  // Valid feature set (enough peaks, good SQI)
    bool  motion_artifact;        // Motion artifact detected
    int   peak_count;             // Number of peaks in window
    int64_t timestamp_us;         // Window center timestamp
} ppg_features_t;

// PPG Processor context
typedef struct {
    float ir_buffer[PPG_PROCESSOR_WINDOW_SAMPLES];
    float red_buffer[PPG_PROCESSOR_WINDOW_SAMPLES];
    size_t write_idx;
    size_t count;
    bool initialized;
    
    // Peak detection state
    float peak_threshold;
    int last_peak_idx;
    int peak_count;
    int peak_intervals[PPG_PROCESSOR_WINDOW_SAMPLES / 10];  // Max reasonable peaks in 5s
    int interval_count;
    
    // Output decimation
    int samples_since_output;
    int output_every;  // 64Hz / 10Hz = 6.4 -> alternate 6/7
    
    // Cached latest features
    ppg_features_t last_features;
} ppg_processor_ctx_t;

// Initialize PPG processor
esp_err_t ppg_processor_init(void);

// Process PPG sample (called from PPG driver at 64Hz)
// Returns ESP_OK if features were computed this call, ESP_ERR_NOT_FINISHED otherwise
esp_err_t ppg_processor_process_sample(float red, float ir, int64_t timestamp_us, const ppg_sqi_result_t* sqi, ppg_features_t* features_out);

// Get latest computed features
esp_err_t ppg_processor_get_latest(ppg_features_t* features_out);

// Reset processor state
esp_err_t ppg_processor_reset(void);

#ifdef __cplusplus
}
#endif