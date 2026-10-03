#include "ecg_ad8232.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

static const char* TAG = "ecg_ad8232";

typedef struct {
    ecg_ad8232_config_t config;
    adc_oneshot_unit_handle_t adc_handle;
    adc_cali_handle_t cali_handle;
    bool initialized;
    bool lead_off_detected;
} ecg_ad8232_ctx_t;

static ecg_ad8232_ctx_t s_ctx = {};

esp_err_t ecg_ad8232_init(const ecg_ad8232_config_t* config) {
    if (!config) return ESP_ERR_INVALID_ARG;

    s_ctx.config = *config;
    s_ctx.initialized = false;
    s_ctx.lead_off_detected = false;
    s_ctx.adc_handle = NULL;
    s_ctx.cali_handle = NULL;

    adc_oneshot_unit_init_cfg_t unit_cfg = {
        .unit_id = ADC_UNIT_1,
        .ulp_mode = ADC_ULP_MODE_DISABLE,
    };
    esp_err_t err = adc_oneshot_new_unit(&unit_cfg, &s_ctx.adc_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADC oneshot init failed: %s", esp_err_to_name(err));
        return err;
    }

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    err = adc_oneshot_config_channel(s_ctx.adc_handle, (adc_channel_t)config->adc_channel, &chan_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADC channel config failed: %s", esp_err_to_name(err));
        return err;
    }

    adc_cali_curve_fitting_config_t cali_cfg = {
        .unit_id = ADC_UNIT_1,
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    if (adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_ctx.cali_handle) != ESP_OK) {
        ESP_LOGW(TAG, "ADC calibration unavailable, using raw scaling");
        s_ctx.cali_handle = NULL;
    }

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
    ESP_LOGI(TAG, "AD8232 ECG initialized (ADC1 CH%d, gain=%.1f)", config->adc_channel, config->gain);
    return ESP_OK;
}

esp_err_t ecg_ad8232_read(sensor_sample_t* sample, void* user_ctx) {
    (void)user_ctx;

    if (!s_ctx.initialized || !s_ctx.adc_handle) return ESP_ERR_INVALID_STATE;

    int raw = 0;
    esp_err_t err = adc_oneshot_read(s_ctx.adc_handle, (adc_channel_t)s_ctx.config.adc_channel, &raw);
    if (err != ESP_OK) return err;

    int voltage_mv = 0;
    if (s_ctx.cali_handle) {
        if (adc_cali_raw_to_voltage(s_ctx.cali_handle, raw, &voltage_mv) != ESP_OK) {
            voltage_mv = (raw * 3300) / 4095;
        }
    } else {
        voltage_mv = (raw * 3300) / 4095;
    }
    float ecg_mv = (float)voltage_mv / s_ctx.config.gain;

    if (s_ctx.config.gpio_drdy >= 0) {
        s_ctx.lead_off_detected = (gpio_get_level((gpio_num_t)s_ctx.config.gpio_drdy) == 1);
    }

    sample->data.ecg.voltage_mv = ecg_mv;
    return ESP_OK;
}

esp_err_t ecg_ad8232_deinit(void* user_ctx) {
    (void)user_ctx;
    if (s_ctx.cali_handle) {
        adc_cali_delete_scheme_curve_fitting(s_ctx.cali_handle);
        s_ctx.cali_handle = NULL;
    }
    if (s_ctx.adc_handle) {
        adc_oneshot_del_unit(s_ctx.adc_handle);
        s_ctx.adc_handle = NULL;
    }
    s_ctx.initialized = false;
    ESP_LOGI(TAG, "AD8232 ECG deinitialized");
    return ESP_OK;
}

bool ecg_ad8232_is_lead_off(void* user_ctx) {
    (void)user_ctx;
    return s_ctx.lead_off_detected;
}
