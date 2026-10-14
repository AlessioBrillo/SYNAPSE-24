/* Minimal driver/i2c.h stub for the host compile gate.
 *
 * Provides exactly what the INA219 driver calls; the behaviour under test is
 * the register arithmetic, not a real bus.
 */
#ifndef DRIVER_I2C_H_STUB
#define DRIVER_I2C_H_STUB

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef int i2c_port_t;

#define I2C_NUM_0 0
#define I2C_NUM_1 1

esp_err_t i2c_master_write_to_device(i2c_port_t port, uint8_t addr,
                                     const uint8_t *data, size_t len,
                                     int timeout_ticks);
esp_err_t i2c_master_write_read_device(i2c_port_t port, uint8_t addr,
                                       const uint8_t *write, size_t write_len,
                                       uint8_t *read, size_t read_len,
                                       int timeout_ticks);

#endif // DRIVER_I2C_H_STUB
