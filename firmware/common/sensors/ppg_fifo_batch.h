/**
 * @file ppg_fifo_batch.h
 * @brief Timing of a MAX30102 FIFO burst. Pure C, no ESP-IDF.
 *
 * The sensor samples on its own oscillator; the poll runs on another. When the poll is late the
 * FIFO holds several samples. They used to be read and all but the newest thrown away, so the
 * stream lost samples and the survivors carried the poll time (jitter up to a whole period).
 * Now every sample is forwarded, and the burst gets timestamps spaced by the sensor's period,
 * the newest one at the poll time. Compiled and run on the host
 * (tests/firmware_host/host_glue_ppg_batch.c).
 */
#ifndef PPG_FIFO_BATCH_H
#define PPG_FIFO_BATCH_H

#include <stdint.h>

/** The FIFO holds 32 samples. */
#define PPG_BATCH_MAX 32u

/** MAX30102 SPO2_SR code (SPO2_CONFIG bits 4:2) -> microseconds between samples; 0 for a bad code. */
static inline uint32_t ppg_period_us_from_code(uint8_t code)
{
    static const uint16_t hz[8] = {50u, 100u, 200u, 400u, 800u, 1000u, 1600u, 3200u};
    return code < 8u ? (uint32_t)(1000000u / hz[code]) : 0u;
}

/**
 * Timestamp of sample @p index (0 = oldest) in a burst of @p count read at @p poll_us: the newest
 * sample is the poll time, each older one a sensor period earlier.
 */
static inline int64_t ppg_batch_timestamp_us(int64_t poll_us, uint32_t count, uint32_t index,
                                             uint32_t period_us)
{
    if (count == 0u || index >= count) {
        return poll_us;
    }
    return poll_us - (int64_t)(count - 1u - index) * (int64_t)period_us;
}

#endif /* PPG_FIFO_BATCH_H */
