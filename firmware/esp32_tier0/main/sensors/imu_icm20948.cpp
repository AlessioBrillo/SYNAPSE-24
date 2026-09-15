/**
 * @file imu_icm20948.cpp
 * @brief ICM-20948 9-Axis IMU Driver Implementation
 *
 * ICM-20948 Configuration:
 * - 3-axis accelerometer: ±4g (default), 100 Hz
 * - 3-axis gyroscope: ±500 dps (default), 100 Hz
 * - 3-axis magnetometer (AK09916): ±4900 µT, 100 Hz
 * - I2C address: 0x68 (AD0=GND)
 * - DMP: Disabled (using raw data for ACC cross-correlation)
 * - FIFO: Enabled for batch reading
 */

#include "imu_icm20948.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cmath>
#include <cstring>

static const char* TAG = "IMU_ICM20948";

// ICM-20948 Register Map (User Bank 0)
#define ICM20948_REG_WHO_AM_I         0x00
#define ICM20948_REG_USER_CTRL        0x03
#define ICM20948_REG_LP_CONFIG        0x05
#define ICM20948_REG_PWR_MGMT_1       0x06
#define ICM20948_REG_PWR_MGMT_2       0x07
#define ICM20948_REG_INT_PIN_CFG      0x0F
#define ICM20948_REG_INT_ENABLE       0x10
#define ICM20948_REG_INT_STATUS       0x19
#define ICM20948_REG_ACCEL_XOUT_H     0x2D
#define ICM20948_REG_GYRO_XOUT_H      0x33
#define ICM20948_REG_TEMP_OUT_H       0x39
#define ICM20948_REG_EXT_SLV_SENS_DATA_00 0x3B
#define ICM20948_REG_FIFO_EN_1        0x66
#define ICM20948_REG_FIFO_EN_2        0x67
#define ICM20948_REG_FIFO_RST         0x68
#define ICM20948_REG_FIFO_MODE        0x69
#define ICM20948_REG_FIFO_COUNTH      0x70
#define ICM20948_REG_FIFO_COUNTL      0x71
#define ICM20948_REG_FIFO_R_W         0x72
#define ICM20948_REG_FIFO_CFG         0x73
#define ICM20948_REG_REG_BANK_SEL     0x7F

// User Bank 1 (Accel/Gyro config)
#define ICM20948_REG_ACCEL_SMPLRT_DIV_1  0x10
#define ICM20948_REG_ACCEL_SMPLRT_DIV_2  0x11
#define ICM20948_REG_ACCEL_CONFIG        0x14
#define ICM20948_REG_GYRO_SMPLRT_DIV     0x12
#define ICM20948_REG_GYRO_CONFIG_1       0x13

// User Bank 2 (Mag config)
#define ICM20948_REG_MAG_XOUT_H          0x03

// Bank Select values
#define ICM20948_BANK_0                 0x00
#define ICM20948_BANK_1                 0x10
#define ICM20948_BANK_2                 0x20
#define ICM20948_BANK_3                 0x30

// PWR_MGMT_1 bits
#define ICM20948_PWR_MGMT_1_SLEEP       (1 << 6)
#define ICM20948_PWR_MGMT_1_CLKSEL_AUTO (0x01)

// ACCEL_CONFIG bits
#define ICM20948_ACCEL_FCHOICE_B        (1 << 3)
#define ICM20948_ACCEL_FS_2G            (0 << 1)
#define ICM20948_ACCEL_FS_4G            (1 << 1)
#define ICM20948_ACCEL_FS_8G            (2 << 1)
#define ICM20948_ACCEL_FS_16G           (3 << 1)
#define ICM20948_ACCEL_DLPFCFG_246HZ    (0 << 0)
#define ICM20948_ACCEL_DLPFCFG_111HZ    (1 << 0)
#define ICM20948_ACCEL_DLPFCFG_50HZ     (2 << 0)
#define ICM20948_ACCEL_DLPFCFG_24HZ     (3 << 0)
#define ICM20948_ACCEL_DLPFCFG_12HZ     (4 << 0)
#define ICM20948_ACCEL_DLPFCFG_6HZ      (5 << 0)

