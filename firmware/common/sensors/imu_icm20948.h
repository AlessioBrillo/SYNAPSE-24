#pragma once

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "synapse_i2c.h"
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

// ICM-20948 IMU configuration. The I2C bus is owned by synapse_i2c (shared with PPG, TMP117,
// INA219): init takes a reference, deinit or a failed init gives it back.
typedef struct {
    int i2c_port;           // 0 or 1
    int i2c_addr;           // I2C address (0x68 default)
    int gpio_int;           // GPIO for interrupt pin (-1 if not used)
    int sda_gpio_num;       // I2C SDA GPIO (< 0: SYNAPSE_I2C_DEFAULT_SDA_GPIO)
    int scl_gpio_num;       // I2C SCL GPIO (< 0: SYNAPSE_I2C_DEFAULT_SCL_GPIO)
    uint32_t i2c_freq_hz;   // bus clock (0: SYNAPSE_I2C_DEFAULT_FREQ_HZ)
    int accel_fsr_g;        // Accelerometer full-scale range: 2, 4, 8, 16g
    int gyro_fsr_dps;       // Gyroscope full-scale range: 250, 500, 1000, 2000 dps
    int accel_odr_hz;       // Accelerometer output data rate (Hz)
    int gyro_odr_hz;        // Gyroscope output data rate (Hz)
} imu_icm20948_config_t;

esp_err_t imu_icm20948_init(const imu_icm20948_config_t* config);
esp_err_t imu_icm20948_read(sensor_sample_t* sample, void* user_ctx);
esp_err_t imu_icm20948_deinit(void* user_ctx);
/**
 * Static sanity check of a band lying still on the bench (the factory station): WHO_AM_I 0xEA,
 * |accel| ~ 1 g with the gyro near zero, and a magnetometer that reads an Earth-sized field.
 * ESP_OK = pass, ESP_FAIL = a check failed, other = bus error; `detail` says what was measured.
 * This is NOT the datasheet self-test (the ST_CTRL registers against the factory trim values):
 * that procedure is not implemented, and the always-OK stub that stood here is gone.
 */
esp_err_t imu_icm20948_self_test(char* detail, size_t detail_len);

#ifdef __cplusplus
}
#endif