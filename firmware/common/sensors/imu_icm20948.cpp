#include "imu_icm20948.h"
#include "imu_odr.h"
#include "synapse_selftest.h"
#include "esp_log.h"
#include <stdio.h>
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>
#include <math.h>

static const char* TAG = "imu_icm20948";

// Init and hot-path errors are returned, never ESP_ERROR_CHECK'd: a loose wire
// must degrade one sensor, not reboot-loop the band.
#define CHK(x) do { esp_err_t _e = (x); if (_e != ESP_OK) { \
    ESP_LOGE(TAG, "%s failed: %s", #x, esp_err_to_name(_e)); return _e; } } while (0)

#define ICM20948_REG_BANK_SEL       0x7F
#define ICM20948_REG_WHO_AM_I       0x00
#define ICM20948_REG_PWR_MGMT_1     0x06
#define ICM20948_REG_PWR_MGMT_2     0x07
#define ICM20948_REG_ACCEL_XOUT_H   0x2D
#define ICM20948_REG_GYRO_XOUT_H    0x33
#define ICM20948_REG_MAG_XOUT_H     0x03
#define ICM20948_REG_USER_CTRL      0x03
#define ICM20948_REG_I2C_MST_CTRL   0x01
#define ICM20948_REG_ACCEL_CONFIG   0x14
#define ICM20948_REG_GYRO_CONFIG_1  0x01
#define ICM20948_REG_GYRO_CONFIG_2  0x02
#define ICM20948_REG_INT_PIN_CFG    0x0F   // bank 0; bit1 = BYPASS_EN
#define ICM20948_REG_GYRO_SMPLRT_DIV    0x00   // bank 2
#define ICM20948_REG_ACCEL_SMPLRT_DIV_1 0x10   // bank 2 (high byte)
#define ICM20948_REG_ACCEL_SMPLRT_DIV_2 0x11   // bank 2 (low byte)
#define ICM20948_ACCEL_BASE_ODR_HZ  1125       // accel output rate with DLPF enabled, before the divider
#define ICM20948_GYRO_BASE_ODR_HZ   1100       // gyro base rate differs from the accel's

#define ICM20948_AK09916_ADDR       0x0C
#define AK09916_REG_WIA1            0x00
#define AK09916_REG_WIA2            0x01
#define AK09916_REG_ST1             0x10
#define AK09916_REG_HXL             0x11
#define AK09916_REG_ST2             0x18
#define AK09916_REG_CNTL2           0x31
#define AK09916_REG_CNTL3           0x32

#define ICM20948_BANK_0             0x00
#define ICM20948_BANK_2             0x20
#define ICM20948_BANK_3             0x30

typedef struct {
    imu_icm20948_config_t config;
    bool initialized;
    bool bus_held;     // this driver owns a reference on the shared I2C bus (synapse_i2c)
    float accel_scale;
    float gyro_scale;
    float mag_scale;
    int16_t mag_raw[3];  // last valid magnetometer sample (held while the AK09916 is not ready)
} imu_icm20948_ctx_t;

static imu_icm20948_ctx_t s_ctx = {};

static esp_err_t i2c_write_reg(int port, int addr, uint8_t reg, uint8_t value) {
    const uint8_t buf[2] = {reg, value};
    return synapse_i2c_write(port, (uint8_t)addr, buf, sizeof(buf));
}

static esp_err_t i2c_read_reg(int port, int addr, uint8_t reg, uint8_t* value) {
    return synapse_i2c_write_read(port, (uint8_t)addr, &reg, 1, value, 1);
}

static esp_err_t i2c_read_multi(int port, int addr, uint8_t reg, uint8_t* data, size_t len) {
    return synapse_i2c_write_read(port, (uint8_t)addr, &reg, 1, data, len);
}

static esp_err_t icm20948_select_bank(uint8_t bank) {
    return i2c_write_reg(s_ctx.config.i2c_port, s_ctx.config.i2c_addr, ICM20948_REG_BANK_SEL, bank);
}

// The AK09916 is reached through the ICM-20948 bypass (INT_PIN_CFG.BYPASS_EN with
// the internal I2C master off), so it answers at 0x0C directly on the host bus.
// No bank switching is involved.
static esp_err_t ak09916_write_reg(uint8_t reg, uint8_t value) {
    return i2c_write_reg(s_ctx.config.i2c_port, ICM20948_AK09916_ADDR, reg, value);
}

static esp_err_t ak09916_read_reg(uint8_t reg, uint8_t* value) {
    return i2c_read_reg(s_ctx.config.i2c_port, ICM20948_AK09916_ADDR, reg, value);
}

// Everything after the bus is up. Split from init so that a failure at any step goes through
// one place that gives the bus reference back.
static esp_err_t imu_configure(const imu_icm20948_config_t* config) {
    // A range or rate the part cannot do is an error, not a silent fall back to +-2 g / +-250 dps:
    // that used to scale every sample by the wrong factor without any sign of it.
    const int a = config->accel_fsr_g;
    const int g = config->gyro_fsr_dps;
    if (!(a == 2 || a == 4 || a == 8 || a == 16) ||
        !(g == 250 || g == 500 || g == 1000 || g == 2000) ||
        config->accel_odr_hz < 1 || config->accel_odr_hz > ICM20948_ACCEL_BASE_ODR_HZ ||
        config->gyro_odr_hz < 1 || config->gyro_odr_hz > ICM20948_GYRO_BASE_ODR_HZ) {
        ESP_LOGE(TAG, "invalid IMU config: accel %d g (2/4/8/16) @ %d Hz (1-%d), gyro %d dps (250/500/1000/2000) @ %d Hz (1-%d)",
                 a, config->accel_odr_hz, ICM20948_ACCEL_BASE_ODR_HZ, g, config->gyro_odr_hz, ICM20948_GYRO_BASE_ODR_HZ);
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t whoami;
    CHK(i2c_read_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_WHO_AM_I, &whoami));
    if (whoami != 0xEA) {
        ESP_LOGE(TAG, "Invalid WHO_AM_I: 0x%02X (expected 0xEA)", whoami);
        return ESP_ERR_NOT_FOUND;
    }

    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_PWR_MGMT_1, 0x01));
    vTaskDelay(pdMS_TO_TICKS(100));
    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_PWR_MGMT_2, 0x00));
    vTaskDelay(pdMS_TO_TICKS(10));

    // Internal I2C master OFF + bypass ON: the magnetometer is wired to the host bus.
    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_USER_CTRL, 0x00));
    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_INT_PIN_CFG, 0x02));
    vTaskDelay(pdMS_TO_TICKS(10));

    CHK(icm20948_select_bank(ICM20948_BANK_2));
    uint8_t accel_fsr = 0;
    switch (config->accel_fsr_g) {
        case 2: accel_fsr = 0x00; s_ctx.accel_scale = 2.0f / 32768.0f; break;
        case 4: accel_fsr = 0x02; s_ctx.accel_scale = 4.0f / 32768.0f; break;
        case 8: accel_fsr = 0x04; s_ctx.accel_scale = 8.0f / 32768.0f; break;
        case 16: accel_fsr = 0x06; s_ctx.accel_scale = 16.0f / 32768.0f; break;
        default: return ESP_ERR_INVALID_ARG;  // unreachable: validated above
    }
    // DLPF on (FCHOICE=1) at ~24 Hz so a 50 Hz poll does not alias.
    // ponytail: fixed filter, only right for ODR <= 50 Hz; derive it from the ODR if that ever rises.
    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_ACCEL_CONFIG, accel_fsr | 0x20 | 0x01));
    uint32_t adiv = imu_smplrt_div_for(ICM20948_ACCEL_BASE_ODR_HZ, (uint32_t)config->accel_odr_hz);
    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_ACCEL_SMPLRT_DIV_1, (uint8_t)(adiv >> 8)));
    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_ACCEL_SMPLRT_DIV_2, (uint8_t)(adiv & 0xFF)));
    CHK(icm20948_select_bank(ICM20948_BANK_0));

    CHK(icm20948_select_bank(ICM20948_BANK_2));
    uint8_t gyro_fsr = 0;
    switch (config->gyro_fsr_dps) {
        case 250: gyro_fsr = 0x00; s_ctx.gyro_scale = 250.0f / 32768.0f; break;
        case 500: gyro_fsr = 0x02; s_ctx.gyro_scale = 500.0f / 32768.0f; break;
        case 1000: gyro_fsr = 0x04; s_ctx.gyro_scale = 1000.0f / 32768.0f; break;
        case 2000: gyro_fsr = 0x06; s_ctx.gyro_scale = 2000.0f / 32768.0f; break;
        default: return ESP_ERR_INVALID_ARG;  // unreachable: validated above
    }
    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_GYRO_CONFIG_1, gyro_fsr | 0x28 | 0x01));
    uint32_t gdiv = imu_smplrt_div_for(ICM20948_GYRO_BASE_ODR_HZ, (uint32_t)config->gyro_odr_hz);
    if (gdiv > 255) {  // 8-bit divider: the slowest gyro ODR is ~4.3 Hz
        ESP_LOGW(TAG, "gyro ODR %u Hz is below the divider range; running at the slowest rate", (unsigned)config->gyro_odr_hz);
        gdiv = 255;
    }
    CHK(i2c_write_reg(config->i2c_port, config->i2c_addr, ICM20948_REG_GYRO_SMPLRT_DIV, (uint8_t)gdiv));
    CHK(icm20948_select_bank(ICM20948_BANK_0));
    ESP_LOGI(TAG, "IMU poll %d/%d Hz: accel samples at %u mHz, gyro at %u mHz (x%u the poll, no duplicates)",
             config->accel_odr_hz, config->gyro_odr_hz,
             (unsigned)imu_odr_mhz(ICM20948_ACCEL_BASE_ODR_HZ, adiv),
             (unsigned)imu_odr_mhz(ICM20948_GYRO_BASE_ODR_HZ, gdiv), (unsigned)IMU_ODR_OVERSAMPLE);

    // The magnetometer is optional: accel/gyro keep working if it does not answer.
    s_ctx.mag_scale = 0.15f;
    memset(s_ctx.mag_raw, 0, sizeof(s_ctx.mag_raw));
    uint8_t wia1 = 0, wia2 = 0;
    if (ak09916_write_reg(AK09916_REG_CNTL3, 0x01) == ESP_OK) {
        vTaskDelay(pdMS_TO_TICKS(100));
        if (ak09916_read_reg(AK09916_REG_WIA1, &wia1) == ESP_OK &&
            ak09916_read_reg(AK09916_REG_WIA2, &wia2) == ESP_OK &&
            wia1 == 0x48 && wia2 == 0x09) {
            if (ak09916_write_reg(AK09916_REG_CNTL2, 0x08) != ESP_OK) {  // continuous mode 4 (100 Hz)
                ESP_LOGW(TAG, "AK09916 mode set failed: magnetometer stays at zero");
            }
        } else {
            ESP_LOGW(TAG, "AK09916 not found (WIA=0x%02X%02X, expected 0x4809): magnetometer stays at zero", wia1, wia2);
        }
    } else {
        ESP_LOGW(TAG, "AK09916 unreachable: magnetometer stays at zero");
    }

    if (config->gpio_int >= 0) {
        gpio_config_t io_conf = {
            .pin_bit_mask = (1ULL << config->gpio_int),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .intr_type = GPIO_INTR_NEGEDGE
        };
        gpio_config(&io_conf);
    }
    return ESP_OK;
}

