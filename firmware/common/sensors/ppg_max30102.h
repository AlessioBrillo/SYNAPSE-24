#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "driver/i2c.h"
#include "sensor_scheduler.h"
#include "ppg_sqi.h"

#ifdef __cplusplus
extern "C" {
#endif

// Default I2C pins for ESP32-S3 (GPIO22-25 do NOT exist on S3).
// Shared bus with IMU: both drivers tolerate ESP_ERR_INVALID_STATE on
// i2c_driver_install when the bus is already owned by the other driver.
#define SYNAPSE_I2C_SDA_GPIO_DEFAULT 8
#define SYNAPSE_I2C_SCL_GPIO_DEFAULT 9

typedef struct {
    i2c_port_t i2c_port;
    int i2c_addr;
    int gpio_int;
    int sda_gpio_num;       // I2C SDA GPIO (default 8 on S3)
    int scl_gpio_num;       // I2C SCL GPIO (default 9 on S3)
    uint8_t led_current_red;
    uint8_t led_current_ir;
    uint8_t led_current_green;
    uint8_t sample_rate;
    uint8_t pulse_width;
    uint8_t adc_range;
} ppg_max30102_config_t;

esp_err_t ppg_max30102_init(const ppg_max30102_config_t* config);
esp_err_t ppg_max30102_read(sensor_sample_t* sample, void* user_ctx);
esp_err_t ppg_max30102_deinit(void* user_ctx);
esp_err_t ppg_max30102_enable_fifo(void* user_ctx);
esp_err_t ppg_max30102_disable_fifo(void* user_ctx);

// Get latest PPG SQI result for motion gate (Architecture.md §74)
esp_err_t ppg_max30102_get_sqi(ppg_sqi_result_t* result);

#ifdef __cplusplus
}
#endif