// GYRO_CONFIG_1 bits
#define ICM20948_GYRO_FCHOICE_B         (1 << 3)
#define ICM20948_GYRO_FS_250DPS         (0 << 1)
#define ICM20948_GYRO_FS_500DPS         (1 << 1)
#define ICM20948_GYRO_FS_1000DPS        (2 << 1)
#define ICM20948_GYRO_FS_2000DPS        (3 << 1)
#define ICM20948_GYRO_DLPFCFG_196HZ     (0 << 0)
#define ICM20948_GYRO_DLPFCFG_151HZ     (1 << 0)
#define ICM20948_GYRO_DLPFCFG_119HZ     (2 << 0)
#define ICM20948_GYRO_DLPFCFG_51HZ      (3 << 0)
#define ICM20948_GYRO_DLPFCFG_23HZ      (4 << 0)
#define ICM20948_GYRO_DLPFCFG_11HZ      (5 << 0)

// USER_CTRL bits
#define ICM20948_USER_CTRL_DMP_EN       (1 << 7)
#define ICM20948_USER_CTRL_FIFO_EN      (1 << 6)
#define ICM20948_USER_CTRL_I2C_MST_EN   (1 << 5)
#define ICM20948_USER_CTRL_I2C_IF_DIS   (1 << 4)

// Expected WHO_AM_I
#define ICM20948_WHO_AM_I_VAL           0xEA

// Magnetometer AK09916
#define AK09916_I2C_ADDR                0x0C
#define AK09916_REG_WHO_AM_I            0x00
#define AK09916_REG_ST1                 0x10
#define AK09916_REG_HXL                 0x11
#define AK09916_REG_CNTL2               0x31
#define AK09916_WHO_AM_I_VAL            0x09
#define AK09916_MODE_CONT_100HZ         0x08

// Gyro bias calibration
static int16_t s_gyro_bias_x = 0, s_gyro_bias_y = 0, s_gyro_bias_z = 0;
static bool s_mag_enabled = true;

// Scale factors
static float s_accel_scale = 0.0f;  // mg/LSB
static float s_gyro_scale = 0.0f;   // mdps/LSB

// I2C Helpers
static esp_err_t i2c_write_reg(uint8_t reg, uint8_t value) {
    uint8_t data[2] = {reg, value};
    return i2c_master_write_to_device(I2C_NUM_0, SYNAPSE_IMU_I2C_ADDR, data, 2, pdMS_TO_TICKS(100));
}

static esp_err_t i2c_read_reg(uint8_t reg, uint8_t* value) {
    return i2c_master_write_read_device(I2C_NUM_0, SYNAPSE_IMU_I2C_ADDR, &reg, 1, value, 1, pdMS_TO_TICKS(100));
}

static esp_err_t i2c_read_regs(uint8_t reg, uint8_t* buffer, size_t len) {
    return i2c_master_write_read_device(I2C_NUM_0, SYNAPSE_IMU_I2C_ADDR, &reg, 1, buffer, len, pdMS_TO_TICKS(100));
}

static esp_err_t select_bank(uint8_t bank) {
    return i2c_write_reg(ICM20948_REG_REG_BANK_SEL, bank);
}

// ============================================================================
// INITIALIZATION
// ============================================================================

