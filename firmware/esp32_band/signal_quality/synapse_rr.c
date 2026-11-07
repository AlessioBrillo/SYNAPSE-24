#include "synapse_rr.h"

#include <math.h>
#include <string.h>

/* Time constants of the conditioning, seconds. */
#define BASELINE_TAU_S 1.5f /* ~0.1 Hz high-pass: slow drift out, the pulse (>= 0.5 Hz) in */
#define SMOOTH_TAU_S 0.05f  /* ~3 Hz low-pass: the noise out, the 1-2.5 Hz pulse in */
#define AMPLITUDE_DECAY_S 10.0f
#define THRESHOLD_SHARE 0.4f

void synapse_rr_init(synapse_rr_t *st)
{
    memset(st, 0, sizeof(*st));
}

static void forget_beat(synapse_rr_t *st)
{
    st->have_prev = false;
    st->filled = 0;
}

static void ring_push(synapse_rr_t *st, float value)
{
    st->ring[st->ring_head] = value;
    st->ring_head = (uint8_t)((st->ring_head + 1u) % SYNAPSE_RR_RING);
    if (st->ring_n < SYNAPSE_RR_RING) {
        st->ring_n++;
    }
}

/* ring[] in time order, oldest first; returns the count. */
static uint8_t ring_ordered(const synapse_rr_t *st, float *out)
{
    const uint8_t start = (uint8_t)((st->ring_head + SYNAPSE_RR_RING - st->ring_n) % SYNAPSE_RR_RING);
    for (uint8_t i = 0; i < st->ring_n; i++) {
        out[i] = st->ring[(start + i) % SYNAPSE_RR_RING];
    }
    return st->ring_n;
}

/* Median of the clean intervals among the last few; 0 when there are fewer than three. */
static float recent_median(const synapse_rr_t *st)
{
    float ordered[SYNAPSE_RR_RING];
    float clean[5];
    uint8_t n = ring_ordered(st, ordered);
    uint8_t m = 0;
    for (int i = (int)n - 1; i >= 0 && m < 5u; i--) {
        if (ordered[i] > 0.0f) {
            clean[m++] = ordered[i];
        }
    }
    if (m < 3u) {
        return 0.0f;
    }
    for (uint8_t i = 1; i < m; i++) { /* insertion sort of at most five values */
        const float v = clean[i];
        int j = (int)i - 1;
        while (j >= 0 && clean[j] > v) {
            clean[j + 1] = clean[j];
            j--;
        }
        clean[j + 1] = v;
    }
    return clean[m / 2u];
}

static void report(synapse_rr_t *st, float rr_ms)
{
    uint32_t units = (uint32_t)(rr_ms * 1024.0f / 1000.0f + 0.5f);
    if (units > 0xFFFEu) {
        units = 0xFFFEu;
    }
    if (st->pending_total < SYNAPSE_RR_PENDING) {
        st->pending[st->pending_total] = (uint16_t)units;
    } else { /* keep the newest: shift left */
        memmove(st->pending, st->pending + 1, (SYNAPSE_RR_PENDING - 1u) * sizeof(st->pending[0]));
        st->pending[SYNAPSE_RR_PENDING - 1u] = (uint16_t)units;
    }
    st->pending_total++;
}

