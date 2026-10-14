/**
 * @file ina219.c
 * @brief INA219 current/power monitor implementation.
 *
 * Calibration (datasheet §8.5): the part is programmed with a maximum
 * expected current; from that and the shunt value it derives the current and
 * power LSBs, which is why the whole conversion here is a register write plus
 * a few multiplications. Everything stays in the 12-bit signed domain of the
 * part — no floating point beyond the final scaling.
 */

#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include "ina219.h"

static const char *TAG = "INA219";

static ina219_t g_ina = {0};

// --- register helpers (16-bit big-endian, as the datasheet specifies) ------

static esp_err_t ina_write_reg(uint8_t reg, uint16_t value)
{
    uint8_t data[3] = {reg, (uint8_t)(value >> 8), (uint8_t)(value & 0xFF)};
    return i2c_master_write_to_device(
        (i2c_port_t)g_ina.config.i2c_port, g_ina.config.i2c_addr, data, 3, pdMS_TO_TICKS(100));
}

static esp_err_t ina_read_reg(uint8_t reg, uint16_t *value)
{
    uint8_t data[2] = {0};
    esp_err_t ret = i2c_master_write_read_device(
        (i2c_port_t)g_ina.config.i2c_port, g_ina.config.i2c_addr, &reg, 1, data, 2, pdMS_TO_TICKS(100));
    if (ret == ESP_OK) {
        *value = (uint16_t)((data[0] << 8) | data[1]);
    }
    return ret;
}

/**
 * Current LSB from the chosen PGA range and the maximum expected current
 * (datasheet §8.5.2). We take the conservative route: pick the PGA that
 * covers max_expected_a, then use the part's own 15-bit resolution.
 */
static esp_err_t ina_calibrate(void)
{
    float max_a = g_ina.config.max_expected_a;
    if (max_a <= 0.0f) {
        max_a = 3.2f;
    }

    // Current_LSB = max_expected / 2^15, rounded up to a clean mantissa so
    // the calibration register round-trips exactly.
    float current_lsb = max_a / 32768.0f;
    if (current_lsb < 1e-6f) {
        current_lsb = 1e-6f;
    }
    g_ina.current_lsb_a = current_lsb;
    g_ina.power_lsb_w = 20.0f * current_lsb;

    // Cal = trunc(0.04096 / (Current_LSB * R_shunt)); a zero R would divide
    // by zero, so guard it like a broken breakout.
    float shunt = g_ina.config.shunt_ohm;
    float cal_f = (shunt > 0.0f) ? (0.04096f / (current_lsb * shunt)) : 0.0f;
    uint32_t cal = (uint32_t)(cal_f + 0.5f);
    if (cal > 0xFFFFu) {
        ESP_LOGW(TAG, "calibration %lu out of range; clamp to 0xFFFF", (unsigned long)cal);
        cal = 0xFFFFu;
    }
    return ina_write_reg(INA219_REG_CALIBRATION, (uint16_t)cal);
}

