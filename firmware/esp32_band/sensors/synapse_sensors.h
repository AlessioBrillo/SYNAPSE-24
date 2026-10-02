/**
 * @file synapse_sensors.h
 * @brief Synapse Band v1 - Sensors Abstraction Layer
 * 
 * Unified interface for all sensors (ECG, PPG, IMU, GPS, Temperature).
 * Wraps firmware/common sensor drivers and adds GPS/Temp.
 * Integrates with sensor_scheduler for FreeRTOS task management.
 */

#ifndef SYNAPSE_SENSORS_H
#define SYNAPSE_SENSORS_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "synapse_config.h"
#include "sensor_scheduler.h"
#include "ecg_ad8232.h"
#include "ppg_max30102.h"
#include "imu_icm20948.h"

#include "synapse_sensor_types.h"

#ifdef __cplusplus
extern "C" {
#endif

// (gps/temp configs and gps_data_t/temp_data_t come from synapse_sensor_types.h)

// Combined sensor sample (extends sensor_sample_t for GPS/Temp)
typedef struct {
    sensor_sample_t base;   // Base sample (ECG/PPG/IMU)
    gps_data_t gps;         // GPS data (updated at GPS rate ~1Hz)
    temp_data_t temp;       // Temperature (updated at ~1Hz)
} synapse_sensor_sample_t;

// Sensor manager state
typedef struct {
    sensor_scheduler_t scheduler;
    QueueHandle_t sample_queue;
    
    // Sensor configs (populated from synapse_config)
    ecg_ad8232_config_t ecg_config;
    ppg_max30102_config_t ppg_config;
    imu_icm20948_config_t imu_config;
    gps_max_m10s_config_t gps_config;
    temp_tmp117_config_t temp_config;
    
    // GPS/Temp task handles
    TaskHandle_t gps_task;
    TaskHandle_t temp_task;
    
    // Latest data (for BLE/FIT access)
    gps_data_t latest_gps;
    temp_data_t latest_temp;
    SemaphoreHandle_t data_mutex;
    
    bool initialized;
    bool running;
} synapse_sensors_t;


// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize all sensors
 * Loads config from synapse_config, initializes sensor_scheduler,
 * registers ECG/PPG/IMU, starts GPS and Temperature tasks.
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_init(void);

/**
 * @brief Start sensor acquisition (creates FreeRTOS tasks)
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_start(void);

/**
 * @brief Stop sensor acquisition
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_stop(void);

/**
 * @brief Deinitialize sensors
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_deinit(void);

/**
 * @brief Get latest sensor sample (non-blocking)
 * @param[out] sample Pointer to sample struct
 * @return ESP_OK if sample available, ESP_ERR_TIMEOUT if queue empty
 */
esp_err_t synapse_sensors_get_sample(synapse_sensor_sample_t *sample);

/**
 * @brief Get latest GPS data (thread-safe)
 * @param[out] gps Pointer to GPS data struct
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_get_gps(gps_data_t *gps);

/**
 * @brief Get latest temperature data (thread-safe)
 * @param[out] temp Pointer to temperature data struct
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_get_temp(temp_data_t *temp);

/**
 * @brief Get sensor scheduler stats (sample counts, dropped samples)
 * @param[out] sample_counts Array of 4 uint32_t (ECG, PPG, IMU, EEG)
 * @param[out] dropped_samples Array of 4 uint32_t
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_get_stats(uint32_t *sample_counts, uint32_t *dropped_samples);

/**
 * @brief Check if sensors are running
 * @return true if running
 */
bool synapse_sensors_is_running(void);

/**
 * @brief Enable/disable specific sensor
 * @param sensor_type Sensor type (SENSOR_TYPE_ECG, PPG, IMU)
 * @param enable true to enable, false to disable
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_set_enabled(sensor_type_t sensor_type, bool enable);

/**
 * @brief Set GPS power mode
 * @param low_power true for low power mode (reduced update rate)
 * @return ESP_OK on success
 */
esp_err_t synapse_sensors_set_gps_power_mode(bool low_power);

/**
 * @brief Poll GPS/Temp drivers and refresh cached aux data
 * Call periodically from acquisition FSM (1Hz)
 */
void synapse_sensors_update_aux_data(void);

#ifdef __cplusplus
}
#endif

#endif // SYNAPSE_SENSORS_H