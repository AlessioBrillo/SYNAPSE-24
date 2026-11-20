#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "synapse_i2c.h"
#include "sensor_scheduler.h"
#include "ppg_sqi.h"

#ifdef __cplusplus
extern "C" {
#endif

// The I2C bus is owned by synapse_i2c (shared with IMU, TMP117, INA219): this driver takes a
// reference in init and gives it back in deinit or when init fails.
typedef struct {
    int i2c_port;           // 0 or 1
    int i2c_addr;
    int gpio_int;           // interrupt pin, -1 if not wired (the driver polls either way)
    int sda_gpio_num;       // I2C SDA GPIO (< 0: SYNAPSE_I2C_DEFAULT_SDA_GPIO)
    int scl_gpio_num;       // I2C SCL GPIO (< 0: SYNAPSE_I2C_DEFAULT_SCL_GPIO)
    uint32_t i2c_freq_hz;   // bus clock (0: SYNAPSE_I2C_DEFAULT_FREQ_HZ)
    uint8_t led_current_red;   // MAX30102 LEDx_PA: 8 bits, 0.2 mA per step (0xFF = ~51 mA)
    uint8_t led_current_ir;
    uint8_t sample_rate;
    uint8_t pulse_width;
    uint8_t adc_range;
} ppg_max30102_config_t;

esp_err_t ppg_max30102_init(const ppg_max30102_config_t* config);
esp_err_t ppg_max30102_read(sensor_sample_t* sample, void* user_ctx);
esp_err_t ppg_max30102_deinit(void* user_ctx);
esp_err_t ppg_max30102_enable_fifo(void* user_ctx);
esp_err_t ppg_max30102_disable_fifo(void* user_ctx);

// More samples of the burst read by the last poll are waiting: call read() again (scheduler hook).
bool ppg_max30102_has_more(void* user_ctx);

// FIFO samples lost: overwritten while the poll stalled (lower bound), or taken out of the FIFO
// by a burst read that failed half way.
uint32_t ppg_max30102_skipped_samples(void);
/** Samples lost since the last call (the call resets it); the scheduler turns them into a sequence hole. */
uint32_t ppg_max30102_take_lost(void* user_ctx);

// Get latest PPG SQI result for motion gate (Architecture.md §74)
esp_err_t ppg_max30102_get_sqi(ppg_sqi_result_t* result);

#ifdef __cplusplus
}
#endif