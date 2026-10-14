/* Host-side gate for the INA219 current/power monitor.
 *
 * firmware/esp32_band/sensors/ina219.c can only run on real hardware, but its
 * conversion arithmetic (register -> volts/amps/watts, calibration register,
 * overflow handling) is pure logic that must not wait for a bench to be
 * verified. This glue simulates an I²C device with scripted register values
 * and compiles the driver against them with -Wall -Wextra -Werror.
 *
 * Wire format (datasheet): 16-bit big-endian registers, register pointer in
 * the first byte of a write, read back on a repeated start.
 */

#include <string.h>
#include "ina219.h"

#include "esp_err.h"
#include "driver/i2c.h"

/* --- simulated device ---------------------------------------------------
 * 16 registers of 16 bits, plus the write buffer the driver last sent so the
 * test can assert the calibration register it programmed. */
static uint16_t s_regs[16];
static uint8_t s_last_write[8];
static size_t s_last_write_len;
static esp_err_t s_next_read_error;

void ina_host_regs_reset(void)
{
    memset(s_regs, 0, sizeof(s_regs));
    s_last_write_len = 0;
    s_next_read_error = ESP_OK;
}

void ina_host_set_register(uint8_t reg, uint16_t value)
{
    if (reg < 16) {
        s_regs[reg] = value;
    }
}

void ina_host_set_read_error(esp_err_t err)
{
    s_next_read_error = err;
}

uint16_t ina_host_last_written_register_value(void)
{
    if (s_last_write_len < 3) {
        return 0;
    }
    return (uint16_t)((s_last_write[1] << 8) | s_last_write[2]);
}

uint8_t ina_host_last_written_register_address(void)
{
    return s_last_write_len ? s_last_write[0] : 0xFF;
}

esp_err_t i2c_master_write_to_device(i2c_port_t port, uint8_t addr,
                                     const uint8_t *data, size_t len,
                                     int timeout_ticks)
{
    (void)port;
    (void)addr;
    (void)timeout_ticks;
    if (len > sizeof(s_last_write)) {
        return ESP_ERR_INVALID_SIZE;
    }
    memcpy(s_last_write, data, len);
    s_last_write_len = len;
    if (len == 3 && data[0] < 16) {
        s_regs[data[0]] = (uint16_t)((data[1] << 8) | data[2]);
    }
    return ESP_OK;
}

esp_err_t i2c_master_write_read_device(i2c_port_t port, uint8_t addr,
                                       const uint8_t *write, size_t write_len,
                                       uint8_t *read, size_t read_len,
                                       int timeout_ticks)
{
    (void)port;
    (void)addr;
    (void)timeout_ticks;
    if (write_len != 1 || write[0] >= 16 || read_len != 2) {
        return ESP_ERR_INVALID_ARG;
    }
    if (s_next_read_error != ESP_OK) {
        return s_next_read_error;
    }
    read[0] = (uint8_t)(s_regs[write[0]] >> 8);
    read[1] = (uint8_t)(s_regs[write[0]] & 0xFF);
    return ESP_OK;
}

/* --- driver-side symbols ------------------------------------------------ */

esp_err_t ina219_init(const ina219_config_t *config);
esp_err_t ina219_read_once(ina219_data_t *data);
bool ina219_is_healthy(void);
esp_err_t ina219_get_stats(uint32_t *reads, uint32_t *errors);

SemaphoreHandle_t xSemaphoreCreateMutex(void) { return (SemaphoreHandle_t)1; }
BaseType_t xSemaphoreTake(SemaphoreHandle_t mutex, TickType_t ticks)
{
    (void)mutex;
    (void)ticks;
    return pdTRUE;
}
void xSemaphoreGive(SemaphoreHandle_t mutex) { (void)mutex; }
void vSemaphoreDelete(SemaphoreHandle_t mutex) { (void)mutex; }
const char *esp_err_to_name(esp_err_t err) { (void)err; return "stub"; }

int ina_host_smoke_main(void)
{
    /* A 3.7 V rail with a 0.1 ohm shunt drawing 100 mA: the classic LiPo
     * operating point the rig will actually sit at during a Tier-0 soak. */
    ina_host_regs_reset();
    // Bus voltage 3.7 V: 925 LSB of 4 mV (right-aligned by 3). Staged before
    // init because init reads the bus once to sanity-check the part.
    ina_host_set_register(INA219_REG_BUS_V, (uint16_t)(925u << 3));
    // 100 mA across 0.1 ohm = 10 mV = (as the driver measures it) shunt raw.
    // The driver derives current from the shunt volts, so 10 mV -> 0.1 A.
    ina_host_set_register(INA219_REG_SHUNT_V, 1000);  // 10 mV at 10 uV/LSB

    // A previous initialisation would short-circuit the fresh register
    // programming this check is about, so start from a deinit'd driver.
    ina219_deinit();
    ina219_config_t cfg = {
        .i2c_port = 0,
        .i2c_addr = 0x40,
        .shunt_ohm = 0.1f,
        .max_expected_a = 2.0f,
    };
    if (ina219_init(&cfg) != ESP_OK) {
        return 1;
    }
    if (!ina219_is_healthy()) {
        return 2;
    }
    // init programs CONFIG and then CALIBRATION; the last write is the
    // calibration register, which is what makes the readings meaningful.
    if (ina_host_last_written_register_address() != INA219_REG_CALIBRATION) {
        return 3;
    }

    ina219_data_t data = {0};
    if (ina219_read_once(&data) != ESP_OK) {
        return 4;
    }
    if (data.bus_voltage_v < 3.65f || data.bus_voltage_v > 3.75f) {
        return 5;
    }
    if (data.current_a < 0.09f || data.current_a > 0.11f) {
        return 6;
    }
    if (data.power_w < 0.33f || data.power_w > 0.42f) {
        return 7;
    }
    return 0;
}

/* Reset driver state, simulated registers and statistics, then re-init with
 * the same configuration the smoke uses: each Python test starts from a
 * device that has just booted. */
void ina_host_reset(void)
{
    ina219_deinit();
    ina_host_regs_reset();
    ina_host_set_register(INA219_REG_BUS_V, (uint16_t)(925u << 3));
    ina_host_set_register(INA219_REG_SHUNT_V, 1000);
    ina219_config_t cfg = {
        .i2c_port = 0,
        .i2c_addr = 0x40,
        .shunt_ohm = 0.1f,
        .max_expected_a = 2.0f,
    };
    ina219_init(&cfg);
}

/* --- python-facing helpers ---------------------------------------------
 * The module resets the simulated device between calls, so the Python test
 * scripts registers and then reads through the same driver entry point the
 * firmware uses. */

static ina219_data_t s_last_data;

int ina_host_read_once(void)
{
    return ina219_read_once(&s_last_data) == ESP_OK ? 0 : 1;
}

float ina_host_bus_v(void) { return s_last_data.bus_voltage_v; }
float ina_host_shunt_mv(void) { return s_last_data.shunt_voltage_mv; }
float ina_host_current_a(void) { return s_last_data.current_a; }
float ina_host_power_w(void) { return s_last_data.power_w; }

int ina_host_read_once_raw(ina219_data_t *out)
{
    return ina219_read_once(out) == ESP_OK ? 0 : 1;
}
