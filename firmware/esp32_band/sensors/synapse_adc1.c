#include "synapse_adc1.h"

static adc_oneshot_unit_handle_t s_adc1;

esp_err_t synapse_adc1_get(adc_oneshot_unit_handle_t *out) {
    if (!out) return ESP_ERR_INVALID_ARG;
    if (!s_adc1) {
        adc_oneshot_unit_init_cfg_t cfg = {
            .unit_id = ADC_UNIT_1,
            .ulp_mode = ADC_ULP_MODE_DISABLE,
        };
        esp_err_t err = adc_oneshot_new_unit(&cfg, &s_adc1);
        if (err != ESP_OK) {
            s_adc1 = NULL;
            return err;
        }
    }
    *out = s_adc1;
    return ESP_OK;
}
