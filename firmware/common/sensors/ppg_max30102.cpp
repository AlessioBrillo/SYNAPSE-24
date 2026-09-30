#include "ppg_max30102.h"
#include "esp_log.h"
#include "driver/i2c.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char* TAG = "ppg_max30102";

#define MAX30102_REG_INTR_STATUS_1  0x00
#define MAX30102_REG_INTR_STATUS_2  0x01
#define MAX30102_REG_INTR_ENABLE_1  0x02
#define MAX30102_REG_INTR_ENABLE_2  0x03
#define MAX30102_REG_FIFO_WR_PTR    0x04
#define MAX30102_REG_OVF_COUNTER    0x05
#define MAX30102_REG_FIFO_RD_PTR    0x06
#define MAX30102_REG_FIFO_DATA      0x07
#define MAX30102_REG_FIFO_CONFIG    0x08
#define MAX30102_REG_MODE_CONFIG    0x09
#define MAX30102_REG_SPO2_CONFIG    0x0A
#define MAX30102_REG_LED1_PA        0x0C
#define MAX30102_REG_LED2_PA        0x0D
#define MAX30102_REG_LED3_PA        0x0E
#define MAX30102_REG_PILOT_PA       0x0F
#define MAX30102_REG_PART_ID        0xFF

#define MAX30102_MODE_HR_ONLY       0x02
#define MAX30102_MODE_SPO2          0x03
#define MAX30102_MODE_MULTI_LED     0x07

typedef struct {
    ppg_max30102_config_t config;
    bool initialized;
    bool fifo_enabled;
} ppg_max30102_ctx_t;

static ppg_max30102_ctx_t s_ctx = {0};

static esp_err_t max30102_write_reg(uint8_t reg, uint8_t value) {
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (s_ctx.config.i2c_addr << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, reg, true);
    i2c_master_write_byte(cmd, value, true);
    i2c_master_stop(cmd);
    esp_err_t ret = i2c_master_cmd_begin(s_ctx.config.i2c_port, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
    return ret;
}

static esp_err_t max30102_read_reg(uint8_t reg, uint8_t* value) {
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (s_ctx.config.i2c_addr << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, reg, true);
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (s_ctx.config.i2c_addr << 1) | I2C_MASTER_READ, true);
    i2c_master_read_byte(cmd, value, I2C_MASTER_NACK);
    i2c_master_stop(cmd);
    esp_err_t ret = i2c_master_cmd_begin(s_ctx.config.i2c_port, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
    return ret;
}

static esp_err_t max30102_read_fifo(uint8_t* data, size_t len) {
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (s_ctx.config.i2c_addr << 1) | I2C_MASTER_WRITE, true);
    i2c_master_write_byte(cmd, MAX30102_REG_FIFO_DATA, true);
    i2c_master_start(cmd);
    i2c_master_write_byte(cmd, (s_ctx.config.i2c_addr << 1) | I2C_MASTER_READ, true);
    for (size_t i = 0; i < len - 1; i++) {
        i2c_master_read_byte(cmd, &data[i], I2C_MASTER_ACK);
    }
    i2c_master_read_byte(cmd, &data[len - 1], I2C_MASTER_NACK);
    i2c_master_stop(cmd);
    esp_err_t ret = i2c_master_cmd_begin(s_ctx.config.i2c_port, cmd, pdMS_TO_TICKS(100));
    i2c_cmd_link_delete(cmd);
    return ret;
}