esp_err_t imu_icm20948_init(void) {
    ESP_LOGI(TAG, "Initializing ICM-20948 IMU...");

    // Verify WHO_AM_I
    uint8_t who_am_i = 0;
    esp_err_t err = i2c_read_reg(ICM20948_REG_WHO_AM_I, &who_am_i);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read WHO_AM_I: %s", esp_err_to_name(err));
        return err;
    }
    if (who_am_i != ICM20948_WHO_AM_I_VAL) {
        ESP_LOGE(TAG, "Invalid WHO_AM_I: 0x%02X (expected 0x%02X)", who_am_i, ICM20948_WHO_AM_I_VAL);
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "ICM-20948 detected (WHO_AM_I: 0x%02X)", who_am_i);

    // Wake up device
    err = i2c_write_reg(ICM20948_REG_PWR_MGMT_1, ICM20948_PWR_MGMT_1_CLKSEL_AUTO);
    if (err != ESP_OK) return err;
    vTaskDelay(pdMS_TO_TICKS(10));

    // Disable I2C master (we'll use pass-through for magnetometer)
    err = i2c_write_reg(ICM20948_REG_USER_CTRL, 0x00);
    if (err != ESP_OK) return err;

    // Select Bank 1 for accel/gyro config
    err = select_bank(ICM20948_BANK_1);
    if (err != ESP_OK) return err;

    // Configure accelerometer: ±4g, 100 Hz, DLPF 50Hz
    // Sample rate divider: 1125/(1+divider) = 100 -> divider = 10.25 -> use 10
    err = i2c_write_reg(ICM20948_REG_ACCEL_SMPLRT_DIV_1, 0x00);
    if (err != ESP_OK) return err;
    err = i2c_write_reg(ICM20948_REG_ACCEL_SMPLRT_DIV_2, 0x0A);
    if (err != ESP_OK) return err;

    err = i2c_write_reg(ICM20948_REG_ACCEL_CONFIG,
                        ICM20948_ACCEL_FCHOICE_B | ICM20948_ACCEL_FS_4G | ICM20948_ACCEL_DLPFCFG_50HZ);
    if (err != ESP_OK) return err;
    s_accel_scale = 0.122f; // 4g/32768 * 1000 = 0.122 mg/LSB

    // Configure gyroscope: ±500 dps, 100 Hz, DLPF 51Hz
    err = i2c_write_reg(ICM20948_REG_GYRO_SMPLRT_DIV, 0x0A);
    if (err != ESP_OK) return err;

    err = i2c_write_reg(ICM20948_REG_GYRO_CONFIG_1,
                        ICM20948_GYRO_FCHOICE_B | ICM20948_GYRO_FS_500DPS | ICM20948_GYRO_DLPFCFG_51HZ);
    if (err != ESP_OK) return err;
    s_gyro_scale = 16.4f; // 500dps/32768 * 1000 = 15.26 mdps/LSB (use 16.4 for 500dps)

    // Return to Bank 0
    err = select_bank(ICM20948_BANK_0);
    if (err != ESP_OK) return err;

    // Enable FIFO for accel + gyro
    err = i2c_write_reg(ICM20948_REG_FIFO_EN_1, 0xF0); // Accel + Gyro enabled
    if (err != ESP_OK) return err;
    err = i2c_write_reg(ICM20948_REG_FIFO_EN_2, 0x00);
    if (err != ESP_OK) return err;

    err = i2c_write_reg(ICM20948_REG_FIFO_MODE, 0x00); // Stream mode
    if (err != ESP_OK) return err;

    err = i2c_write_reg(ICM20948_REG_FIFO_RST, 0x1F); // Reset all FIFOs
    if (err != ESP_OK) return err;
    err = i2c_write_reg(ICM20948_REG_FIFO_RST, 0x00);
    if (err != ESP_OK) return err;

    err = i2c_write_reg(ICM20948_REG_USER_CTRL, ICM20948_USER_CTRL_FIFO_EN);
    if (err != ESP_OK) return err;

    // Initialize magnetometer via I2C pass-through
    if (s_mag_enabled) {
        // Enable I2C master pass-through
        err = i2c_write_reg(ICM20948_REG_INT_PIN_CFG, 0x02); // BYPASS_EN
        if (err != ESP_OK) return err;
        vTaskDelay(pdMS_TO_TICKS(10));

        // Verify AK09916
        uint8_t mag_who = 0;
        err = i2c_master_write_read_device(I2C_NUM_0, AK09916_I2C_ADDR,
                                           (uint8_t[]){AK09916_REG_WHO_AM_I}, 1, &mag_who, 1, pdMS_TO_TICKS(100));
        if (err == ESP_OK && mag_who == AK09916_WHO_AM_I_VAL) {
            // Configure continuous 100Hz
            i2c_master_write_to_device(I2C_NUM_0, AK09916_I2C_ADDR,
                                       (uint8_t[]){AK09916_REG_CNTL2, AK09916_MODE_CONT_100HZ}, 2, pdMS_TO_TICKS(100));
            ESP_LOGI(TAG, "AK09916 magnetometer initialized");
        } else {
            ESP_LOGW(TAG, "AK09916 not detected, magnetometer disabled");
            s_mag_enabled = false;
        }
    }

    // Calibrate gyro bias
    imu_icm20948_calibrate_gyro();

    ESP_LOGI(TAG, "ICM-20948 initialized: Accel=±4g, Gyro=±500dps, Mag=%s, Rate=100Hz",
             s_mag_enabled ? "enabled" : "disabled");
    return ESP_OK;
}

// ============================================================================
// SINGLE SAMPLE READ
// ============================================================================

