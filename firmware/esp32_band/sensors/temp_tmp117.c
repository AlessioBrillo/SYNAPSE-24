/**
 * @file temp_tmp117.c
 * @brief TMP117 Temperature Sensor Implementation
 */

#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "driver/i2c.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "temp_tmp117.h"

static const char *TAG = "TEMP_TMP117";

static temp_tmp117_t g_temp = {0};

// I2C register read/write helpers
static esp_err_t i2c_write_reg(uint8_t reg, uint16_t value) {
    uint8_t data[3] = {reg, (value >> 8) & 0xFF, value & 0xFF};
    return i2c_master_write_to_device(g_temp.config.i2c_port, g_temp.config.i2c_addr, data, 3, pdMS_TO_TICKS(100));
}

static esp_err_t i2c_read_reg(uint8_t reg, uint16_t *value) {
    uint8_t data[2];
    esp_err_t ret = i2c_master_write_read_device(g_temp.config.i2c_port, g_temp.config.i2c_addr, &reg, 1, data, 2, pdMS_TO_TICKS(100));
    if (ret == ESP_OK) {
        *value = (data[0] << 8) | data[1];
    }
    return ret;
}

// Temperature conversion: raw 16-bit signed to Celsius
static float raw_to_celsius(int16_t raw) {
    return raw * TMP117_TEMP_LSB_C;
}

// Celsius to raw for limits
static int16_t celsius_to_raw(float celsius) {
    return (int16_t)roundf(celsius / TMP117_TEMP_LSB_C);
}

// Periodic reading task
static void temp_task_fn(void *arg) {
    (void)arg;
    TickType_t last_wake = xTaskGetTickCount();
    uint16_t cycle_ticks = pdMS_TO_TICKS(g_temp.config.conversion_cycle_ms);
    if (cycle_ticks == 0) cycle_ticks = pdMS_TO_TICKS(15); // Minimum 15.5ms

    ESP_LOGI(TAG, "Temperature task started (cycle: %d ms)", g_temp.config.conversion_cycle_ms);

    while (g_temp.running) {
        vTaskDelayUntil(&last_wake, cycle_ticks);

        temp_data_t data;
        if (temp_tmp117_read_once(&data) == ESP_OK) {
            if (xSemaphoreTake(g_temp.mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
                g_temp.latest_data = data;
                xSemaphoreGive(g_temp.mutex);
            }
        }
    }
    vTaskDelete(NULL);
}

esp_err_t temp_tmp117_init(const temp_tmp117_config_t *config) {
    if (g_temp.initialized) {
        ESP_LOGW(TAG, "Already initialized");
        return ESP_OK;
    }
    if (!config) return ESP_ERR_INVALID_ARG;

    memcpy(&g_temp.config, config, sizeof(temp_tmp117_config_t));
    
    // Default conversion cycle if not set
    if (g_temp.config.conversion_cycle_ms == 0) {
        g_temp.config.conversion_cycle_ms = 15; // 15.5ms default
    }
    
    // Create mutex
    g_temp.mutex = xSemaphoreCreateMutex();
    if (!g_temp.mutex) {
        ESP_LOGE(TAG, "Failed to create mutex");
        return ESP_ERR_NO_MEM;
    }
    
    // Verify device ID
    uint16_t device_id;
    esp_err_t ret = i2c_read_reg(TMP117_REG_DEVICE_ID, &device_id);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read device ID: %s", esp_err_to_name(ret));
        return ret;
    }
    if (device_id != TMP117_DEVICE_ID) {
        ESP_LOGE(TAG, "Invalid device ID: 0x%04X (expected 0x%04X)", device_id, TMP117_DEVICE_ID);
        return ESP_ERR_NOT_FOUND;
    }
    ESP_LOGI(TAG, "TMP117 detected (ID: 0x%04X)", device_id);
    
    // Configure sensor
    uint16_t cfg = TMP117_CFG_MODE_CONT | TMP117_CFG_CONV_CYCLE_15_5MS;
    if (!g_temp.config.continuous_mode) {
        cfg = TMP117_CFG_MODE_ONESHOT | TMP117_CFG_CONV_CYCLE_15_5MS;
    }
    // Set conversion cycle based on config
    if (g_temp.config.conversion_cycle_ms >= 16000) cfg |= TMP117_CFG_CONV_CYCLE_16S;
    else if (g_temp.config.conversion_cycle_ms >= 8000) cfg |= TMP117_CFG_CONV_CYCLE_8S;
    else if (g_temp.config.conversion_cycle_ms >= 4000) cfg |= TMP117_CFG_CONV_CYCLE_4S;
    else if (g_temp.config.conversion_cycle_ms >= 1000) cfg |= TMP117_CFG_CONV_CYCLE_1S;
    else if (g_temp.config.conversion_cycle_ms >= 500) cfg |= TMP117_CFG_CONV_CYCLE_500MS;
    else if (g_temp.config.conversion_cycle_ms >= 250) cfg |= TMP117_CFG_CONV_CYCLE_250MS;
    else if (g_temp.config.conversion_cycle_ms >= 125) cfg |= TMP117_CFG_CONV_CYCLE_125MS;
    // else 15.5ms (default)
    
    ret = i2c_write_reg(TMP117_REG_CONFIGURATION, cfg);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write configuration: %s", esp_err_to_name(ret));
        return ret;
    }
    
    // Initialize latest data
    memset(&g_temp.latest_data, 0, sizeof(temp_data_t));
    
    g_temp.initialized = true;
    ESP_LOGI(TAG, "TMP117 initialized on I2C%d (addr 0x%02X, cycle: %d ms, mode: %s)", 
             config->i2c_port, config->i2c_addr, config->conversion_cycle_ms,
             config->continuous_mode ? "continuous" : "one-shot");
    return ESP_OK;
}