esp_err_t imu_icm20948_init(const imu_icm20948_config_t* config) {
    if (!config) return ESP_ERR_INVALID_ARG;

    if (s_ctx.bus_held) {  // re-init without a deinit: do not leak the previous reference
        synapse_i2c_release(s_ctx.config.i2c_port);
        s_ctx.bus_held = false;
    }
    s_ctx.config = *config;
    s_ctx.initialized = false;

    esp_err_t err = synapse_i2c_acquire(config->i2c_port, config->sda_gpio_num, config->scl_gpio_num,
                                        config->i2c_freq_hz);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C bus unavailable: %s", esp_err_to_name(err));
        return err;
    }
    s_ctx.bus_held = true;

    err = imu_configure(config);
    if (err != ESP_OK) {
        synapse_i2c_release(config->i2c_port);  // a sensor that did not come up must not keep the bus alive
        s_ctx.bus_held = false;
        return err;
    }

    s_ctx.initialized = true;
    ESP_LOGI(TAG, "ICM-20948 IMU initialized (I2C port %d, addr 0x%02X, accel=%dg, gyro=%d dps)",
             config->i2c_port, config->i2c_addr, config->accel_fsr_g, config->gyro_fsr_dps);
    return ESP_OK;
}

esp_err_t imu_icm20948_read(sensor_sample_t* sample, void* user_ctx) {
    (void)user_ctx;

    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;

    // ACCEL_XOUT_H (0x2D) .. GYRO_ZOUT_L (0x38) are contiguous: one transaction instead of two,
    // so accel and gyro come from the same instant of the shadow registers.
    static_assert(ICM20948_REG_GYRO_XOUT_H == ICM20948_REG_ACCEL_XOUT_H + 6, "accel and gyro registers are contiguous");
    uint8_t data[12];
    CHK(i2c_read_multi(s_ctx.config.i2c_port, s_ctx.config.i2c_addr, ICM20948_REG_ACCEL_XOUT_H, data, 12));
    int16_t ax_raw = (int16_t)((data[0] << 8) | data[1]);
    int16_t ay_raw = (int16_t)((data[2] << 8) | data[3]);
    int16_t az_raw = (int16_t)((data[4] << 8) | data[5]);
    int16_t gx_raw = (int16_t)((data[6] << 8) | data[7]);
    int16_t gy_raw = (int16_t)((data[8] << 8) | data[9]);
    int16_t gz_raw = (int16_t)((data[10] << 8) | data[11]);

    // ST1, HXL..HZH, ST2 in one burst: reading ST2 releases the AK09916 data
    // protection, otherwise the output freezes after the first sample.
    // The mag runs at 100 Hz and is polled at the IMU rate, so a not-ready or
    // overflowed sample keeps the last valid one instead of failing the read.
    uint8_t mag[9];
    if (i2c_read_multi(s_ctx.config.i2c_port, ICM20948_AK09916_ADDR, AK09916_REG_ST1, mag, 9) == ESP_OK &&
        (mag[0] & 0x01) && !(mag[8] & 0x08)) {
        s_ctx.mag_raw[0] = (int16_t)((mag[2] << 8) | mag[1]);
        s_ctx.mag_raw[1] = (int16_t)((mag[4] << 8) | mag[3]);
        s_ctx.mag_raw[2] = (int16_t)((mag[6] << 8) | mag[5]);
    }
    int16_t mx_raw = s_ctx.mag_raw[0];
    int16_t my_raw = s_ctx.mag_raw[1];
    int16_t mz_raw = s_ctx.mag_raw[2];

    sample->data.imu.ax = ax_raw * s_ctx.accel_scale;
    sample->data.imu.ay = ay_raw * s_ctx.accel_scale;
    sample->data.imu.az = az_raw * s_ctx.accel_scale;
    sample->data.imu.gx = gx_raw * s_ctx.gyro_scale * (M_PI / 180.0f);
    sample->data.imu.gy = gy_raw * s_ctx.gyro_scale * (M_PI / 180.0f);
    sample->data.imu.gz = gz_raw * s_ctx.gyro_scale * (M_PI / 180.0f);
    sample->data.imu.mx = mx_raw * s_ctx.mag_scale;
    sample->data.imu.my = my_raw * s_ctx.mag_scale;
    sample->data.imu.mz = mz_raw * s_ctx.mag_scale;

    return ESP_OK;
}

