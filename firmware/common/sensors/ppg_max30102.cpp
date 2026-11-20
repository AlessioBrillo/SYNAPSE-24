#include "ppg_max30102.h"
#include "ppg_fifo_batch.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <string.h>

static const char* TAG = "ppg_max30102";

// Init and read errors are returned, never ESP_ERROR_CHECK'd: a missing or loose
// sensor must degrade that sensor, not reboot-loop the band.
#define CHK(x) do { esp_err_t _e = (x); if (_e != ESP_OK) { \
    ESP_LOGE(TAG, "%s failed: %s", #x, esp_err_to_name(_e)); return _e; } } while (0)

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
    bool bus_held;     // this driver owns a reference on the shared I2C bus (synapse_i2c)
    bool fifo_enabled;
    uint32_t skipped_samples;  // FIFO samples lost: overwritten while the poll stalled, or a failed burst read
    uint32_t lost_unreported;  // the part of skipped_samples the scheduler has not turned into a sequence hole yet
} ppg_max30102_ctx_t;

static ppg_max30102_ctx_t s_ctx = {};
static int64_t s_last_ts_us = INT64_MIN;  // newest timestamp handed out: stamps never step back

static esp_err_t max30102_write_reg(uint8_t reg, uint8_t value) {
    const uint8_t buf[2] = {reg, value};
    return synapse_i2c_write(s_ctx.config.i2c_port, (uint8_t)s_ctx.config.i2c_addr, buf, sizeof(buf));
}

static esp_err_t max30102_read_reg(uint8_t reg, uint8_t* value) {
    return synapse_i2c_write_read(s_ctx.config.i2c_port, (uint8_t)s_ctx.config.i2c_addr, &reg, 1, value, 1);
}

// One burst from the FIFO data register: the register does not auto-increment, so `len`
// bytes (a multiple of 6 here) are consecutive samples.
static esp_err_t max30102_read_fifo(uint8_t* data, size_t len) {
    const uint8_t reg = MAX30102_REG_FIFO_DATA;
    return synapse_i2c_write_read(s_ctx.config.i2c_port, (uint8_t)s_ctx.config.i2c_addr, &reg, 1, data, len);
}