esp_err_t temp_tmp117_start(void) {
    if (!g_temp.initialized) return ESP_ERR_INVALID_STATE;
    if (g_temp.running) return ESP_OK;
    
    g_temp.running = true;
    
    BaseType_t ret = xTaskCreate(temp_task_fn, "temp_task", 2048, NULL, 3, &g_temp.task);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create temperature task");
        g_temp.running = false;
        return ESP_ERR_NO_MEM;
    }
    
    ESP_LOGI(TAG, "Temperature monitoring started");
    return ESP_OK;
}

esp_err_t temp_tmp117_stop(void) {
    if (!g_temp.running) return ESP_OK;
    
    g_temp.running = false;
    if (g_temp.task) {
        vTaskDelay(pdMS_TO_TICKS(100));
        g_temp.task = NULL;
    }
    
    ESP_LOGI(TAG, "Temperature monitoring stopped");
    return ESP_OK;
}

esp_err_t temp_tmp117_deinit(void) {
    temp_tmp117_stop();
    
    if (g_temp.mutex) {
        vSemaphoreDelete(g_temp.mutex);
        g_temp.mutex = NULL;
    }
    
    g_temp.initialized = false;
    ESP_LOGI(TAG, "TMP117 deinitialized");
    return ESP_OK;
}

esp_err_t temp_tmp117_read_once(temp_data_t *data) {
    if (!data) return ESP_ERR_INVALID_ARG;
    if (!g_temp.initialized) return ESP_ERR_INVALID_STATE;
    
    uint16_t raw;
    esp_err_t ret = i2c_read_reg(TMP117_REG_TEMP_RESULT, &raw);
    if (ret != ESP_OK) {
        g_temp.error_count++;
        return ret;
    }
    
    int16_t temp_raw = (int16_t)raw;
    float temp_c = raw_to_celsius(temp_raw);
    
    data->temperature_c = temp_c;
    data->valid = true;
    data->timestamp_us = esp_timer_get_time();
    
    g_temp.read_count++;
    return ESP_OK;
}

esp_err_t temp_tmp117_get_latest(temp_data_t *data) {
    if (!data) return ESP_ERR_INVALID_ARG;
    if (!g_temp.initialized) return ESP_ERR_INVALID_STATE;
    
    if (xSemaphoreTake(g_temp.mutex, pdMS_TO_TICKS(10)) == pdTRUE) {
        memcpy(data, &g_temp.latest_data, sizeof(temp_data_t));
        xSemaphoreGive(g_temp.mutex);
        return data->valid ? ESP_OK : ESP_ERR_INVALID_STATE;
    }
    return ESP_ERR_TIMEOUT;
}

bool temp_tmp117_is_healthy(void) {
    if (!g_temp.initialized) return false;
    uint16_t device_id;
    return i2c_read_reg(TMP117_REG_DEVICE_ID, &device_id) == ESP_OK && device_id == TMP117_DEVICE_ID;
}

esp_err_t temp_tmp117_set_limits(float high_c, float low_c) {
    if (!g_temp.initialized) return ESP_ERR_INVALID_STATE;
    
    int16_t high_raw = celsius_to_raw(high_c);
    int16_t low_raw = celsius_to_raw(low_c);
    
    esp_err_t ret = i2c_write_reg(TMP117_REG_TEMP_HIGH_LIMIT, (uint16_t)high_raw);
    if (ret != ESP_OK) return ret;
    
    ret = i2c_write_reg(TMP117_REG_TEMP_LOW_LIMIT, (uint16_t)low_raw);
    return ret;
}

esp_err_t temp_tmp117_soft_reset(void) {
    if (!g_temp.initialized) return ESP_ERR_INVALID_STATE;
    
    uint16_t cfg;
    esp_err_t ret = i2c_read_reg(TMP117_REG_CONFIGURATION, &cfg);
    if (ret != ESP_OK) return ret;
    
    cfg |= TMP117_CFG_SOFT_RESET;
    ret = i2c_write_reg(TMP117_REG_CONFIGURATION, cfg);
    if (ret != ESP_OK) return ret;
    
    vTaskDelay(pdMS_TO_TICKS(10));
    return ESP_OK;
}

esp_err_t temp_tmp117_get_stats(uint32_t *reads, uint32_t *errors) {
    if (reads) *reads = g_temp.read_count;
    if (errors) *errors = g_temp.error_count;
    return ESP_OK;
}