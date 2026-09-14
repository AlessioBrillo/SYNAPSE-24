/**
 * @file ppg_max30102.cpp
 * @brief MAX30102 PPG Driver Implementation
 *
 * MAX30102 Configuration:
 * - Dual wavelength: Red (660nm) + IR (880nm)
 * - I2C address: 0x57
 * - Sample rate: 64 Hz (normative for Tier 0)
 * - LED current: 6.4mA each (0x1F)
 * - Pulse width: 411 µs
 * - ADC range: 16384 nA (max)
 * - FIFO: 32 samples, rollover enabled
 */

#include "ppg_max30102.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <cstring>

static const char* TAG = "PPG_MAX30102";

// MAX30102 Register Map
#define MAX30102_REG_INT_STATUS1      0x00
#define MAX30102_REG_INT_STATUS2      0x01
#define MAX30102_REG_INT_ENABLE1      0x02
#define MAX30102_REG_INT_ENABLE2      0x03
#define MAX30102_REG_FIFO_WR_PTR      0x04
#define MAX30102_REG_FIFO_OVF_CNT     0x05
#define MAX30102_REG_FIFO_RD_PTR      0x06
#define MAX30102_REG_FIFO_DATA        0x07
#define MAX30102_REG_FIFO_CONFIG      0x08
#define MAX30102_REG_MODE_CONFIG      0x09
#define MAX30102_REG_SPO2_CONFIG      0x0A
#define MAX30102_REG_LED1_PA          0x0C
#define MAX30102_REG_LED2_PA          0x0D
#define MAX30102_REG_PILOT_PA         0x10
#define MAX30102_REG_MULTI_LED_CTRL1  0x11
#define MAX30102_REG_MULTI_LED_CTRL2  0x12
#define MAX30102_REG_TEMP_INT         0x1F
#define MAX30102_REG_TEMP_FRAC        0x20
#define MAX30102_REG_TEMP_CONFIG      0x21
#define MAX30102_REG_PROX_INT_THRESH  0x30
#define MAX30102_REG_REV_ID           0xFE
#define MAX30102_REG_PART_ID          0xFF

// Mode Config bits
#define MAX30102_MODE_SHDN            (1 << 7)
#define MAX30102_MODE_RESET           (1 << 6)
#define MAX30102_MODE_SPO2            0x03  // SpO2 mode (Red + IR)

// SPO2 Config bits
#define MAX30102_SPO2_ADC_RGE_2048    (0 << 5)
#define MAX30102_SPO2_ADC_RGE_4096    (1 << 5)
#define MAX30102_SPO2_ADC_RGE_8192    (2 << 5)
#define MAX30102_SPO2_ADC_RGE_16384   (3 << 5)
#define MAX30102_SPO2_SR_50           (0 << 2)
#define MAX30102_SPO2_SR_100          (1 << 2)
#define MAX30102_SPO2_SR_200          (2 << 2)
#define MAX30102_SPO2_SR_400          (3 << 2)
#define MAX30102_SPO2_SR_800          (4 << 2)
#define MAX30102_SPO2_SR_1000         (5 << 2)
#define MAX30102_SPO2_SR_1600         (6 << 2)
#define MAX30102_SPO2_SR_3200         (7 << 2)
#define MAX30102_SPO2_PW_69           (0 << 0)
#define MAX30102_SPO2_PW_118          (1 << 0)
#define MAX30102_SPO2_PW_215          (2 << 0)
#define MAX30102_SPO2_PW_411          (3 << 0)

// FIFO Config
#define MAX30102_FIFO_SMP_AVE_1       (0 << 5)
#define MAX30102_FIFO_SMP_AVE_2       (1 << 5)
#define MAX30102_FIFO_SMP_AVE_4       (2 << 5)
#define MAX30102_FIFO_SMP_AVE_8       (3 << 5)
#define MAX30102_FIFO_SMP_AVE_16      (4 << 5)
#define MAX30102_FIFO_SMP_AVE_32      (5 << 5)
#define MAX30102_FIFO_ROLLOVER_EN     (1 << 4)
#define MAX30102_FIFO_A_FULL_32       (0x00 << 0)  // Interrupt when 32 samples left

// Expected Part ID
#define MAX30102_PART_ID_VAL          0x15

// I2C Helpers
static esp_err_t i2c_write_reg(uint8_t reg, uint8_t value) {
    uint8_t data[2] = {reg, value};
    return i2c_master_write_to_device(I2C_NUM_0, SYNAPSE_PPG_I2C_ADDR, data, 2, pdMS_TO_TICKS(100));
}

static esp_err_t i2c_read_reg(uint8_t reg, uint8_t* value) {
    return i2c_master_write_read_device(I2C_NUM_0, SYNAPSE_PPG_I2C_ADDR, &reg, 1, value, 1, pdMS_TO_TICKS(100));
}

