/**
 * @file synapse_calibration.h
 * @brief Per-unit calibration record, written by the factory station.
 *
 * Kconfig is the default for an uncalibrated band. This record is the measured
 * truth for one serial: it lives in NVS, is versioned and CRC-checked, and is
 * read by the sensor layer before any driver is configured. A corrupt or old
 * record falls back to the defaults instead of silently scaling the ECG by an
 * unknown gain.
 */

#ifndef SYNAPSE_CALIBRATION_H
#define SYNAPSE_CALIBRATION_H

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define SYNAPSE_CALIBRATION_VERSION 2u
#define SYNAPSE_CAL_SERIAL_MAX 20u
#define SYNAPSE_CAL_DATE_MAX 16u
#define SYNAPSE_CAL_OPERATOR_MAX 16u
#define SYNAPSE_CAL_INSTRUMENT_MAX 24u
#define SYNAPSE_CAL_FW_MAX 16u

typedef struct {
    uint32_t version;
    uint32_t crc32;             /* over the struct with this field zeroed */
    uint32_t ecg_gain_x100;     /* 0 = use the Kconfig default */
    int32_t ecg_midrail_mv;     /* metadata only; 0 = the driver measures it */
    uint8_t ppg_led_red;        /* 0 = use the Kconfig default */
    uint8_t ppg_led_ir;         /* 0 = use the Kconfig default */
    int16_t accel_bias_mg[3];   /* subtracted after scaling, in mg */
    int16_t gyro_bias_mdps[3];  /* subtracted after scaling, in mdps */
    char serial[SYNAPSE_CAL_SERIAL_MAX];
    char date[SYNAPSE_CAL_DATE_MAX];
    char operator[SYNAPSE_CAL_OPERATOR_MAX];
    char instrument[SYNAPSE_CAL_INSTRUMENT_MAX];  /* serial of the generator/meter */
    char fw_version[SYNAPSE_CAL_FW_MAX];          /* firmware the measurement was made with */
    uint16_t gain_uncertainty_pct_x100;          /* 500 = +/-5.00 % */
    int16_t noise_std_uv_x10;                    /* shorted-input ECG noise, 0 = not measured */
} synapse_calibration_t;

/** Fill @p cal with the "no override" record. */
void synapse_calibration_defaults(synapse_calibration_t *cal);

/** Recompute the CRC after editing the record. */
void synapse_calibration_seal(synapse_calibration_t *cal);

/** True when the CRC and version both match. */
bool synapse_calibration_valid(const synapse_calibration_t *cal);

/** Load from NVS; on any problem return the defaults and a non-OK status. */
esp_err_t synapse_calibration_load(synapse_calibration_t *out);

/** Validate and persist a sealed record. */
esp_err_t synapse_calibration_save(const synapse_calibration_t *cal);

/** Remove the record (returns the band to Kconfig defaults). */
esp_err_t synapse_calibration_erase(void);

/** Where the last load came from: "defaults", "nvs" or "invalid". */
const char *synapse_calibration_state(void);

#ifdef __cplusplus
}
#endif

#endif /* SYNAPSE_CALIBRATION_H */
