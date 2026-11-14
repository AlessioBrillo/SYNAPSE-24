#include "ecg_ad8232.h"
#include "ecg_contact.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"

static const char* TAG = "ecg_ad8232";

typedef struct {
    ecg_ad8232_config_t config;
    adc_oneshot_unit_handle_t adc_handle;
    bool owns_adc;
    adc_cali_handle_t cali_handle;
    bool initialized;
    bool lead_off_detected;
    ecg_contact_t contact;  // lead-off history: when the bias is measured again
    uint32_t lost_unreported;  // samples not delivered while the bias was measured again (sequence hole)
    float midrail_mv;     // DC bias removed from every sample
    bool midrail_ready;
    int cal_n;            // samples accumulated by the start-up calibration
    double cal_sum_mv;
} ecg_ad8232_ctx_t;

// The AD8232 output sits on a ~1.65 V bias (REFOUT = Vcc/2) with the ECG riding
// on top. Averaging the first second of an ECG (zero-mean at these time scales)
// measures that bias; dividing the raw ADC voltage by the gain without removing
// it gave ~275 mV, which the research stream clamped to full scale.
// 500 samples = 1 s at the 500 Hz ECG rate. The bias is then FROZEN. It used to be tracked
// with a ~10 s time constant, which is a 0.016 Hz high-pass applied on board to a stream
// published as raw: the host could not undo it, and a baseline drift (which is information)
// vanished from every saved recording. Whatever filtering is wanted happens on the host.
// A start-up taken with the electrodes off now needs a restart to recalibrate.
#define ECG_MIDRAIL_CAL_SAMPLES 500

static ecg_ad8232_ctx_t s_ctx = {};

esp_err_t ecg_ad8232_init(const ecg_ad8232_config_t* config) {
    if (!config) return ESP_ERR_INVALID_ARG;

    s_ctx.config = *config;
    s_ctx.initialized = false;
    s_ctx.lead_off_detected = false;
    s_ctx.contact = (ecg_contact_t){0u, 0u};
    s_ctx.lost_unreported = 0u;
    s_ctx.cal_n = 0;
    s_ctx.cal_sum_mv = 0.0;
    s_ctx.midrail_mv = config->midrail_mv;
    s_ctx.midrail_ready = config->midrail_mv > 0.0f;
    s_ctx.adc_handle = NULL;
    s_ctx.cali_handle = NULL;

    esp_err_t err = ESP_OK;
    if (config->adc_handle) {
        // ADC1 is shared with the battery channel: borrow, never delete.
        s_ctx.adc_handle = (adc_oneshot_unit_handle_t)config->adc_handle;
        s_ctx.owns_adc = false;
    } else {
        adc_oneshot_unit_init_cfg_t unit_cfg = {
            .unit_id = ADC_UNIT_1,
            .ulp_mode = ADC_ULP_MODE_DISABLE,
        };
        err = adc_oneshot_new_unit(&unit_cfg, &s_ctx.adc_handle);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "ADC oneshot init failed: %s", esp_err_to_name(err));
            return err;
        }
        s_ctx.owns_adc = true;
    }

    adc_oneshot_chan_cfg_t chan_cfg = {
        .atten = ADC_ATTEN_DB_12,
        .bitwidth = ADC_BITWIDTH_12,
    };
    err = adc_oneshot_config_channel(s_ctx.adc_handle, (adc_channel_t)config->adc_channel, &chan_cfg);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "ADC channel config failed: %s", esp_err_to_name(err));
        if (s_ctx.owns_adc) {
            adc_oneshot_del_unit(s_ctx.adc_handle);
        }
        s_ctx.adc_handle = NULL;
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
            // Pull-DOWN: LO+ is driven high by the AD8232 when an electrode is off. With a pull-up an
            // unwired pin would read "lead off" forever; with a pull-down it reads "contact", so a
            // missing wire flags nothing (and detects nothing).
            .pull_up_en = GPIO_PULLUP_DISABLE,
            .pull_down_en = GPIO_PULLDOWN_ENABLE,
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
    if (s_ctx.config.gpio_drdy >= 0) {
        s_ctx.lead_off_detected = (gpio_get_level((gpio_num_t)s_ctx.config.gpio_drdy) == 1);
    }

    // Lead-off is reported, not used to gate the samples: gating on it would silence the stream
    // whenever the pin misreports. It only decides when the frozen bias is measured again: after
    // a real lead-off, once the electrodes are on and steady (an explicit midrail_mv is trusted).
    if (s_ctx.config.midrail_mv <= 0.0f && s_ctx.midrail_ready &&
        ecg_contact_update(&s_ctx.contact, s_ctx.lead_off_detected)) {
        s_ctx.midrail_ready = false;
        s_ctx.cal_n = 0;
        s_ctx.cal_sum_mv = 0.0;
        ESP_LOGI(TAG, "AD8232 electrodes applied after a lead-off: measuring the bias again");
    }
    if (!s_ctx.midrail_ready) {
        s_ctx.cal_sum_mv += (double)voltage_mv;
        if (++s_ctx.cal_n < ECG_MIDRAIL_CAL_SAMPLES) {
            // Calibrating: no sample yet. The scheduler numbers only delivered samples, so these
            // become a hole in the sequence (take_lost) instead of a silent shift of the timeline.
            s_ctx.lost_unreported++;
            return ESP_ERR_NOT_FOUND;
        }
        s_ctx.midrail_mv = (float)(s_ctx.cal_sum_mv / (double)s_ctx.cal_n);
        s_ctx.midrail_ready = true;
        ESP_LOGI(TAG, "AD8232 mid-rail calibrated: %.1f mV", s_ctx.midrail_mv);
    }

    float gain = s_ctx.config.gain > 0.0f ? s_ctx.config.gain : 1.0f;
    sample->data.ecg.voltage_mv = ((float)voltage_mv - s_ctx.midrail_mv) / gain;
    return ESP_OK;
}

esp_err_t ecg_ad8232_deinit(void* user_ctx) {
    (void)user_ctx;
    if (s_ctx.cali_handle) {
        adc_cali_delete_scheme_curve_fitting(s_ctx.cali_handle);
        s_ctx.cali_handle = NULL;
    }
    if (s_ctx.adc_handle && s_ctx.owns_adc) {
        adc_oneshot_del_unit(s_ctx.adc_handle);
    }
    s_ctx.adc_handle = NULL;
    s_ctx.initialized = false;
    ESP_LOGI(TAG, "AD8232 ECG deinitialized");
    return ESP_OK;
}

uint32_t ecg_ad8232_take_lost(void* user_ctx) {
    (void)user_ctx;
    uint32_t lost = s_ctx.lost_unreported;
    s_ctx.lost_unreported = 0u;
    return lost;
}

bool ecg_ad8232_is_lead_off(void* user_ctx) {
    (void)user_ctx;
    return s_ctx.lead_off_detected;
}