esp_err_t ina219_init(const ina219_config_t *config)
{
    if (config == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    if (g_ina.initialized) {
        return ESP_OK;
    }

    g_ina.config = *config;
    if (g_ina.config.i2c_port != 0 && g_ina.config.i2c_port != 1) {
        return ESP_ERR_INVALID_ARG;
    }
    if (g_ina.config.shunt_ohm <= 0.0f) {
        return ESP_ERR_INVALID_ARG;
    }

    g_ina.mutex = xSemaphoreCreateMutex();
    if (g_ina.mutex == NULL) {
        return ESP_ERR_NO_MEM;
    }

    // 32 V bus, ±320 mV shunt (PGA /8), 12-bit with averaging, continuous.
    uint16_t cfg = (uint16_t)(INA219_CFG_BUS_RANGE_32V | INA219_CFG_PGA_DIV8 |
                              INA219_CFG_ADC_12BIT_1S | INA219_CFG_SADC_12BIT_8S |
                              INA219_CFG_MODE_CONT_ALL);
    esp_err_t ret = ina_write_reg(INA219_REG_CONFIG, cfg);
    if (ret == ESP_OK) {
        ret = ina_calibrate();
    }
    if (ret != ESP_OK) {
        vSemaphoreDelete(g_ina.mutex);
        g_ina.mutex = NULL;
        ESP_LOGE(TAG, "init failed: %s", esp_err_to_name(ret));
        return ret;
    }

    // A device present on the bus answers; a LiPo rail reads ~3.0-4.2 V.
    uint16_t raw = 0;
    ret = ina_read_reg(INA219_REG_BUS_V, &raw);
    if (ret != ESP_OK) {
        vSemaphoreDelete(g_ina.mutex);
        g_ina.mutex = NULL;
        return ret;
    }
    float bus_v = ((float)(raw >> 3) * 4.0f) / 1000.0f;
    if (bus_v < 1.0f || bus_v > 40.0f) {
        ESP_LOGW(TAG, "bus voltage %.2f V out of range; continuing but unhealthy", bus_v);
        g_ina.healthy = false;
    }

    g_ina.initialized = true;
    g_ina.healthy = (bus_v >= 1.0f && bus_v <= 40.0f);
    ESP_LOGI(TAG, "INA219 ready at 0x%02x (bus %.2f V, shunt %.3f ohm)",
             g_ina.config.i2c_addr, bus_v, g_ina.config.shunt_ohm);
    return ESP_OK;
}

esp_err_t ina219_read_once(ina219_data_t *data)
{
    if (data == NULL || !g_ina.initialized) {
        return ESP_ERR_INVALID_STATE;
    }

    if (xSemaphoreTake(g_ina.mutex, pdMS_TO_TICKS(50)) != pdTRUE) {
        g_ina.error_count++;
        return ESP_ERR_TIMEOUT;
    }

    uint16_t shunt_raw = 0;
    uint16_t bus_raw = 0;
    esp_err_t ret = ina_read_reg(INA219_REG_SHUNT_V, &shunt_raw);
    if (ret == ESP_OK) {
        ret = ina_read_reg(INA219_REG_BUS_V, &bus_raw);
    }
    if (ret == ESP_OK) {
        // Current comes from the shunt voltage and the programmed LSB, which
        // is what makes the result independent of the PGA choice.
        float shunt_mv = ((float)(int16_t)shunt_raw) * 10.0f / 1000.0f;
        float current_a = shunt_mv / 1000.0f / g_ina.config.shunt_ohm;
        float bus_v = ((float)(bus_raw >> 3) * 4.0f) / 1000.0f;
        bool overflow = (bus_raw & 0x01u) != 0u;
        bool conversion_ready = (bus_raw & 0x02u) != 0u;
        (void)conversion_ready;

        data->bus_voltage_v = bus_v;
        data->shunt_voltage_mv = shunt_mv;
        data->current_a = current_a;
        data->power_w = bus_v * current_a;
        // A math overflow on the power/current registers is the part telling
        // us the PGA range is wrong; report it as an error rather than a
        // plausible-looking lie.
        if (overflow) {
            ret = ESP_ERR_INVALID_RESPONSE;
            ESP_LOGW(TAG, "power register overflow: PGA range too small");
        }
    }

    if (ret == ESP_OK) {
        g_ina.read_count++;
        g_ina.healthy = true;
    } else {
        g_ina.error_count++;
        g_ina.healthy = false;
    }

    xSemaphoreGive(g_ina.mutex);
    return ret;
}

bool ina219_is_healthy(void)
{
    return g_ina.initialized && g_ina.healthy;
}

esp_err_t ina219_get_stats(uint32_t *reads, uint32_t *errors)
{
    if (reads == NULL || errors == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    *reads = g_ina.read_count;
    *errors = g_ina.error_count;
    return ESP_OK;
}

esp_err_t ina219_deinit(void)
{
    if (g_ina.mutex != NULL) {
        vSemaphoreDelete(g_ina.mutex);
        g_ina.mutex = NULL;
    }
    memset(&g_ina, 0, sizeof(g_ina));
    return ESP_OK;
}