esp_err_t ppg_max30102_init(const ppg_max30102_config_t* config) {
    if (!config) return ESP_ERR_INVALID_ARG;

    s_ctx.config = *config;
    s_ctx.initialized = false;
    s_ctx.fifo_enabled = false;

    i2c_config_t i2c_conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = GPIO_NUM_21,
        .scl_io_num = GPIO_NUM_22,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = 400000
    };
    esp_err_t err = i2c_param_config(config->i2c_port, &i2c_conf);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C param config failed: %s", esp_err_to_name(err));
        return err;
    }
    err = i2c_driver_install(config->i2c_port, i2c_conf.mode, 0, 0, 0);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "I2C driver install failed: %s", esp_err_to_name(err));
        return err;
    }

    uint8_t part_id;
    ESP_ERROR_CHECK(max30102_read_reg(MAX30102_REG_PART_ID, &part_id));
    if (part_id != 0x15) {
        ESP_LOGE(TAG, "Invalid PART_ID: 0x%02X (expected 0x15)", part_id);
        return ESP_ERR_NOT_FOUND;
    }

    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_MODE_CONFIG, 0x40));
    vTaskDelay(pdMS_TO_TICKS(100));

    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_INTR_ENABLE_1, 0x00));
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_INTR_ENABLE_2, 0x00));

    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_FIFO_CONFIG, 0x4F));

    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_MODE_CONFIG, MAX30102_MODE_MULTI_LED));
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_SPO2_CONFIG, 0x27));

    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_LED1_PA, config->led_current_red));
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_LED2_PA, config->led_current_ir));
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_LED3_PA, config->led_current_green));

    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_FIFO_WR_PTR, 0x00));
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_OVF_COUNTER, 0x00));
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_FIFO_RD_PTR, 0x00));

    if (config->gpio_int >= 0) {
        gpio_config_t io_conf = {
            .pin_bit_mask = (1ULL << config->gpio_int),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .intr_type = GPIO_INTR_NEGEDGE
        };
        gpio_config(&io_conf);
    }

    s_ctx.initialized = true;
    ESP_LOGI(TAG, "MAX30102 PPG initialized (I2C port %d, addr 0x%02X)", config->i2c_port, config->i2c_addr);
    return ESP_OK;
}

esp_err_t ppg_max30102_read(sensor_sample_t* sample, void* user_ctx) {
    (void)user_ctx;

    if (!s_ctx.initialized || !s_ctx.fifo_enabled) return ESP_ERR_INVALID_STATE;

    uint8_t fifo_data[6];
    esp_err_t ret = max30102_read_fifo(fifo_data, 6);
    if (ret != ESP_OK) return ret;

    uint32_t red = ((uint32_t)fifo_data[0] << 16) | ((uint32_t)fifo_data[1] << 8) | fifo_data[2];
    uint32_t ir = ((uint32_t)fifo_data[3] << 16) | ((uint32_t)fifo_data[4] << 8) | fifo_data[5];

    red &= 0x3FFFF;
    ir &= 0x3FFFF;

    sample->data.ppg.red = (float)red;
    sample->data.ppg.ir = (float)ir;
    sample->data.ppg.green = 0.0f;

    // Process PPG SQI for motion gate (Architecture.md §74)
    ppg_sqi_result_t sqi_result;
    ppg_sqi_process_sample((float)red, (float)ir, sample->timestamp_us, &sqi_result);

    return ESP_OK;
}

esp_err_t ppg_max30102_get_sqi(ppg_sqi_result_t* result) {
    return ppg_sqi_get_latest(result);
}

esp_err_t ppg_max30102_deinit(void* user_ctx) {
    (void)user_ctx;
    ppg_max30102_disable_fifo(NULL);
    i2c_driver_delete(s_ctx.config.i2c_port);
    s_ctx.initialized = false;
    ESP_LOGI(TAG, "MAX30102 PPG deinitialized");
    return ESP_OK;
}

esp_err_t ppg_max30102_enable_fifo(void* user_ctx) {
    (void)user_ctx;
    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_FIFO_WR_PTR, 0x00));
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_OVF_COUNTER, 0x00));
    ESP_ERROR_CHECK(max30102_write_reg(MAX30102_REG_FIFO_RD_PTR, 0x00));
    s_ctx.fifo_enabled = true;
    return ESP_OK;
}

esp_err_t ppg_max30102_disable_fifo(void* user_ctx) {
    (void)user_ctx;
    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;
    s_ctx.fifo_enabled = false;
    return ESP_OK;
}