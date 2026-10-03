/**
 * @file fit_writer.h
 * @brief Synapse Band v1 - FIT File Writer (LittleFS)
 * 
 * Writes valid FIT files compatible with Garmin Connect, Strava, Averyn, etc.
 * Uses SPIFFS on the 'storage' partition (built-in IDF component).
 * FIT Profile: 21.138.00 (SDK 21.138.00)
 * 
 * Developer Data Fields (Synapse Custom - flat scalars only per FIT spec):
 * - synapse_stress (uint8): 0=baseline, 1=stress, 2=artifact
 * - synapse_sqi (uint8): PPG Signal Quality Index (0-255 scaled)
 * - synapse_map (uint8): Motion Artifact Probability (0-255 scaled)
 * - synapse_ecg_quality (uint8): ECG quality confidence (0-255 scaled)
 * - synapse_sleep_stage (uint8): 0=W, 1=N1, 2=N2, 3=N3, 4=REM
 * - synapse_rmssd (uint16): RMSSD in ms
 * - synapse_sdnn (uint16): SDNN in ms
 * - synapse_lf_hf (uint16): LF/HF ratio ×100
 */

#ifndef SYNAPSE_FIT_H
#define SYNAPSE_FIT_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "ble_gatt_server.h"  // For synapse_feature_stream_t

