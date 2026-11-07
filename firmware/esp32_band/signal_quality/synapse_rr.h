/**
 * @file synapse_rr.h
 * @brief Beat-to-beat intervals from the infrared PPG. Pure C, no ESP-IDF.
 *
 * A streaming detector: one sample in, at most one interval out. It is compiled and run on the
 * host against synthetic pulse trains with known beat times (tests/test_firmware_s5_rr.py).
 *
 * What it does, and why each step is there:
 *   - the reflective PPG is inverted (the ADC count FALLS on the pulse), the baseline is removed
 *     and a short low-pass takes the noise off;
 *   - a peak is a local maximum above an adaptive threshold, with a refractory period;
 *   - the instant of the peak is interpolated with a parabola through the three samples around
 *     it: at 50 Hz the sample period is 20 ms, and an interval quantised to it would be useless
 *     for RMSSD (a heart at rest varies by tens of ms);
 *   - an interval is kept only if it is physiologically possible and close to the recent median;
 *     a rejected one breaks the chain, so no statistic ever spans an artefact, and RMSSD / SDNN
 *     are reported only while three quarters of the recent intervals are clean (noise makes
 *     intervals that are possible one by one and random together);
 *   - a gap in the samples forgets the previous beat: no interval is made across it.
 *
 * What it does not do (ponytail): it has never seen a real finger. The thresholds are starting
 * values to tune with the band on the bench (docs/hardware/BRINGUP_SOP.md), and the whole feature
 * is off (CONFIG_SYNAPSE_RR_ENABLE) until that is done.
 */
#ifndef SYNAPSE_RR_H
#define SYNAPSE_RR_H

#include <stdbool.h>
#include <stdint.h>

#define SYNAPSE_RR_RING 32u     /**< intervals kept for RMSSD / SDNN (clean ones and break marks) */
#define SYNAPSE_RR_PENDING 8u   /**< intervals waiting to be reported */
#define SYNAPSE_RR_MIN_FOR_HRV 8u /**< clean intervals needed before RMSSD / SDNN are reported */

#define SYNAPSE_RR_MIN_MS 300.0f   /**< 200 bpm */
#define SYNAPSE_RR_MAX_MS 2000.0f  /**< 30 bpm */
#define SYNAPSE_RR_TOLERANCE 0.20f /**< share of the recent median an interval may deviate by */
#define SYNAPSE_RR_MIN_CLEAN_SHARE 0.75f /**< of the recent intervals, for RMSSD / SDNN to be reported */
#define SYNAPSE_RR_MIN_AMPLITUDE 50.0f /**< ADC counts: a flatter pulse is not a pulse */
#define SYNAPSE_RR_MAX_GAP_US 200000   /**< a longer silence between samples forgets the last beat */
#define SYNAPSE_RR_REFRACTORY_SHARE 0.6f /**< of the recent median: the echo window for the next peak */
/** A peak inside the echo window (this, or 0.6 x median) that is lower than SHARE x the previous
 *  one is a dicrotic wave. */
#define SYNAPSE_RR_SECONDARY_WINDOW_MS 450.0f
#define SYNAPSE_RR_SECONDARY_SHARE 0.7f
#define SYNAPSE_RR_RESYNC_AFTER 4u /**< consecutive rejected intervals before the history is dropped */
#define SYNAPSE_RR_EXPIRE_US 5000000   /**< no beat for this long: the recent intervals are stale */

typedef struct {
    /* conditioning */
    bool primed;
    float baseline;
    float smooth;
    int64_t last_us;
    /* the last three conditioned samples, oldest first */
    float y[3];
    int64_t t[3];
    uint8_t filled;
    /* detector */
    float amplitude;
    bool have_prev;
    int64_t prev_peak_us;
    float prev_height; /* conditioned height of that peak */
    /* intervals (ms): 0 marks a break (a rejected interval or a gap) */
    float ring[SYNAPSE_RR_RING];
    uint8_t ring_n;
    uint8_t ring_head;
    /* reported to the consumer, in 1/1024 s (the Bluetooth RR unit) */
    uint16_t pending[SYNAPSE_RR_PENDING];
    uint32_t pending_total;
    /* diagnostics */
    uint32_t accepted;
    uint32_t rejected;
    uint8_t reject_run; /**< consecutive rejected intervals */
} synapse_rr_t;

void synapse_rr_init(synapse_rr_t *st);

/** One infrared sample (ADC counts) at @p ts_us. Returns true when it completed an interval. */
bool synapse_rr_push(synapse_rr_t *st, float ir, int64_t ts_us);

/**
 * Look at the intervals found since the last commit, oldest first, in 1/1024 s. Returns how many
 * there were in total (it can exceed @p max: only the newest @p max are copied). Nothing is
 * cleared: an interval is consumed only once a frame carrying it has left (synapse_rr_commit).
 */
uint32_t synapse_rr_peek(const synapse_rr_t *st, uint16_t *out, uint8_t max);

/** Mark every interval seen by the last peek as reported (or as not worth keeping). */
void synapse_rr_commit(synapse_rr_t *st);

/** peek + commit, for callers that have nothing to wait for. */
uint32_t synapse_rr_take(synapse_rr_t *st, uint16_t *out, uint8_t max);

/** Forget the recent intervals when no beat has been seen for SYNAPSE_RR_EXPIRE_US.
 *  @p now_us must be on the same clock as the ts_us given to synapse_rr_push(). */
void synapse_rr_expire(synapse_rr_t *st, int64_t now_us);

/** RMSSD and SDNN (ms) over the recent clean intervals; false while there are too few. */
bool synapse_rr_hrv(const synapse_rr_t *st, float *rmssd_ms, float *sdnn_ms);

#endif /* SYNAPSE_RR_H */
