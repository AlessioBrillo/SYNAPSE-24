/**
 * @file synapse_feature_map.h
 * @brief How a measurement becomes a consumer-frame field. Pure C, no ESP-IDF.
 *
 * Kept apart from app_main.c so the host gate compiles and runs these rules
 * (tests/firmware_host/host_glue_feature_map.c): what the band reports as a
 * value, and what it reports as "not available", decides what a person sees.
 *
 * The rule behind every function: a number that was not measured is the
 * sentinel, never 0. A fabricated 0 reads as a perfect measurement, or, for
 * the heart rate, as a dead patient.
 */
#ifndef SYNAPSE_FEATURE_MAP_H
#define SYNAPSE_FEATURE_MAP_H

#include <stdint.h>

#define SYNAPSE_NA_U8  0xFFu
#define SYNAPSE_NA_U16 0xFFFFu

/** Plausible human heart rate; outside it the reading is a detector failure, not a value. */
#define SYNAPSE_HR_MIN_BPM 20.0f
#define SYNAPSE_HR_MAX_BPM 300.0f

/** A PPG quality record older than this describes a finger that may be long gone. */
#define SYNAPSE_PPG_MAX_AGE_US 5000000LL

/**
 * 0.0..1.0 -> 0..254. 255 is the "not available" sentinel of these fields, so a perfect
 * signal must not reach it. NaN and negative inputs are not a measurement: they are the
 * sentinel (they used to become 0, "a perfectly bad signal", a value nobody measured).
 */
static inline uint8_t synapse_unit_to_u8(float v)
{
    if (!(v >= 0.0f)) {
        return SYNAPSE_NA_U8; /* NaN and negatives */
    }
    if (v >= 1.0f) {
        return 254u;
    }
    return (uint8_t)(v * 254.0f + 0.5f);
}

/** bpm -> u16, rounded (it used to be truncated: 71.9 read as 71); outside the plausible range -> NA. */
static inline uint16_t synapse_hr_to_u16(float bpm)
{
    if (!(bpm >= SYNAPSE_HR_MIN_BPM && bpm <= SYNAPSE_HR_MAX_BPM)) {
        return SYNAPSE_NA_U16; /* includes NaN */
    }
    return (uint16_t)(bpm + 0.5f);
}

/** 1 when @p stamp_us is a real timestamp no older than @p max_age_us at @p now_us. */
static inline int synapse_is_fresh(int64_t now_us, int64_t stamp_us, int64_t max_age_us)
{
    return stamp_us > 0 && now_us >= stamp_us && (now_us - stamp_us) <= max_age_us;
}

/** RR and HRV are published only from a reading this good (CONFIG_SYNAPSE_RR_ENABLE). */
#define SYNAPSE_RR_MIN_SQI 0.5f
#define SYNAPSE_RR_MAX_MOTION 0.3f

/**
 * 1 when the PPG reading is fresh, its quality index is at least SYNAPSE_RR_MIN_SQI and its
 * motion-artifact probability at most SYNAPSE_RR_MAX_MOTION. A NaN on either side fails.
 */
static inline int synapse_rr_publishable(int fresh, float sqi, float motion_artifact_prob)
{
    return fresh && sqi >= SYNAPSE_RR_MIN_SQI && motion_artifact_prob <= SYNAPSE_RR_MAX_MOTION;
}

#endif /* SYNAPSE_FEATURE_MAP_H */
