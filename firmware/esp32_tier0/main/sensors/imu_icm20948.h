#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

// ICM-20948 IMU configuration
typedef struct {
    int i2c_port;         // I2C port number (I2C_NUM_0 or I2C_NUM_1)
    int i2c_addr;         // I2C address (0x68 default)
    int gpio_int;         // GPIO for interrupt pin (-1 if not used)
    int accel_fsr_g;      // Accelerometer full-scale range: 2, 4, 8, 16g
    int gyro_fsr_dps;     // Gyroscope full-scale range: 250, 500, 1000, 2000 dps
    int accel_odr_hz;     // Accelerometer output data rate (Hz)
    int gyro_odr_hz;      // Gyroscope output data rate (Hz)
} imu_icm20948_config_t;

esp_err_t imu_icm20948_init(const imu_icm20948_config_t* config);
esp_err_t imu_icm20948_read(sensor_sample_t* sample, void* user_ctx);
esp_err_t imu_icm20948_deinit(void* user_ctx);
esp_err_t imu_icm20948_self_test(void* user_ctx);

#ifdef __cplusplus
}
#endif