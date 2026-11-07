/**
 * @file synapse_window.h
 * @brief "Is this a new quality window?" Pure, no ESP-IDF; executed by the host tests.
 *
 * The quality module publishes a new reading once per window (PPG 10 Hz, IMU 1 Hz), while the
 * FSM is ticked every 10 ms. Counting ticks instead of windows made "5 minutes of stillness"
 * pass in 100 ms: a window is counted once, when its timestamp changes.
 */
#ifndef SYNAPSE_WINDOW_H
#define SYNAPSE_WINDOW_H

#include <stdint.h>

/** A reading older than this is not a window: the finger left or the processor stalled. */
#define SYNAPSE_WINDOW_MAX_AGE_US 5000000LL

/** 1 once per distinct, real @p stamp_us; remembers it in @p last_us. */
static inline int synapse_window_is_new(int64_t *last_us, int64_t stamp_us)
{
    if (stamp_us <= 0 || stamp_us == *last_us) {
        return 0;
    }
    *last_us = stamp_us;
    return 1;
}

/** 1 when a reading stamped @p stamp_us is still current at @p now_us. */
static inline int synapse_window_is_current(int64_t now_us, int64_t stamp_us)
{
    return stamp_us > 0 && now_us >= stamp_us && (now_us - stamp_us) <= SYNAPSE_WINDOW_MAX_AGE_US;
}

#endif /* SYNAPSE_WINDOW_H */
