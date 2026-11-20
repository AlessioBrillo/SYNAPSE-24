/**
 * @file synapse_i2c.c
 * @brief Shared I2C bus owner (see synapse_i2c.h) on the ESP-IDF 5.x i2c_master driver.
 *
 * The legacy driver/i2c.h is deprecated in IDF 5.x and cannot coexist with the new one in one
 * image, so every sensor goes through this file. One bus per port, a device handle per 7-bit
 * address created on first use, and a reference count so the last driver to leave removes it.
 */

#include "synapse_i2c.h"

#include <stdbool.h>
#include <string.h>
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "synapse_i2c";

#define SYNAPSE_I2C_MAX_DEVICES 8   /* PPG, IMU, magnetometer, TMP117, INA219 and room for the next */
#define SYNAPSE_I2C_TIMEOUT_MS  100

typedef struct {
    i2c_master_bus_handle_t bus;
    int refs;
    int sda;
    int scl;
    uint32_t freq_hz;
    int ndev;
    uint8_t addr[SYNAPSE_I2C_MAX_DEVICES];
    i2c_master_dev_handle_t dev[SYNAPSE_I2C_MAX_DEVICES];
} bus_t;

static bus_t s_bus[SYNAPSE_I2C_PORTS];

/* The table is touched from the sensor tasks and from the power init: one lock. It is created
 * lazily because there is no earlier single-threaded place to do it; the creation itself is the
 * only part that needs the spinlock (it never blocks). The transaction runs outside the lock,
 * the driver serialises the bus on its own. */
static StaticSemaphore_t s_lock_storage;
static SemaphoreHandle_t s_lock;
static portMUX_TYPE s_create_mux = portMUX_INITIALIZER_UNLOCKED;

static SemaphoreHandle_t lock_get(void)
{
    portENTER_CRITICAL(&s_create_mux);
    if (s_lock == NULL) {
        s_lock = xSemaphoreCreateMutexStatic(&s_lock_storage);
    }
    portEXIT_CRITICAL(&s_create_mux);
    return s_lock;
}

static bool port_ok(int port)
{
    return port >= 0 && port < SYNAPSE_I2C_PORTS;
}

esp_err_t synapse_i2c_acquire(int port, int sda_gpio, int scl_gpio, uint32_t freq_hz)
{
    if (!port_ok(port)) {
        return ESP_ERR_INVALID_ARG;
    }
    if (sda_gpio < 0) sda_gpio = SYNAPSE_I2C_DEFAULT_SDA_GPIO;
    if (scl_gpio < 0) scl_gpio = SYNAPSE_I2C_DEFAULT_SCL_GPIO;
    if (freq_hz == 0) freq_hz = SYNAPSE_I2C_DEFAULT_FREQ_HZ;

    SemaphoreHandle_t lock = lock_get();
    xSemaphoreTake(lock, portMAX_DELAY);
    bus_t *b = &s_bus[port];
    esp_err_t err = ESP_OK;

    if (b->refs > 0) {
        if (b->sda != sda_gpio || b->scl != scl_gpio || b->freq_hz != freq_hz) {
            ESP_LOGE(TAG, "port %d is already wired SDA=%d SCL=%d at %lu Hz; refusing SDA=%d SCL=%d at %lu Hz",
                     port, b->sda, b->scl, (unsigned long)b->freq_hz, sda_gpio, scl_gpio, (unsigned long)freq_hz);
            err = ESP_ERR_INVALID_STATE;
        } else {
            b->refs++;
        }
    } else {
        i2c_master_bus_config_t cfg = {
            .i2c_port = port,
            .sda_io_num = sda_gpio,
            .scl_io_num = scl_gpio,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .flags.enable_internal_pullup = true,  /* breakouts usually carry their own; harmless in parallel */
        };
        err = i2c_new_master_bus(&cfg, &b->bus);
        if (err == ESP_OK) {
            b->refs = 1;
            b->sda = sda_gpio;
            b->scl = scl_gpio;
            b->freq_hz = freq_hz;
            b->ndev = 0;
            ESP_LOGI(TAG, "port %d up: SDA=%d SCL=%d %lu Hz", port, sda_gpio, scl_gpio, (unsigned long)freq_hz);
        } else {
            ESP_LOGE(TAG, "port %d bus creation failed: %s", port, esp_err_to_name(err));
        }
    }
    xSemaphoreGive(lock);
    return err;
}

void synapse_i2c_release(int port)
{
    if (!port_ok(port)) {
        return;
    }
    SemaphoreHandle_t lock = lock_get();
    xSemaphoreTake(lock, portMAX_DELAY);
    bus_t *b = &s_bus[port];
    if (b->refs > 0 && --b->refs == 0) {
        for (int i = 0; i < b->ndev; i++) {
            (void)i2c_master_bus_rm_device(b->dev[i]);
        }
        (void)i2c_del_master_bus(b->bus);
        ESP_LOGI(TAG, "port %d down (last user left)", port);
        memset(b, 0, sizeof(*b));
    }
    xSemaphoreGive(lock);
}

/* Device handle for `addr` on `port`, created on first use. */
static esp_err_t device_get(int port, uint8_t addr, i2c_master_dev_handle_t *out)
{
    if (!port_ok(port)) {
        return ESP_ERR_INVALID_ARG;
    }
    SemaphoreHandle_t lock = lock_get();
    xSemaphoreTake(lock, portMAX_DELAY);
    bus_t *b = &s_bus[port];
    esp_err_t err = ESP_ERR_INVALID_STATE;  /* nobody acquired the bus */
    if (b->refs > 0) {
        err = ESP_OK;
        *out = NULL;
        for (int i = 0; i < b->ndev; i++) {
            if (b->addr[i] == addr) {
                *out = b->dev[i];
                break;
            }
        }
        if (*out == NULL) {
            if (b->ndev >= SYNAPSE_I2C_MAX_DEVICES) {
                err = ESP_ERR_NO_MEM;
            } else {
                i2c_device_config_t dc = {
                    .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                    .device_address = addr,
                    .scl_speed_hz = b->freq_hz,
                };
                i2c_master_dev_handle_t h = NULL;
                err = i2c_master_bus_add_device(b->bus, &dc, &h);
                if (err == ESP_OK) {
                    b->addr[b->ndev] = addr;
                    b->dev[b->ndev] = h;
                    b->ndev++;
                    *out = h;
                }
            }
        }
    }
    xSemaphoreGive(lock);
    return err;
}

esp_err_t synapse_i2c_write(int port, uint8_t addr, const uint8_t *data, size_t len)
{
    if (data == NULL || len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    i2c_master_dev_handle_t h = NULL;
    esp_err_t err = device_get(port, addr, &h);
    if (err != ESP_OK) {
        return err;
    }
    return i2c_master_transmit(h, data, len, SYNAPSE_I2C_TIMEOUT_MS);
}

esp_err_t synapse_i2c_write_read(int port, uint8_t addr, const uint8_t *write, size_t write_len,
                                 uint8_t *read, size_t read_len)
{
    if (write == NULL || write_len == 0 || read == NULL || read_len == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    i2c_master_dev_handle_t h = NULL;
    esp_err_t err = device_get(port, addr, &h);
    if (err != ESP_OK) {
        return err;
    }
    return i2c_master_transmit_receive(h, write, write_len, read, read_len, SYNAPSE_I2C_TIMEOUT_MS);
}