esp_err_t imu_icm20948_read_sample(synapse_imu_sample_t* sample) {
    if (!sample) return ESP_ERR_INVALID_ARG;

    // Read 6 accel + 6 gyro + 6 mag = 18 bytes from FIFO or direct registers
    uint8_t buffer[18];
    esp_err_t err = i2c_read_regs(ICM20948_REG_ACCEL_XOUT_H, buffer, 18);
    if (err != ESP_OK) return err;

    // Parse accelerometer (mg)
    sample->accel_x = (int16_t)(((int16_t)buffer[0] << 8) | buffer[1]);
    sample->accel_y = (int16_t)(((int16_t)buffer[2] << 8) | buffer[3]);
    sample->accel_z = (int16_t)(((int16_t)buffer[4] << 8) | buffer[5]);

    // Apply scale: raw * 0.122 = mg
    sample->accel_x = (int16_t)(sample->accel_x * s_accel_scale);
    sample->accel_y = (int16_t)(sample->accel_y * s_accel_scale);
    sample->accel_z = (int16_t)(sample->accel_z * s_accel_scale);

    // Parse gyroscope (mdps)
    sample->gyro_x = (int16_t)(((int16_t)buffer[6] << 8) | buffer[7]);
    sample->gyro_y = (int16_t)(((int16_t)buffer[8] << 8) | buffer[9]);
    sample->gyro_z = (int16_t)(((int16_t)buffer[10] << 8) | buffer[11]);

    // Apply bias correction and scale
    sample->gyro_x = (int16_t)((sample->gyro_x - s_gyro_bias_x) * s_gyro_scale);
    sample->gyro_y = (int16_t)((sample->gyro_y - s_gyro_bias_y) * s_gyro_scale);
    sample->gyro_z = (int16_t)((sample->gyro_z - s_gyro_bias_z) * s_gyro_scale);

    // Parse magnetometer (µT) - from AK09916 via pass-through or EXT_SLV
    if (s_mag_enabled) {
        // Read from EXT_SLV_SENS_DATA_00 (first 6 bytes of mag data)
        sample->mag_x = (int16_t)(((int16_t)buffer[12] << 8) | buffer[13]);
        sample->mag_y = (int16_t)(((int16_t)buffer[14] << 8) | buffer[15]);
        sample->mag_z = (int16_t)(((int16_t)buffer[16] << 8) | buffer[17]);
        // AK09916: 0.15 µT/LSB
        sample->mag_x = (int16_t)(sample->mag_x * 0.15f);
        sample->mag_y = (int16_t)(sample->mag_y * 0.15f);
        sample->mag_z = (int16_t)(sample->mag_z * 0.15f);
    } else {
        sample->mag_x = sample->mag_y = sample->mag_z = 0;
    }

    sample->timestamp_us = esp_timer_get_time();

    return ESP_OK;
}

// ============================================================================
// CONFIGURATION
// ============================================================================

esp_err_t imu_icm20948_configure_ranges(uint8_t accel_range_g, uint16_t gyro_range_dps) {
    esp_err_t err = select_bank(ICM20948_BANK_1);
    if (err != ESP_OK) return err;

    uint8_t accel_fs = ICM20948_ACCEL_FS_4G;
    switch (accel_range_g) {
        case 2:  accel_fs = ICM20948_ACCEL_FS_2G;  s_accel_scale = 0.061f; break;
        case 4:  accel_fs = ICM20948_ACCEL_FS_4G;  s_accel_scale = 0.122f; break;
        case 8:  accel_fs = ICM20948_ACCEL_FS_8G;  s_accel_scale = 0.244f; break;
        case 16: accel_fs = ICM20948_ACCEL_FS_16G; s_accel_scale = 0.488f; break;
    }
    err = i2c_write_reg(ICM20948_REG_ACCEL_CONFIG,
                        ICM20948_ACCEL_FCHOICE_B | accel_fs | ICM20948_ACCEL_DLPFCFG_50HZ);
    if (err != ESP_OK) return err;

    uint8_t gyro_fs = ICM20948_GYRO_FS_500DPS;
    switch (gyro_range_dps) {
        case 250:  gyro_fs = ICM20948_GYRO_FS_250DPS;  s_gyro_scale = 8.2f; break;
        case 500:  gyro_fs = ICM20948_GYRO_FS_500DPS;  s_gyro_scale = 16.4f; break;
        case 1000: gyro_fs = ICM20948_GYRO_FS_1000DPS; s_gyro_scale = 32.8f; break;
        case 2000: gyro_fs = ICM20948_GYRO_FS_2000DPS; s_gyro_scale = 65.5f; break;
    }
    err = i2c_write_reg(ICM20948_REG_GYRO_CONFIG_1,
                        ICM20948_GYRO_FCHOICE_B | gyro_fs | ICM20948_GYRO_DLPFCFG_51HZ);
    if (err != ESP_OK) return err;

    err = select_bank(ICM20948_BANK_0);
    return err;
}

