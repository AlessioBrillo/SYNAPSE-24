#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"
#include "sensor_scheduler.h"

#ifdef __cplusplus
extern "C" {
#endif

// AD8232 ECG configuration
typedef struct {
    int adc_channel;      // ADC1 channel (ADC1_CH0 = GPIO1 on the S3)
    int gpio_lead_off;    // GPIO for lead-off sense, the breakout LO+ (-1 if not used)
    float gain;           // total front-end gain, electrode to ADC pin (nominal 1100 on the common breakout)
    void* adc_handle;     // Optional shared adc_oneshot_unit_handle_t (not owned); NULL = create own
    float midrail_mv;     // AD8232 output bias (REFOUT, ~Vcc/2) in mV; <= 0 = measure it at start-up
} ecg_ad8232_config_t;

esp_err_t ecg_ad8232_init(const ecg_ad8232_config_t* config);
esp_err_t ecg_ad8232_read(sensor_sample_t* sample, void* user_ctx);
esp_err_t ecg_ad8232_deinit(void* user_ctx);
bool ecg_ad8232_is_lead_off(void* user_ctx);
/** The input has been a rail or a constant for ~2 s: the front end is dead (broken wire, no power),
 *  which the lead-off pin cannot tell. Reported only; the stream is not gated on it (ecg_stuck.h). */
bool ecg_ad8232_is_signal_stuck(void* user_ctx);
/** Samples not delivered since the last call because the bias was being measured (the call resets
 *  it); the scheduler turns them into a sequence hole. */
uint32_t ecg_ad8232_take_lost(void* user_ctx);

#ifdef __cplusplus
}
#endif