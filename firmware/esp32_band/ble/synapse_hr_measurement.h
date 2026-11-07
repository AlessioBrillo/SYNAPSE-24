/**
 * @file synapse_hr_measurement.h
 * @brief Pure rules of the standard Heart Rate Measurement (0x2A37) value. No ESP-IDF.
 *
 * Compiled by the host tests (tests/test_firmware_s2_data.py).
 */
#ifndef SYNAPSE_HR_MEASUREMENT_H
#define SYNAPSE_HR_MEASUREMENT_H

#include <stdint.h>

/** How long a measurement stays readable after the notify path last built it. */
#define SYNAPSE_HR_READ_MAX_AGE_US 3000000LL

/**
 * Flags byte: bit0 = 16-bit value, bit4 = RR present, bits 1-2 = sensor contact, where 0b11 is
 * "feature supported, contact detected". The band builds the value only from a fresh PPG
 * reading, so contact is what the value itself says.
 */
static inline uint8_t synapse_hr_flags(int rr_present)
{
    return (uint8_t)(0x01u | 0x06u | (rr_present ? 0x10u : 0x00u));
}

/** 1 when a measurement stamped @p last_us is still fresh at @p now_us (0 = never stamped). */
static inline int synapse_hr_is_readable(int64_t now_us, int64_t last_us)
{
    return last_us > 0 && now_us >= last_us && (now_us - last_us) <= SYNAPSE_HR_READ_MAX_AGE_US;
}

#endif /* SYNAPSE_HR_MEASUREMENT_H */