esp_err_t imu_icm20948_deinit(void* user_ctx) {
    (void)user_ctx;
    // Only this driver's reference goes: the bus stays up for the PPG, TMP117 and INA219.
    if (s_ctx.bus_held) {
        synapse_i2c_release(s_ctx.config.i2c_port);
        s_ctx.bus_held = false;
    }
    s_ctx.initialized = false;
    ESP_LOGI(TAG, "ICM-20948 IMU deinitialized");
    return ESP_OK;
}

esp_err_t imu_icm20948_self_test(char* detail, size_t len) {
    if (!s_ctx.initialized) {
        snprintf(detail, len, "not initialised");
        return ESP_ERR_INVALID_STATE;
    }
    uint8_t who = 0;
    esp_err_t e = i2c_read_reg(s_ctx.config.i2c_port, s_ctx.config.i2c_addr, ICM20948_REG_WHO_AM_I, &who);
    if (e != ESP_OK) {
        snprintf(detail, len, "WHO_AM_I: %s", esp_err_to_name(e));
        return e;
    }
    if (who != 0xEA) {
        snprintf(detail, len, "WHO_AM_I 0x%02X, expected 0xEA", who);
        return ESP_FAIL;
    }

    // A real conversion through the same path the stream uses (scaled: g, rad/s, uT).
    sensor_sample_t s = {};
    e = imu_icm20948_read(&s, NULL);
    if (e != ESP_OK) {
        snprintf(detail, len, "read: %s", esp_err_to_name(e));
        return e;
    }
    const float ax = s.data.imu.ax, ay = s.data.imu.ay, az = s.data.imu.az;
    const float gx = s.data.imu.gx, gy = s.data.imu.gy, gz = s.data.imu.gz;
    const float mx = s.data.imu.mx, my = s.data.imu.my, mz = s.data.imu.mz;
    if (!st_imu_rest_ok(ax, ay, az, gx, gy, gz)) {
        snprintf(detail, len, "not at rest or scaled wrong: a=(%.2f %.2f %.2f) g, w=(%.2f %.2f %.2f) rad/s",
                 (double)ax, (double)ay, (double)az, (double)gx, (double)gy, (double)gz);
        return ESP_FAIL;
    }
    if (!st_mag_ok(mx, my, mz)) {
        snprintf(detail, len, "magnetometer reads (%.0f %.0f %.0f) uT: AK09916 missing or not running",
                 (double)mx, (double)my, (double)mz);
        return ESP_FAIL;
    }
    snprintf(detail, len, "id 0xEA, |a|=%.2f g at rest, mag %.0f uT",
             (double)sqrtf(ax * ax + ay * ay + az * az), (double)sqrtf(mx * mx + my * my + mz * mz));
    return ESP_OK;
}