bool synapse_rr_push(synapse_rr_t *st, float ir, int64_t ts_us)
{
    if (!(ir == ir)) { /* NaN */
        return false;
    }
    if (st->primed) {
        const int64_t dt_us = ts_us - st->last_us;
        if (dt_us <= 0) {
            return false; /* a repeat or an older sample */
        }
        if (dt_us > SYNAPSE_RR_MAX_GAP_US) {
            /* The recording has a hole: the beat before it is too far to pair with the next one. */
            forget_beat(st);
            ring_push(st, 0.0f);
            st->primed = false;
        }
    }
    const float x = -ir; /* reflective PPG: the count falls when the blood volume rises */
    if (!st->primed) {
        st->primed = true;
        st->baseline = x;
        st->smooth = 0.0f;
        st->last_us = ts_us;
        return false;
    }
    const float dt = (float)(ts_us - st->last_us) * 1e-6f;
    st->last_us = ts_us;
    st->baseline += (x - st->baseline) * (dt / (BASELINE_TAU_S + dt));
    const float high = x - st->baseline;
    st->smooth += (high - st->smooth) * (dt / (SMOOTH_TAU_S + dt));
    st->amplitude -= st->amplitude * (dt / AMPLITUDE_DECAY_S);

    /* slide the three-sample window */
    st->y[0] = st->y[1];
    st->y[1] = st->y[2];
    st->y[2] = st->smooth;
    st->t[0] = st->t[1];
    st->t[1] = st->t[2];
    st->t[2] = ts_us;
    if (st->filled < 3u) {
        st->filled++;
        return false;
    }

    const float y0 = st->y[0];
    const float y1 = st->y[1];
    const float y2 = st->y[2];
    float threshold = THRESHOLD_SHARE * st->amplitude;
    if (threshold < SYNAPSE_RR_MIN_AMPLITUDE) {
        threshold = SYNAPSE_RR_MIN_AMPLITUDE;
    }
    if (!(y1 > y0 && y1 >= y2 && y1 > threshold)) {
        return false;
    }

    /* Parabola through the three samples: the true maximum lies between them. */
    const float denom = y0 - 2.0f * y1 + y2;
    float offset = 0.0f; /* in samples, relative to the middle one */
    if (denom < 0.0f) {
        offset = 0.5f * (y0 - y2) / denom;
        if (offset > 1.0f) {
            offset = 1.0f;
        } else if (offset < -1.0f) {
            offset = -1.0f;
        }
    }
    const float half_span_us = (float)(st->t[2] - st->t[0]) * 0.5f;
    const int64_t peak_us = st->t[1] + (int64_t)(offset * half_span_us);

    /* Two kinds of "too soon":
     *  - under SYNAPSE_RR_MIN_MS (200 bpm) it is the same beat seen twice, or noise: dropped;
     *  - the dicrotic wave comes 250-400 ms after the systolic peak and can clear the amplitude
     *    threshold. It is LOWER than the peak it follows, while a real fast beat is about as
     *    high as the last one. So a peak inside the recent-rhythm window (0.6 x median, at least
     *    SYNAPSE_RR_SECONDARY_WINDOW_MS) is dropped only if it is clearly lower.
     * A real beat that comes sooner than the median allows is NOT dropped here: it is kept, fails
     * the closeness test below, and is counted as a rejection, so a median learned from a bad
     * start (or a sudden rise in heart rate) is dropped after SYNAPSE_RR_RESYNC_AFTER of them
     * instead of locking the detector onto every second beat. */
    const float median = st->have_prev ? recent_median(st) : 0.0f;
    if (st->have_prev) {
        const float since_ms = (float)(peak_us - st->prev_peak_us) * 1e-3f;
        if (since_ms < SYNAPSE_RR_MIN_MS) {
            return false;
        }
        float echo_window_ms = SYNAPSE_RR_SECONDARY_WINDOW_MS;
        if (SYNAPSE_RR_REFRACTORY_SHARE * median > echo_window_ms) {
            echo_window_ms = SYNAPSE_RR_REFRACTORY_SHARE * median;
        }
        if (since_ms < echo_window_ms && y1 < SYNAPSE_RR_SECONDARY_SHARE * st->prev_height) {
            return false;
        }
    }
    st->amplitude = st->amplitude > 0.0f ? 0.8f * st->amplitude + 0.2f * y1 : y1;

    bool produced = false;
    if (st->have_prev) {
        const float rr_ms = (float)(peak_us - st->prev_peak_us) * 1e-3f;
        const bool possible = rr_ms >= SYNAPSE_RR_MIN_MS && rr_ms <= SYNAPSE_RR_MAX_MS;
        const bool close = median <= 0.0f || fabsf(rr_ms - median) <= SYNAPSE_RR_TOLERANCE * median;
        if (possible && close) {
            ring_push(st, rr_ms);
            report(st, rr_ms);
            st->accepted++;
            st->reject_run = 0;
            produced = true;
        } else {
            ring_push(st, 0.0f); /* the chain is broken here: no pair may span it */
            st->rejected++;
            if (++st->reject_run >= SYNAPSE_RR_RESYNC_AFTER) {
                /* Every beat is "wrong" against the median: the median is, not the beats
                 * (it was learned from a bad start). Forget it and learn again. */
                st->ring_n = 0;
                st->ring_head = 0;
                st->reject_run = 0;
            }
        }
    }
    st->have_prev = true;
    st->prev_peak_us = peak_us;
    st->prev_height = y1;
    return produced;
}

uint32_t synapse_rr_peek(const synapse_rr_t *st, uint16_t *out, uint8_t max)
{
    const uint32_t total = st->pending_total;
    const uint32_t held = total < SYNAPSE_RR_PENDING ? total : SYNAPSE_RR_PENDING;
    const uint32_t copy = held < max ? held : max;
    if (out != NULL) {
        for (uint32_t i = 0; i < copy; i++) {
            out[i] = st->pending[held - copy + i];
        }
    }
    return total;
}

void synapse_rr_commit(synapse_rr_t *st)
{
    st->pending_total = 0;
}

uint32_t synapse_rr_take(synapse_rr_t *st, uint16_t *out, uint8_t max)
{
    const uint32_t total = synapse_rr_peek(st, out, max);
    synapse_rr_commit(st);
    return total;
}

void synapse_rr_expire(synapse_rr_t *st, int64_t now_us)
{
    if (st->have_prev && now_us - st->prev_peak_us > SYNAPSE_RR_EXPIRE_US) {
        st->ring_n = 0; /* the old intervals describe a rhythm that is no longer being measured */
        st->ring_head = 0;
        st->have_prev = false;
        st->reject_run = 0;
        st->prev_height = 0.0f;
    }
}

bool synapse_rr_hrv(const synapse_rr_t *st, float *rmssd_ms, float *sdnn_ms)
{
    float ordered[SYNAPSE_RR_RING];
    const uint8_t n = ring_ordered(st, ordered);
    float sum = 0.0f;
    uint32_t clean = 0;
    for (uint8_t i = 0; i < n; i++) {
        if (ordered[i] > 0.0f) {
            sum += ordered[i];
            clean++;
        }
    }
    if (clean < SYNAPSE_RR_MIN_FOR_HRV || (float)clean < SYNAPSE_RR_MIN_CLEAN_SHARE * (float)n) {
        return false;
    }
    const float mean = sum / (float)clean;
    float var = 0.0f;
    for (uint8_t i = 0; i < n; i++) {
        if (ordered[i] > 0.0f) {
            var += (ordered[i] - mean) * (ordered[i] - mean);
        }
    }
    float sq = 0.0f;
    uint32_t pairs = 0;
    for (uint8_t i = 1; i < n; i++) {
        if (ordered[i] > 0.0f && ordered[i - 1u] > 0.0f) {
            const float d = ordered[i] - ordered[i - 1u];
            sq += d * d;
            pairs++;
        }
    }
    if (pairs == 0u) {
        return false;
    }
    if (sdnn_ms != NULL) {
        *sdnn_ms = sqrtf(var / (float)(clean - 1u));
    }
    if (rmssd_ms != NULL) {
        *rmssd_ms = sqrtf(sq / (float)pairs);
    }
    return true;
}
