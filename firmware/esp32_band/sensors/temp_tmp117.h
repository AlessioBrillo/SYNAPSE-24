/**
 * @file temp_tmp117.h
 * @brief Synapse Band v1 - TMP117 Temperature Sensor Driver
 * 
 * High-precision digital temperature sensor (±0.1°C accuracy)
 * I2C interface, address 0x48, continuous conversion mode.
 */

#ifndef TEMP_TMP117_H
#define TEMP_TMP117_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "synapse_sensor_types.h"

#ifdef __cplusplus
extern "C" {
#endif

// TMP117 Register Addresses
#define TMP117_REG_TEMP_RESULT    0x00
#define TMP117_REG_CONFIGURATION  0x01
#define TMP117_REG_TEMP_HIGH_LIMIT 0x02
#define TMP117_REG_TEMP_LOW_LIMIT  0x03
#define TMP117_REG_EEPROM_UL      0x04
#define TMP117_REG_EEPROM_LL      0x05
#define TMP117_REG_EEPROM_CONFIG  0x06
#define TMP117_REG_DEVICE_ID      0x0F

// Configuration register bits
#define TMP117_CFG_SHUTDOWN       (1 << 0)
#define TMP117_CFG_MODE_MASK      (3 << 1)   // 00=continuous, 01=one-shot, 10=shutdown
#define TMP117_CFG_MODE_CONT      (0 << 1)
#define TMP117_CFG_MODE_ONESHOT   (1 << 1)
#define TMP117_CFG_MODE_SHUTDOWN  (2 << 1)
#define TMP117_CFG_CONV_CYCLE_MASK (7 << 3)  // Conversion cycle time
#define TMP117_CFG_CONV_CYCLE_15_5MS (0 << 3)
#define TMP117_CFG_CONV_CYCLE_125MS  (1 << 3)
#define TMP117_CFG_CONV_CYCLE_250MS  (2 << 3)
#define TMP117_CFG_CONV_CYCLE_500MS  (3 << 3)
#define TMP117_CFG_CONV_CYCLE_1S     (4 << 3)
#define TMP117_CFG_CONV_CYCLE_4S     (5 << 3)
#define TMP117_CFG_CONV_CYCLE_8S     (6 << 3)
#define TMP117_CFG_CONV_CYCLE_16S    (7 << 3)
#define TMP117_CFG_ALERT_ENABLE    (1 << 6)
#define TMP117_CFG_ALERT_POLARITY  (1 << 7)
#define TMP117_CFG_DRDY_MODE       (1 << 13)
#define TMP117_CFG_SOFT_RESET      (1 << 15)

#define TMP117_DEVICE_ID          0x0117
#define TMP117_I2C_ADDR_DEFAULT   0x48
#define TMP117_TEMP_LSB_C         0.0078125f  // 7.8125 mC per LSB

typedef struct {
    int i2c_port;
    int i2c_addr;
    int gpio_alert;
    uint16_t conversion_cycle_ms;
    bool continuous_mode;
} temp_tmp117_config_t;

typedef struct {
    temp_tmp117_config_t config;
    SemaphoreHandle_t mutex;
    bool initialized;
    bool running;
    TaskHandle_t task;
    temp_data_t latest_data;
    uint32_t read_count;
    uint32_t error_count;
} temp_tmp117_t;

// ============================================================================
// Public API
// ============================================================================

/**
 * @brief Initialize TMP117 driver
 * @param config TMP117 configuration
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_init(const temp_tmp117_config_t *config);

/**
 * @brief Start continuous temperature readings
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_start(void);

/**
 * @brief Stop temperature readings
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_stop(void);

/**
 * @brief Deinitialize TMP117 driver
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_deinit(void);

/**
 * @brief Read temperature once (one-shot or continuous)
 * @param[out] data Temperature data
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_read_once(temp_data_t *data);

/**
 * @brief Get latest temperature (thread-safe)
 * @param[out] data Temperature data
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_get_latest(temp_data_t *data);

/**
 * @brief Check if sensor is initialized and responding
 * @return true if OK
 */
bool temp_tmp117_is_healthy(void);

/**
 * @brief Set high/low temperature limits for ALERT pin
 * @param high_c High limit in Celsius
 * @param low_c Low limit in Celsius
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_set_limits(float high_c, float low_c);

/**
 * @brief Soft reset the sensor
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_soft_reset(void);

/**
 * @brief Get driver statistics
 * @param[out] reads Number of successful reads
 * @param[out] errors Number of I2C errors
 * @return ESP_OK on success
 */
esp_err_t temp_tmp117_get_stats(uint32_t *reads, uint32_t *errors);

#ifdef __cplusplus
}
#endif

#endif // TEMP_TMP117_H