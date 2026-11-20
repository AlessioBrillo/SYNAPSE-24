/* Host gate for firmware/common/sensors/synapse_i2c.c, the owner of the shared I2C bus.
 *
 * The ESP-IDF i2c_master driver is replaced by a counting fake (stubs/driver/i2c_master.h has the
 * declarations), so the behaviour that used to be wrong can be asserted: one bus for any number of
 * drivers, the bus survives until the LAST driver leaves, two wirings on one port are refused, and
 * a device handle is created once per address.
 */
#include <stdint.h>
#include <string.h>

#include "driver/i2c_master.h"
#include "freertos/semphr.h"
#include "synapse_i2c.h"

/* --- the fake bus ---------------------------------------------------------------------------- */
static int s_buses_created, s_buses_deleted, s_devs_added, s_devs_removed, s_transfers;
static int s_fail_next_bus;
static uint8_t s_last_write[16];
static size_t s_last_write_len;
static int s_last_cfg_sda, s_last_cfg_scl, s_last_dev_addr;
static uint32_t s_last_cfg_freq;
static struct i2c_bus_stub { int id; } s_bus_obj;
static struct i2c_dev_stub { int id; } s_dev_objs[16];
static int s_dev_next;

esp_err_t i2c_new_master_bus(const i2c_master_bus_config_t *cfg, i2c_master_bus_handle_t *out)
{
    if (s_fail_next_bus != 0) {
        esp_err_t e = s_fail_next_bus;
        s_fail_next_bus = 0;
        return e;
    }
    s_last_cfg_sda = cfg->sda_io_num;
    s_last_cfg_scl = cfg->scl_io_num;
    s_buses_created++;
    *out = &s_bus_obj;
    return ESP_OK;
}

esp_err_t i2c_del_master_bus(i2c_master_bus_handle_t bus)
{
    (void)bus;
    s_buses_deleted++;
    return ESP_OK;
}

esp_err_t i2c_master_bus_add_device(i2c_master_bus_handle_t bus, const i2c_device_config_t *cfg,
                                    i2c_master_dev_handle_t *out)
{
    (void)bus;
    s_last_cfg_freq = cfg->scl_speed_hz;
    s_last_dev_addr = cfg->device_address;
    s_devs_added++;
    *out = &s_dev_objs[s_dev_next++ % 16];
    return ESP_OK;
}

esp_err_t i2c_master_bus_rm_device(i2c_master_dev_handle_t dev)
{
    (void)dev;
    s_devs_removed++;
    return ESP_OK;
}

esp_err_t i2c_master_transmit(i2c_master_dev_handle_t dev, const uint8_t *w, size_t n, int timeout_ms)
{
    (void)dev;
    (void)timeout_ms;
    s_transfers++;
    s_last_write_len = n < sizeof(s_last_write) ? n : sizeof(s_last_write);
    memcpy(s_last_write, w, s_last_write_len);
    return ESP_OK;
}

esp_err_t i2c_master_transmit_receive(i2c_master_dev_handle_t dev, const uint8_t *w, size_t wn, uint8_t *r,
                                      size_t rn, int timeout_ms)
{
    (void)dev;
    (void)timeout_ms;
    s_transfers++;
    s_last_write_len = wn < sizeof(s_last_write) ? wn : sizeof(s_last_write);
    memcpy(s_last_write, w, s_last_write_len);
    memset(r, 0xA5, rn); /* a recognisable "device answered" pattern */
    return ESP_OK;
}

/* --- FreeRTOS and ESP bits synapse_i2c.c needs ------------------------------------------------ */
SemaphoreHandle_t xSemaphoreCreateMutexStatic(StaticSemaphore_t *b) { return (SemaphoreHandle_t)b; }
BaseType_t xSemaphoreTake(SemaphoreHandle_t m, TickType_t t) { (void)m; (void)t; return pdTRUE; }
void xSemaphoreGive(SemaphoreHandle_t m) { (void)m; }
const char *esp_err_to_name(esp_err_t e) { (void)e; return "stub"; }

/* --- what the Python test drives ---------------------------------------------------------------- */
/* Counters are cleared by hand; the module's own state is cleared by releasing, which is safe when
 * nothing is held (that is part of what is tested). */
void h_reset(void)
{
    for (int i = 0; i < 8; i++) {
        synapse_i2c_release(0);
        synapse_i2c_release(1);
    }
    s_buses_created = s_buses_deleted = s_devs_added = s_devs_removed = s_transfers = 0;
    s_fail_next_bus = 0;
    s_dev_next = 0;
    s_last_write_len = 0;
}

int h_acquire(int port, int sda, int scl, unsigned freq) { return synapse_i2c_acquire(port, sda, scl, freq); }
void h_release(int port) { synapse_i2c_release(port); }
int h_write(int port, int addr, const uint8_t *d, size_t n) { return synapse_i2c_write(port, (uint8_t)addr, d, n); }
int h_read_reg(int port, int addr, uint8_t reg, uint8_t *out, size_t n)
{
    return synapse_i2c_write_read(port, (uint8_t)addr, &reg, 1, out, n);
}
void h_fail_next_bus(int err) { s_fail_next_bus = err; }

int h_buses_created(void) { return s_buses_created; }
int h_buses_deleted(void) { return s_buses_deleted; }
int h_devs_added(void) { return s_devs_added; }
int h_devs_removed(void) { return s_devs_removed; }
int h_transfers(void) { return s_transfers; }
int h_last_sda(void) { return s_last_cfg_sda; }
int h_last_scl(void) { return s_last_cfg_scl; }
unsigned h_last_dev_freq(void) { return s_last_cfg_freq; }
int h_last_dev_addr(void) { return s_last_dev_addr; }
int h_last_write_len(void) { return (int)s_last_write_len; }
int h_last_write_byte(int i) { return s_last_write[i]; }
int h_default_sda(void) { return SYNAPSE_I2C_DEFAULT_SDA_GPIO; }
int h_default_scl(void) { return SYNAPSE_I2C_DEFAULT_SCL_GPIO; }
unsigned h_default_freq(void) { return SYNAPSE_I2C_DEFAULT_FREQ_HZ; }
int h_err_invalid_state(void) { return ESP_ERR_INVALID_STATE; }
int h_err_invalid_arg(void) { return ESP_ERR_INVALID_ARG; }
