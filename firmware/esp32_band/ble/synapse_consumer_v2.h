/**
 * @file synapse_consumer_v2.h
 * @brief Consumer contract v2: feature frame, status, stream metadata, control point.
 *
 * Pure C, no ESP-IDF or NimBLE dependency: the same translation unit is compiled
 * on the host and checked byte for byte against the SDK codec
 * (tests/test_consumer_v2_parity.py). Every offset, size and sentinel comes from
 * synapse_contract_generated.h, i.e. from config/hardware.yaml: this file holds
 * the logic, not a second copy of the layout.
 */
#ifndef SYNAPSE_CONSUMER_V2_H
#define SYNAPSE_CONSUMER_V2_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "synapse_contract_generated.h"

#ifdef __cplusplus
extern "C" {
#endif

/* `flags`: ONE definition for the feature frame and the status record. The bits come from
 * config/hardware.yaml (ble.consumer_v2.flags); `charging` is meaningful only when the POWER
 * bit of `sensor_health` is set. */
#define SYNAPSE_V2_FLAG_CHARGING      SYNAPSE_CONTRACT_V2_FLAG_CHARGING
#define SYNAPSE_V2_FLAG_LEAD_OFF      SYNAPSE_CONTRACT_V2_FLAG_LEAD_OFF
#define SYNAPSE_V2_FLAG_TRANSMITTING  SYNAPSE_CONTRACT_V2_FLAG_TRANSMITTING
#define SYNAPSE_V2_FLAG_CLOCK_WRAPPED SYNAPSE_CONTRACT_V2_FLAG_CLOCK_WRAPPED

/* `sensor_health` bits (set = the sensor initialised) */
#define SYNAPSE_V2_HEALTH_ECG   SYNAPSE_CONTRACT_V2_HEALTH_ECG
#define SYNAPSE_V2_HEALTH_PPG   SYNAPSE_CONTRACT_V2_HEALTH_PPG
#define SYNAPSE_V2_HEALTH_IMU   SYNAPSE_CONTRACT_V2_HEALTH_IMU
#define SYNAPSE_V2_HEALTH_TEMP  SYNAPSE_CONTRACT_V2_HEALTH_TEMP
#define SYNAPSE_V2_HEALTH_POWER SYNAPSE_CONTRACT_V2_HEALTH_POWER

/** Fields hold the value to send; a field equal to its NA sentinel is "not available". */
typedef struct {
    uint8_t flags;
    uint16_t sequence;
    uint32_t t_dev_ms;
    uint16_t heart_rate_bpm;
    uint16_t rr_last;          /**< 1/1024 s */
    uint8_t rr_count;
    uint8_t ppg_sqi;
    uint8_t ecg_quality;
    uint8_t motion_level;
    uint8_t stress_level;
    uint8_t battery_pct;
    int16_t temperature_c100;
    uint16_t rmssd_ms;
    uint16_t sdnn_ms;
    uint8_t sport_type;
    uint8_t sensor_health;
    uint8_t motion_artifact;   /**< 0..254 = 0.0..1.0 (v1 ppg_map), 255 = not available */
} synapse_feature_v2_t;

typedef struct {
    uint8_t tier;
    uint8_t sensor_health;
    uint8_t flags;
    uint16_t mtu;
    uint32_t uptime_s;
    uint16_t epoch_wraps;
    uint32_t ring_overflow;
    uint32_t stale_samples;
    uint32_t dropped_frames;
    uint32_t read_errors;
    uint32_t fw_build;
    uint32_t ppg_skipped;      /**< PPG FIFO samples not read in time */
    uint32_t power_dropped;    /**< POWER telemetry samples dropped */
} synapse_status_v2_t;

/** Start a feature record with every optional field set to its NA sentinel. */
void synapse_feature_v2_init_na(synapse_feature_v2_t *f);

/** Serialise; returns the frame length, or 0 when @p cap is too small. */
size_t synapse_feature_v2_encode(const synapse_feature_v2_t *f, uint8_t *out, size_t cap);

/** Serialise the status record; returns its length, or 0 when @p cap is too small. */
size_t synapse_status_v2_encode(const synapse_status_v2_t *s, uint8_t *out, size_t cap);

/** [count u8] + count * entry, from the generated stream table; 0 when @p cap is too small. */
size_t synapse_stream_metadata_encode(uint8_t *out, size_t cap);

/** Actions a control-point write can trigger; the callbacks run in the caller's context. */
typedef struct {
    void (*set_transmitting)(bool on, void *ctx);
    void (*set_feature_rate)(uint8_t hz, void *ctx);
    void (*set_time)(uint64_t epoch_ms, void *ctx);   /**< the phone gives the band its wall clock */
    void *ctx;
} synapse_control_ops_t;

/** Apply one control-point write. Returns 0, or a SYNAPSE_CONTRACT_V2_ERR_* code. */
int synapse_control_apply(const uint8_t *data, size_t len, const synapse_control_ops_t *ops);

#ifdef __cplusplus
}
#endif

#endif /* SYNAPSE_CONSUMER_V2_H */
