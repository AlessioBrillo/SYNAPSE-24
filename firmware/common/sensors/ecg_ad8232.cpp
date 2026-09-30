#include "ecg_ad8232.h"
#include "esp_log.h"
#include "driver/adc.h"
#include "driver/gpio.h"
#include "esp_adc_cal.h"

static const char* TAG = "ecg_ad8232";

typedef struct {
    ecg_ad8232_config_t config;
    esp_adc_cal_characteristics_t adc_chars;
    bool initialized;
    bool lead_off_detected;
} ecg_ad8232_ctx_t;

static ecg_ad8232_ctx_t s_ctx = {0};

esp_err_t ecg_ad8232_init(const ecg_ad8232_config_t* config) {
    if (!config) return ESP_ERR_INVALID_ARG;

    s_ctx.config = *config;
    s_ctx.initialized = false;
    s_ctx.lead_off_detected = false;

    adc1_config_width(ADC_WIDTH_BIT_12);
    adc1_config_channel_atten(config->adc_channel, ADC_ATTEN_DB_11);

    esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_11, ADC_WIDTH_BIT_12, config->vref_mv, &s_ctx.adc_chars);

    if (config->gpio_drdy >= 0) {
        gpio_config_t io_conf = {
            .pin_bit_mask = (1ULL << config->gpio_drdy),
            .mode = GPIO_MODE_INPUT,
            .pull_up_en = GPIO_PULLUP_ENABLE,
            .pull_down_en = GPIO_PULLDOWN_DISABLE,
            .intr_type = GPIO_INTR_DISABLE
        };
        gpio_config(&io_conf);
    }

    s_ctx.initialized = true;
    ESP_LOGI(TAG, "AD8232 ECG initialized (ADC channel %d, Vref=%.1f mV, gain=%.1f)", config->adc_channel, config->vref_mv, config->gain);
    return ESP_OK;
}

esp_err_t ecg_ad8232_read(sensor_sample_t* sample, void* user_ctx) {
    (void)user_ctx;

    if (!s_ctx.initialized) return ESP_ERR_INVALID_STATE;

    int raw = adc1_get_raw(s_ctx.config.adc_channel);
    uint32_t voltage_mv = esp_adc_cal_raw_to_voltage(raw, &s_ctx.adc_chars);
    float ecg_mv = (float)voltage_mv / s_ctx.config.gain;

    if (s_ctx.config.gpio_drdy >= 0) {
        s_ctx.lead_off_detected = (gpio_get_level(s_ctx.config.gpio_drdy) == 1);
    }

    sample->data.ecg.voltage_mv = ecg_mv;
    return ESP_OK;
}

esp_err_t ecg_ad8232_deinit(void* user_ctx) {
    (void)user_ctx;
    s_ctx.initialized = false;
    ESP_LOGI(TAG, "AD8232 ECG deinitialized");
    return ESP_OK;
}

bool ecg_ad8232_is_lead_off(void* user_ctx) {
    (void)user_ctx;
    return s_ctx.lead_off_detected;
}