// Everything after the bus is up. Split from init so that a failure at any step goes through
// one place that gives the bus reference back.
static esp_err_t ppg_configure(const ppg_max30102_config_t* config) {
    // Codes are validated, not masked: an out-of-range code used to be cut to its low bits and
    // configure a different sensor than the one asked for.
    if (config->sample_rate > 0x07 || config->pulse_width > 0x03 || config->adc_range > 0x03) {
        ESP_LOGE(TAG, "invalid PPG codes: sample_rate=%u (0-7) pulse_width=%u (0-3) adc_range=%u (0-3)",
                 (unsigned)config->sample_rate, (unsigned)config->pulse_width, (unsigned)config->adc_range);
        return ESP_ERR_INVALID_ARG;
    }

    uint8_t part_id;
    CHK(max30102_read_reg(MAX30102_REG_PART_ID, &part_id));
    if (part_id != 0x15) {
        ESP_LOGE(TAG, "Invalid PART_ID: 0x%02X (expected 0x15)", part_id);
        return ESP_ERR_NOT_FOUND;
    }

    CHK(max30102_write_reg(MAX30102_REG_MODE_CONFIG, 0x40));
    vTaskDelay(pdMS_TO_TICKS(100));

    CHK(max30102_write_reg(MAX30102_REG_INTR_ENABLE_1, 0x00));
    CHK(max30102_write_reg(MAX30102_REG_INTR_ENABLE_2, 0x00));

    // No sample averaging (the output rate is exactly SPO2_SR), rollover on so the
    // FIFO always holds the freshest samples, almost-full threshold 15.
    CHK(max30102_write_reg(MAX30102_REG_FIFO_CONFIG, 0x1F));

    // SpO2 mode = RED + IR, 3 bytes each. (Multi-LED mode would also need the
    // SLOT registers, and the MAX30102 has no green LED.)
    CHK(max30102_write_reg(MAX30102_REG_MODE_CONFIG, MAX30102_MODE_SPO2));
    uint8_t spo2_cfg = (uint8_t)(((config->adc_range & 0x03) << 5) |
                                 ((config->sample_rate & 0x07) << 2) |
                                 (config->pulse_width & 0x03));
    CHK(max30102_write_reg(MAX30102_REG_SPO2_CONFIG, spo2_cfg));

    CHK(max30102_write_reg(MAX30102_REG_LED1_PA, config->led_current_red));
    CHK(max30102_write_reg(MAX30102_REG_LED2_PA, config->led_current_ir));

    CHK(max30102_write_reg(MAX30102_REG_FIFO_WR_PTR, 0x00));
    CHK(max30102_write_reg(MAX30102_REG_OVF_COUNTER, 0x00));
    CHK(max30102_write_reg(MAX30102_REG_FIFO_RD_PTR, 0x00));
    s_ctx.skipped_samples = 0;
    s_ctx.lost_unreported = 0;
    s_last_ts_us = INT64_MIN;  // a restarted clock must not be clamped to the previous run's stamps
    s_ctx.fifo_enabled = true;  // acquisition starts with the scheduler; nothing else enables the FIFO

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

esp_err_t ppg_max30102_init(const ppg_max30102_config_t* config) {
    if (!config) return ESP_ERR_INVALID_ARG;

    if (s_ctx.bus_held) {  // re-init without a deinit: do not leak the previous reference
        synapse_i2c_release(s_ctx.config.i2c_port);
        s_ctx.bus_held = false;
    }
    s_ctx.config = *config;
    s_ctx.initialized = false;
    s_ctx.fifo_enabled = false;

    esp_err_t err = synapse_i2c_acquire(config->i2c_port, config->sda_gpio_num, config->scl_gpio_num,
                                        config->i2c_freq_hz);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "I2C bus unavailable: %s", esp_err_to_name(err));
        return err;
    }
    s_ctx.bus_held = true;

    err = ppg_configure(config);
    if (err != ESP_OK) {
        synapse_i2c_release(config->i2c_port);  // a sensor that did not come up must not keep the bus alive
        s_ctx.bus_held = false;
        return err;
    }

    s_ctx.initialized = true;
    ESP_LOGI(TAG, "MAX30102 PPG initialized (I2C port %d, addr 0x%02X)", config->i2c_port, config->i2c_addr);
    return ESP_OK;
}

// One poll can find several samples in the FIFO (the sensor and the poll run on different
// clocks). All of them are forwarded: the burst is read once, kept here, and handed out one
// per read() call; the scheduler keeps calling while ppg_max30102_has_more() says so. The
// timestamps are spaced by the sensor's period with the newest at the poll time
// (ppg_fifo_batch.h) instead of every sample carrying the poll time.
static float s_batch_red[PPG_BATCH_MAX];
static float s_batch_ir[PPG_BATCH_MAX];
static int64_t s_batch_ts[PPG_BATCH_MAX];
static uint8_t s_batch_n = 0;
static uint8_t s_batch_next = 0;

static void ppg_deliver(sensor_sample_t* sample, uint8_t i) {
    sample->data.ppg.red = s_batch_red[i];
    sample->data.ppg.ir = s_batch_ir[i];
    sample->data.ppg.green = 0.0f;
    sample->timestamp_us = s_batch_ts[i];
    sample->lsl_timestamp_us = s_batch_ts[i];

    // Process PPG SQI for motion gate (Architecture.md §74)
    ppg_sqi_result_t sqi_result;
    ppg_sqi_process_sample(s_batch_red[i], s_batch_ir[i], s_batch_ts[i], &sqi_result);
}

bool ppg_max30102_has_more(void* user_ctx) {
    (void)user_ctx;
    return s_batch_next < s_batch_n;
}

