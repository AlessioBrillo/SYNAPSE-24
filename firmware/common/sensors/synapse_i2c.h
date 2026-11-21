#pragma once

/**
 * @file synapse_i2c.h
 * @brief The one owner of the shared I2C bus (PPG, IMU, TMP117, INA219 all sit on it).
 *
 * Before this module PPG and IMU each installed and *deleted* the bus driver, and
 * TMP117/INA219 worked only if one of them had run first: stopping the PPG killed the
 * temperature sensor and the power monitor. Now every driver calls acquire() in its init
 * and release() in its deinit (and on a failed init); the bus exists while any of them
 * holds it and goes away with the last one.
 *
 * The drivers only see this header, never the ESP-IDF I2C API, so the host tests drive them
 * against a simulated bus (tests/firmware_host) and the ESP-IDF driver can change underneath.
 */

#include <stddef.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Defaults for the ESP32-S3 DevKitC-1 N8R8: GPIO 21-25 are unusable with octal PSRAM. */
#define SYNAPSE_I2C_DEFAULT_SDA_GPIO 8
#define SYNAPSE_I2C_DEFAULT_SCL_GPIO 9
#define SYNAPSE_I2C_DEFAULT_FREQ_HZ  400000u
#define SYNAPSE_I2C_PORTS            2

/**
 * Take a reference on the bus of `port`, creating it on the first call.
 * A negative pin or a zero frequency means "the default above".
 * A later acquire with different pins or frequency than the bus was created with fails with
 * ESP_ERR_INVALID_STATE (one bus cannot have two wirings), never silently uses the first.
 */
esp_err_t synapse_i2c_acquire(int port, int sda_gpio, int scl_gpio, uint32_t freq_hz);

/** Drop a reference taken by acquire(); the bus is removed when the last one goes. Safe without acquire(). */
void synapse_i2c_release(int port);

/**
 * Does a device acknowledge 7-bit `addr`? ESP_OK = yes, ESP_ERR_NOT_FOUND = nobody answers there,
 * anything else = the bus itself failed (not acquired, timeout). Used by the I2C scan; it
 * transfers no data, so it cannot disturb a sensor.
 */
esp_err_t synapse_i2c_probe(int port, uint8_t addr);

/** Write `len` bytes (register address first) to the device at 7-bit `addr`. */
esp_err_t synapse_i2c_write(int port, uint8_t addr, const uint8_t *data, size_t len);

/** Write `write_len` bytes, then read `read_len` bytes after a repeated start (register read). */
esp_err_t synapse_i2c_write_read(int port, uint8_t addr, const uint8_t *write, size_t write_len,
                                 uint8_t *read, size_t read_len);

#ifdef __cplusplus
}
#endif