#ifdef __cplusplus
extern "C" {
#endif

// ============================================================================
// FIT File Constants (from FIT SDK 21.138.00)
// ============================================================================
#define FIT_FILE_HEADER_SIZE 14
#define FIT_CRC_SIZE 2
#define FIT_MAX_RECORD_SIZE 256

// FIT message types
#define FIT_MESG_FILE_ID             0
#define FIT_MESG_DEVICE_INFO         23
#define FIT_MESG_SESSION             18
#define FIT_MESG_LAP                 19
#define FIT_MESG_RECORD              20
#define FIT_MESG_EVENT               21
#define FIT_MESG_DEVELOPER_DATA_ID   206
#define FIT_MESG_FIELD_DESCRIPTION   207
#define FIT_MESG_DEVELOPER_DATA      209

// FIT base types
#define FIT_BASE_TYPE_ENUM       0
#define FIT_BASE_TYPE_SINT8      1
#define FIT_BASE_TYPE_UINT8      2
#define FIT_BASE_TYPE_SINT16     131
#define FIT_BASE_TYPE_UINT16     132
#define FIT_BASE_TYPE_SINT32     133
#define FIT_BASE_TYPE_UINT32     134
#define FIT_BASE_TYPE_STRING     7
#define FIT_BASE_TYPE_FLOAT32    136
#define FIT_BASE_TYPE_FLOAT64    137
#define FIT_BASE_TYPE_UINT8Z     10
#define FIT_BASE_TYPE_UINT16Z    11
#define FIT_BASE_TYPE_UINT32Z    12
#define FIT_BASE_TYPE_BYTE       13

// FIT Developer Data Field IDs (assigned sequentially)
#define SYNAPSE_DEV_FIELD_STRESS       0
#define SYNAPSE_DEV_FIELD_SQI          1
#define SYNAPSE_DEV_FIELD_MAP          2
#define SYNAPSE_DEV_FIELD_ECG_QUALITY  3
#define SYNAPSE_DEV_FIELD_SLEEP_STAGE  4
#define SYNAPSE_DEV_FIELD_RMSSD        5
#define SYNAPSE_DEV_FIELD_SDNN         6
#define SYNAPSE_DEV_FIELD_LF_HF        7

#define SYNAPSE_NUM_DEV_FIELDS 8

// ============================================================================
// FIT Session Configuration
// ============================================================================
typedef enum {
    FIT_SPORT_GENERIC = 0,
    FIT_SPORT_RUNNING = 1,
    FIT_SPORT_CYCLING = 2,
    FIT_SPORT_SWIMMING = 3,
    FIT_SPORT_WALKING = 4,
    FIT_SPORT_HIKING = 5,
    FIT_SPORT_SLEEP = 6,
} fit_sport_t;

typedef struct {
    fit_sport_t sport_type;
    uint32_t start_time_utc;      // UTC seconds since 1989-12-31 (FIT epoch)
    uint32_t duration_sec;        // Total session duration
    double start_lat;             // degrees
    double start_lon;             // degrees
    bool has_gps;
} fit_session_config_t;

// ============================================================================
// FIT Record Data (per-second or per-sample record)
// ============================================================================
typedef struct {
    uint32_t timestamp_utc;       // UTC seconds
    // Position
    double lat;                   // degrees (semicircles in FIT)
    double lon;                   // degrees
    double altitude;              // meters
    float speed;                  // m/s
    // Heart Rate
    uint16_t heart_rate;          // bpm
    uint16_t rr_interval_ms;      // ms (0 = none)
    // Sensor data
    int16_t ecg_raw;              // Raw ECG sample (scaled)
    uint16_t ppg_raw_ir;          // PPG IR raw
    uint16_t ppg_raw_red;         // PPG Red raw
    // IMU
    int16_t accel_x;              // mg
    int16_t accel_y;              // mg
    int16_t accel_z;              // mg
    int16_t gyro_x;               // deg/s * 100
    int16_t gyro_y;               // deg/s * 100
    int16_t gyro_z;               // deg/s * 100
    // Temperature
    int16_t temperature;          // 0.01°C
    // Synapse Developer Data
    uint8_t stress_level;         // 0=baseline, 1=stress, 2=artifact
    uint8_t ppg_sqi;              // 0-255
    uint8_t ppg_map;              // 0-255
    uint8_t ecg_quality;          // 0-255
    uint8_t sleep_stage;          // 0=W,1=N1,2=N2,3=N3,4=REM
    uint16_t rmssd;               // ms
    uint16_t sdnn;                // ms
    uint16_t lf_hf;               // ×100
    // Battery
    uint8_t battery_level;        // 0-100%
} fit_record_t;

// ============================================================================
// Session State
// ============================================================================
typedef struct {
    bool active;
    fit_session_config_t config;
    char filepath[64];
    uint32_t record_count;
    uint32_t session_start_ticks;
    // Circular buffer for power-loss protection
    // Writes to LittleFS periodically
} fit_session_t;

// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize FIT writer (mounts LittleFS)
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_init(void);

/**
 * @brief Start a new FIT recording session
 * @param config Session configuration
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_start_session(const fit_session_config_t *config);

/**
 * @brief Write a data record to the current session
 * @param record Record data
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_write_record(const fit_record_t *record);

/**
 * @brief Write a record from feature stream (convenience)
 * @param feature Feature stream data from BLE
 * @param gps GPS data (optional, can be NULL)
 * @param ecg_sample Raw ECG sample (optional)
 * @param ppg_ir PPG IR raw
 * @param ppg_red PPG Red raw
 * @param accel IMU accelerometer
 * @param gyro IMU gyroscope
 * @param temp Temperature 0.01°C
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_write_from_feature(const synapse_feature_stream_t *feature,
                                          const double *gps_lat,
                                          const double *gps_lon,
                                          const double *gps_alt,
                                          const float *gps_speed,
                                          const int16_t *ecg_sample,
                                          const uint16_t *ppg_ir,
                                          const uint16_t *ppg_red,
                                          const int16_t accel[3],
                                          const int16_t gyro[3],
                                          const int16_t *temp);

/**
 * @brief Stop current session and finalize FIT file
 * @param output_path Where to copy the final file (optional, NULL = keep in LittleFS)
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_stop_session(const char *output_path);

/**
 * @brief Get current session status
 * @param active Returns true if session active
 * @param record_count Returns number of records written
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_get_status(bool *active, uint32_t *record_count);

/**
 * @brief List all FIT files in LittleFS
 * @param files Array to fill (max 32 entries)
 * @param max_files Max entries
 * @param count Returns actual count
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_list_files(char files[][64], int max_files, int *count);

/**
 * @brief Delete a FIT file
 * @param filename File to delete
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_delete_file(const char *filename);

/**
 * @brief Validate a FIT file (check CRC, structure)
 * @param filepath File to validate
 * @return ESP_OK if valid
 */
esp_err_t synapse_fit_validate_file(const char *filepath);

/**
 * @brief Convert LittleFS FIT file to standard FIT format (for export)
 * @param src_path Source in LittleFS
 * @param dst_path Destination (can be SD card, USB, etc.)
 * @return ESP_OK on success
 */
esp_err_t synapse_fit_export_file(const char *src_path, const char *dst_path);

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_FIT_H