esp_err_t ppg_max30102_read(sensor_sample_t* sample, void* user_ctx) {
    (void)user_ctx;

    if (!s_ctx.initialized || !s_ctx.fifo_enabled) return ESP_ERR_INVALID_STATE;

    if (s_batch_next < s_batch_n) {  // the rest of a burst already read
        ppg_deliver(sample, s_batch_next++);
        return ESP_OK;
    }
    s_batch_n = 0;
    s_batch_next = 0;

    uint8_t wr = 0, rd = 0;
    CHK(max30102_read_reg(MAX30102_REG_FIFO_WR_PTR, &wr));
    CHK(max30102_read_reg(MAX30102_REG_FIFO_RD_PTR, &rd));
    uint8_t avail = (uint8_t)((wr - rd) & 0x1F);
    if (avail == 0) {
        // wr == rd means empty OR a FIFO that wrapped completely (rollover is on
        // and the poll stalled >= 32 samples): the overflow counter tells them apart.
        uint8_t ovf = 0;
        CHK(max30102_read_reg(MAX30102_REG_OVF_COUNTER, &ovf));
        if (ovf == 0) return ESP_ERR_NOT_FOUND;  // nothing new since the last poll
        // The counter saturates at 15: at least that many samples were overwritten.
        s_ctx.skipped_samples += ovf;
        s_ctx.lost_unreported += ovf;
        avail = PPG_BATCH_MAX;
    }
    if (avail > PPG_BATCH_MAX) avail = PPG_BATCH_MAX;

    const int64_t poll_us = sample->timestamp_us;
    const uint32_t period_us = ppg_period_us_from_code(s_ctx.config.sample_rate);
    uint8_t fifo_data[6];
    for (uint8_t i = 0; i < avail; i++) {
        esp_err_t rc = max30102_read_fifo(fifo_data, 6);
        if (rc != ESP_OK) {
            // The whole burst is gone: the i samples already taken out of the FIFO sit in the batch
            // arrays, which are never delivered (s_batch_n stays 0), so all `avail` are lost.
            s_ctx.skipped_samples += (uint32_t)avail;
            s_ctx.lost_unreported += (uint32_t)avail;
            return rc;
        }
        uint32_t red = (((uint32_t)fifo_data[0] << 16) | ((uint32_t)fifo_data[1] << 8) | fifo_data[2]) & 0x3FFFF;
        uint32_t ir = (((uint32_t)fifo_data[3] << 16) | ((uint32_t)fifo_data[4] << 8) | fifo_data[5]) & 0x3FFFF;
        s_batch_red[i] = (float)red;
        s_batch_ir[i] = (float)ir;
        s_last_ts_us = ppg_batch_monotonic_us(ppg_batch_timestamp_us(poll_us, avail, i, period_us),
                                              s_last_ts_us);
        s_batch_ts[i] = s_last_ts_us;
    }
    s_batch_n = avail;
    ppg_deliver(sample, s_batch_next++);
    return ESP_OK;
}

uint32_t ppg_max30102_take_lost(void* user_ctx) {
    (void)user_ctx;
    uint32_t lost = s_ctx.lost_unreported;
    s_ctx.lost_unreported = 0;
    return lost;
}

uint32_t ppg_max30102_skipped_samples(void) {
    return s_ctx.skipped_samples;
}

esp_err_t ppg_max30102_get_sqi(ppg_sqi_result_t* result) {
    return ppg_sqi_get_latest(result);
}

esp_err_t ppg_max30102_deinit(void* user_ctx) {
    (void)user_ctx;
    ppg_max30102_disable_fifo(NULL);
    // Only this driver's reference goes: the bus stays up for the IMU, TMP117 and INA219 that
    // still use it (this driver used to delete the whole bus here and took them down with it).
    if (s_ctx.bus_held) {
        synapse_i2c_release(s_ctx.config.i2c_port);
        s_ctx.bus_held = false;
    }
    s_ctx.initialized = false;
    ESP_LOGI(TAG, "MAX30102 PPG deinitialized");
    return ESP_OK;
}

esp_err_t ppg_max30102_enable_fifo(void* user_ctx) {
    (void)user_ctx;
    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;
    CHK(max30102_write_reg(MAX30102_REG_FIFO_WR_PTR, 0x00));
    CHK(max30102_write_reg(MAX30102_REG_OVF_COUNTER, 0x00));
    CHK(max30102_write_reg(MAX30102_REG_FIFO_RD_PTR, 0x00));
    s_ctx.fifo_enabled = true;
    return ESP_OK;
}

esp_err_t ppg_max30102_disable_fifo(void* user_ctx) {
    (void)user_ctx;
    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;
    s_ctx.fifo_enabled = false;
    return ESP_OK;
}