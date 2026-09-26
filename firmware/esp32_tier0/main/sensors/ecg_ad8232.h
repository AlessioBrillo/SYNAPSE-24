#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

// AD8232 ECG configuration
typedef struct {
    int adc_channel;      // ADC1 channel (e.g., ADC1_CHANNEL_0 = GPIO36)
    int gpio_drdy;        // GPIO for DRDY/lead-off detection (-1 if not used)
    float vref_mv;        // ADC reference voltage in mV (default 1100 for ESP32)
    float gain;           // AD8232 gain (default 6.0 for standard config)
} ecg_ad8232_config_t;

esp_err_t ecg_ad8232_init(const ecg_ad8232_config_t* config);
esp_err_t ecg_ad8232_read(sensor_sample_t* sample, void* user_ctx);
esp_err_t ecg_ad8232_deinit(void* user_ctx);
bool ecg_ad8232_is_lead_off(void* user_ctx);

#ifdef __cplusplus
}
#endif