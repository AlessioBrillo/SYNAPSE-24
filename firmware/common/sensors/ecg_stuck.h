/**
 * @file ecg_stuck.h
 * @brief Detects an ECG input that is not a signal at all. Pure C, no ESP-IDF.
 *
 * The lead-off pin only knows about the electrodes. A broken wire between the AD8232 and the ADC, a
 * breakout without power, or a shorted pin leaves the ADC reading a rail or a constant, and the
 * stream would carry that as a perfectly valid "ECG" of zero microvolts. A live input never gives
 * the same 12-bit code a thousand times in a row, and a real ECG never sits on a rail for seconds:
 * so a long run of samples that are at a rail, or identical to the one before, means the front end
 * is dead, not that the person has no heartbeat.
 *
 * Reported, not acted on: the stream is never gated on it (same rule as lead-off). Compiled and run
 * on the host (tests/firmware_host/host_glue_ecg_stuck.c).
 */
#ifndef ECG_STUCK_H
#define ECG_STUCK_H

#include <stdbool.h>
#include <stdint.h>

#define ECG_STUCK_RAW_LOW      16      /* codes at or below this are the bottom rail (12-bit, 0..4095) */
#define ECG_STUCK_RAW_HIGH     4079    /* codes at or above this are the top rail */
#define ECG_STUCK_MIN_SAMPLES  1000u   /* 2 s at the 500 Hz ECG rate */

typedef struct {
    uint32_t run; /* consecutive samples that were at a rail or equal to the previous one */
    int last;     /* previous raw code; -1 = none yet */
} ecg_stuck_t;

static inline void ecg_stuck_reset(ecg_stuck_t *s)
{
    s->run = 0u;
    s->last = -1;
}

/** Feed one raw ADC code; true = the input has been dead for at least ECG_STUCK_MIN_SAMPLES. */
static inline bool ecg_stuck_update(ecg_stuck_t *s, int raw)
{
    const bool dead = raw <= ECG_STUCK_RAW_LOW || raw >= ECG_STUCK_RAW_HIGH || raw == s->last;
    s->last = raw;
    if (dead) {
        if (s->run < UINT32_MAX) {
            s->run++;
        }
    } else {
        s->run = 0u;
    }
    return s->run >= ECG_STUCK_MIN_SAMPLES;
}

#endif /* ECG_STUCK_H */