esp_err_t imu_icm20948_set_magnetometer(bool enable) {
    s_mag_enabled = enable;
    if (enable) {
        // Enable I2C master pass-through
        return i2c_write_reg(ICM20948_REG_INT_PIN_CFG, 0x02);
    } else {
        // Disable pass-through
        return i2c_write_reg(ICM20948_REG_INT_PIN_CFG, 0x00);
    }
}

esp_err_t imu_icm20948_read_magnetometer(int16_t* mag_x, int16_t* mag_y, int16_t* mag_z) {
    if (!mag_x || !mag_y || !mag_z || !s_mag_enabled) return ESP_ERR_INVALID_ARG;

    // Read directly from AK09916 via I2C pass-through
    uint8_t buffer[6];
    esp_err_t err = i2c_master_write_read_device(I2C_NUM_0, AK09916_I2C_ADDR,
                                                  (uint8_t[]){AK09916_REG_HXL}, 1, buffer, 6, pdMS_TO_TICKS(100));
    if (err != ESP_OK) return err;

    *mag_x = (int16_t)(((int16_t)buffer[0] << 8) | buffer[1]);
    *mag_y = (int16_t)(((int16_t)buffer[2] << 8) | buffer[3]);
    *mag_z = (int16_t)(((int16_t)buffer[4] << 8) | buffer[5]);

    *mag_x = (int16_t)(*mag_x * 0.15f);
    *mag_y = (int16_t)(*mag_y * 0.15f);
    *mag_z = (int16_t)(*mag_z * 0.15f);

    return ESP_OK;
}

// ============================================================================
// CALIBRATION
// ============================================================================

void imu_icm20948_calibrate_gyro(void) {
    ESP_LOGI(TAG, "Calibrating gyroscope bias (keep device still)...");

    const int num_samples = 500;
    int64_t sum_x = 0, sum_y = 0, sum_z = 0;

    for (int i = 0; i < num_samples; i++) {
        uint8_t buffer[6];
        if (i2c_read_regs(ICM20948_REG_GYRO_XOUT_H, buffer, 6) == ESP_OK) {
            int16_t gx = (int16_t)(((int16_t)buffer[0] << 8) | buffer[1]);
            int16_t gy = (int16_t)(((int16_t)buffer[2] << 8) | buffer[3]);
            int16_t gz = (int16_t)(((int16_t)buffer[4] << 8) | buffer[5]);
            sum_x += gx;
            sum_y += gy;
            sum_z += gz;
        }
        vTaskDelay(pdMS_TO_TICKS(2));
    }

    s_gyro_bias_x = (int16_t)(sum_x / num_samples);
    s_gyro_bias_y = (int16_t)(sum_y / num_samples);
    s_gyro_bias_z = (int16_t)(sum_z / num_samples);

    ESP_LOGI(TAG, "Gyro bias calibrated: x=%d, y=%d, z=%d (raw LSB)",
             s_gyro_bias_x, s_gyro_bias_y, s_gyro_bias_z);
}

// ============================================================================
// UTILITIES
// ============================================================================

int16_t imu_icm20948_get_accel_magnitude(void) {
    synapse_imu_sample_t sample;
    if (imu_icm20948_read_sample(&sample) == ESP_OK) {
        // Magnitude in mg
        int32_t mag_sq = (int32_t)sample.accel_x * sample.accel_x +
                         (int32_t)sample.accel_y * sample.accel_y +
                         (int32_t)sample.accel_z * sample.accel_z;
        return (int16_t)sqrtf((float)mag_sq);
    }
    return 0;
}

void imu_icm20948_deinit(void) {
    // Sleep mode
    i2c_write_reg(ICM20948_REG_PWR_MGMT_1, ICM20948_PWR_MGMT_1_SLEEP);
    ESP_LOGI(TAG, "ICM-20948 sleep mode");
}