static esp_err_t i2c_read_fifo(uint8_t* buffer, size_t len) {
    uint8_t reg = MAX30102_REG_FIFO_DATA;
    return i2c_master_write_read_device(I2C_NUM_0, SYNAPSE_PPG_I2C_ADDR, &reg, 1, buffer, len, pdMS_TO_TICKS(100));
}

// ============================================================================
// INITIALIZATION
// ============================================================================

esp_err_t ppg_max30102_init(void) {
    ESP_LOGI(TAG, "Initializing MAX30102 PPG sensor...");

    // Verify Part ID
    uint8_t part_id = 0;
    esp_err_t err = i2c_read_reg(MAX30102_REG_PART_ID, &part_id);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read Part ID: %s", esp_err_to_name(err));
        return err;
    }
    if (part_id != MAX30102_PART_ID_VAL) {
        ESP_LOGE(TAG, "Invalid Part ID: 0x%02X (expected 0x%02X)", part_id, MAX30102_PART_ID_VAL);
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "MAX30102 detected (Part ID: 0x%02X)", part_id);

    // Reset device
    err = i2c_write_reg(MAX30102_REG_MODE_CONFIG, MAX30102_MODE_RESET);
    if (err != ESP_OK) return err;
    vTaskDelay(pdMS_TO_TICKS(100)); // Wait for reset

    // Configure FIFO: rollover enabled, no averaging, interrupt at 32 samples
    err = i2c_write_reg(MAX30102_REG_FIFO_CONFIG,
                        MAX30102_FIFO_ROLLOVER_EN | MAX30102_FIFO_SMP_AVE_1 | MAX30102_FIFO_A_FULL_32);
    if (err != ESP_OK) return err;

    // Configure Mode: SpO2 mode (Red + IR)
    err = i2c_write_reg(MAX30102_REG_MODE_CONFIG, MAX30102_MODE_SPO2);
    if (err != ESP_OK) return err;

    // Default SpO2 configuration: 64 Hz, 411 µs pulse, 16384 nA range
    err = ppg_max30102_configure(0x1F, 0x1F, 64, 411);
    if (err != ESP_OK) return err;

    // Clear interrupts
    uint8_t dummy;
    i2c_read_reg(MAX30102_REG_INT_STATUS1, &dummy);
    i2c_read_reg(MAX30102_REG_INT_STATUS2, &dummy);

    // Enable FIFO almost full interrupt
    err = i2c_write_reg(MAX30102_REG_INT_ENABLE1, 0x80); // A_FULL_EN
    if (err != ESP_OK) return err;
    err = i2c_write_reg(MAX30102_REG_INT_ENABLE2, 0x00);
    if (err != ESP_OK) return err;

    ESP_LOGI(TAG, "MAX30102 initialized: 64 Hz, Red+IR, 6.4mA, 411µs");
    return ESP_OK;
}

// ============================================================================
// CONFIGURATION
// ============================================================================

esp_err_t ppg_max30102_configure(uint8_t led_current_red, uint8_t led_current_ir,
                                  uint16_t sample_rate, uint16_t pulse_width) {
    // LED currents (0x00-0x3F, each step ~0.2mA, max 50mA)
    esp_err_t err = i2c_write_reg(MAX30102_REG_LED1_PA, led_current_red);
    if (err != ESP_OK) return err;
    err = i2c_write_reg(MAX30102_REG_LED2_PA, led_current_ir);
    if (err != ESP_OK) return err;

    // Sample rate and pulse width
    uint8_t spo2_cfg = MAX30102_SPO2_ADC_RGE_16384; // 16384 nA range
    switch (sample_rate) {
        case 50:   spo2_cfg |= MAX30102_SPO2_SR_50;   break;
        case 100:  spo2_cfg |= MAX30102_SPO2_SR_100;  break;
        case 200:  spo2_cfg |= MAX30102_SPO2_SR_200;  break;
        case 400:  spo2_cfg |= MAX30102_SPO2_SR_400;  break;
        case 800:  spo2_cfg |= MAX30102_SPO2_SR_800;  break;
        case 1000: spo2_cfg |= MAX30102_SPO2_SR_1000; break;
        case 1600: spo2_cfg |= MAX30102_SPO2_SR_1600; break;
        case 3200: spo2_cfg |= MAX30102_SPO2_SR_3200; break;
        default:   spo2_cfg |= MAX30102_SPO2_SR_100;  break;
    }
    switch (pulse_width) {
        case 69:  spo2_cfg |= MAX30102_SPO2_PW_69;  break;
        case 118: spo2_cfg |= MAX30102_SPO2_PW_118; break;
        case 215: spo2_cfg |= MAX30102_SPO2_PW_215; break;
        case 411: spo2_cfg |= MAX30102_SPO2_PW_411; break;
        default:  spo2_cfg |= MAX30102_SPO2_PW_411; break;
    }
    err = i2c_write_reg(MAX30102_REG_SPO2_CONFIG, spo2_cfg);
    if (err != ESP_OK) return err;

    ESP_LOGI(TAG, "MAX30102 configured: Red=%dmA, IR=%dmA, %dHz, %dµs",
             led_current_red * 200, led_current_ir * 200, sample_rate, pulse_width);
    return ESP_OK;
}

