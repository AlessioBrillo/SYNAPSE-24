#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "sensor_scheduler.h"
#include "ppg_sqi.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    int i2c_port;
    int i2c_addr;
    int gpio_int;
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