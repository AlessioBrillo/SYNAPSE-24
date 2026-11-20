/* driver/i2c_master.h stub for the host compile gate: the ESP-IDF 5.x i2c_master API surface
 * that firmware/common/sensors/synapse_i2c.c uses, with the real names and argument order.
 * The bodies live in tests/firmware_host/host_glue_i2c.c (a counting fake bus).
 */
#ifndef DRIVER_I2C_MASTER_H_STUB
#define DRIVER_I2C_MASTER_H_STUB

#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef struct i2c_bus_stub *i2c_master_bus_handle_t;
typedef struct i2c_dev_stub *i2c_master_dev_handle_t;
typedef int i2c_port_num_t;

typedef enum { I2C_CLK_SRC_DEFAULT = 0 } i2c_clock_source_t;
typedef enum { I2C_ADDR_BIT_LEN_7 = 0 } i2c_addr_bit_len_t;

typedef struct {
    i2c_port_num_t i2c_port;
    int sda_io_num;
    int scl_io_num;
    i2c_clock_source_t clk_source;
    uint8_t glitch_ignore_cnt;
    int intr_priority;
    size_t trans_queue_depth;
    struct {
        uint32_t enable_internal_pullup : 1;
        uint32_t allow_pd : 1;
    } flags;
} i2c_master_bus_config_t;

typedef struct {
    i2c_addr_bit_len_t dev_addr_length;
    uint16_t device_address;
    uint32_t scl_speed_hz;
    uint32_t scl_wait_us;
    struct {
        uint32_t disable_ack_check : 1;
    } flags;
} i2c_device_config_t;

esp_err_t i2c_new_master_bus(const i2c_master_bus_config_t *bus_config, i2c_master_bus_handle_t *ret_bus_handle);
esp_err_t i2c_del_master_bus(i2c_master_bus_handle_t bus_handle);
esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t bus_handle, const i2c_device_config_t *dev_config,
                                    i2c_master_dev_handle_t *ret_handle);
esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t handle);
esp_err_t i2c_master_transmit(i2c_master_dev_handle_t i2c_dev, const uint8_t *write_buffer, size_t write_size,
                              int xfer_timeout_ms);
esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t i2c_dev, const uint8_t *write_buffer,
                                      size_t write_size, uint8_t *read_buffer, size_t read_size,
                                      int xfer_timeout_ms);

#endif // DRIVER_I2C_MASTER_H_STUB