// ============================================================================
// SINGLE SAMPLE READ (for normative rate)
// ============================================================================

esp_err_t ppg_max30102_read_sample(synapse_ppg_sample_t* sample) {
    if (!sample) return ESP_ERR_INVALID_ARG;

    // Check FIFO for available samples
    uint8_t wr_ptr, rd_ptr;
    esp_err_t err = i2c_read_reg(MAX30102_REG_FIFO_WR_PTR, &wr_ptr);
    if (err != ESP_OK) return err;
    err = i2c_read_reg(MAX30102_REG_FIFO_RD_PTR, &rd_ptr);
    if (err != ESP_OK) return err;

    // Calculate available samples
    uint8_t available = (wr_ptr - rd_ptr) & 0x1F; // 32-sample FIFO (5 bits)
    if (available == 0) {
        return ESP_ERR_TIMEOUT; // No new samples
    }

    // Read one sample (6 bytes: 3 bytes Red + 3 bytes IR)
    uint8_t fifo_data[6];
    err = i2c_read_fifo(fifo_data, 6);
    if (err != ESP_OK) return err;

    // Parse 18-bit values (3 bytes each, MSB first)
    sample->red = ((uint32_t)fifo_data[0] << 16) | ((uint32_t)fifo_data[1] << 8) | fifo_data[2];
    sample->ir  = ((uint32_t)fifo_data[3] << 16) | ((uint32_t)fifo_data[4] << 8) | fifo_data[5];

    // Mask to 18 bits
    sample->red &= 0x3FFFF;
    sample->ir  &= 0x3FFFF;

    sample->timestamp_us = esp_timer_get_time();

    return ESP_OK;
}

// ============================================================================
// BATCH FIFO READ
// ============================================================================

esp_err_t ppg_max30102_read_fifo(synapse_ppg_sample_t* samples, size_t max_samples, size_t* actual_samples) {
    if (!samples || !actual_samples) return ESP_ERR_INVALID_ARG;

    uint8_t wr_ptr, rd_ptr;
    esp_err_t err = i2c_read_reg(MAX30102_REG_FIFO_WR_PTR, &wr_ptr);
    if (err != ESP_OK) return err;
    err = i2c_read_reg(MAX30102_REG_FIFO_RD_PTR, &rd_ptr);
    if (err != ESP_OK) return err;

    uint8_t available = (wr_ptr - rd_ptr) & 0x1F;
    size_t to_read = (available < max_samples) ? available : max_samples;

    if (to_read == 0) {
        *actual_samples = 0;
        return ESP_OK;
    }

    // Read all samples (6 bytes each)
    uint8_t* buffer = new uint8_t[to_read * 6];
    err = i2c_read_fifo(buffer, to_read * 6);
    if (err != ESP_OK) {
        delete[] buffer;
        return err;
    }

    int64_t base_time = esp_timer_get_time();
    int64_t period_us = 1000000 / SYNAPSE_PPG_SAMPLING_RATE_HZ;

    for (size_t i = 0; i < to_read; i++) {
        size_t offset = i * 6;
        samples[i].red = ((uint32_t)buffer[offset] << 16) |
                         ((uint32_t)buffer[offset + 1] << 8) |
                         buffer[offset + 2];
        samples[i].ir = ((uint32_t)buffer[offset + 3] << 16) |
                        ((uint32_t)buffer[offset + 4] << 8) |
                        buffer[offset + 5];
        samples[i].red &= 0x3FFFF;
        samples[i].ir  &= 0x3FFFF;
        samples[i].timestamp_us = base_time + (int64_t)i * period_us;
    }

    *actual_samples = to_read;
    delete[] buffer;
    return ESP_OK;
}

// ============================================================================
// FIFO INTERRUPT CONTROL
// ============================================================================

esp_err_t ppg_max30102_set_fifo_interrupt(bool enable) {
    uint8_t reg = enable ? 0x80 : 0x00; // A_FULL_EN bit
    return i2c_write_reg(MAX30102_REG_INT_ENABLE1, reg);
}

// ============================================================================
// DEINITIALIZATION
// ============================================================================

void ppg_max30102_deinit(void) {
    // Shutdown
    i2c_write_reg(MAX30102_REG_MODE_CONFIG, MAX30102_MODE_SHDN);
    ESP_LOGI(TAG, "MAX30102 shutdown");
}