/**
 * @file synapse_adc1.h
 * @brief Single owner of the ADC1 oneshot unit.
 *
 * ESP-IDF allows one oneshot handle per ADC unit: the ECG channel and the
 * battery channel both live on ADC1, so both borrow this handle.
 */
#ifndef SYNAPSE_ADC1_H
#define SYNAPSE_ADC1_H

#include "esp_err.h"
#include "esp_adc/adc_oneshot.h"

#ifdef __cplusplus
extern "C" {
#endif

/** Create the ADC1 unit on first call; later calls return the same handle. */
esp_err_t synapse_adc1_get(adc_oneshot_unit_handle_t *out);

#ifdef __cplusplus
}
#endif

#endif
