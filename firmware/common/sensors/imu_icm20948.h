#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "driver/i2c.h"
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

// Default I2C pins for ESP32-S3 (GPIO22-25 do NOT exist on S3; 21 is valid but
// kept free for wired-sync; 8/9 are safe on DevKitC-1)
#define SYNAPSE_I2C_SDA_GPIO_DEFAULT 8
#define SYNAPSE_I2C_SCL_GPIO_DEFAULT 9

// ICM-20948 IMU configuration
typedef struct {
    i2c_port_t i2c_port;    // I2C port (I2C_NUM_0 or I2C_NUM_1)
    int i2c_addr;           // I2C address (0x68 default)
    int gpio_int;           // GPIO for interrupt pin (-1 if not used)
    int sda_gpio_num;       // I2C SDA GPIO (default 8 on S3)
    int scl_gpio_num;       // I2C SCL GPIO (default 9 on S3)
    int accel_fsr_g;        // Accelerometer full-scale range: 2, 4, 8, 16g
    int gyro_fsr_dps;       // Gyroscope full-scale range: 250, 500, 1000, 2000 dps
    int accel_odr_hz;       // Accelerometer output data rate (Hz)
    int gyro_odr_hz;        // Gyroscope output data rate (Hz)
} imu_icm20948_config_t;

esp_err_t imu_icm20948_init(const imu_icm20948_config_t* config);
esp_err_t imu_icm20948_read(sensor_sample_t* sample, void* user_ctx);
esp_err_t imu_icm20948_deinit(void* user_ctx);
esp_err_t imu_icm20948_self_test(void* user_ctx);

#ifdef __cplusplus
}
#endif