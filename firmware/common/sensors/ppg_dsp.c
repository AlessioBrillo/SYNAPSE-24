#include "ppg_dsp.h"
#include <math.h>
#include <string.h>

#define MIN_DIST_SAMPLES  (PPG_DSP_FS_HZ * 60 / 180)  /* 180 BPM max */
#define MAX_DIST_SAMPLES  (PPG_DSP_FS_HZ * 60 / 30)   /*  30 BPM min */
#define SKIP_SAMPLES      (PPG_DSP_FS_HZ / 2)         /* filter start-up transient */
#define MAX_WINDOW        512

/* butter(2, [0.5, 8.0], 'bandpass', fs=50, output='sos') */
static const float kSos[2][6] = {
    {0.1311064399, 0.2622128798, 0.1311064399, 1.0, -0.7424725561, 0.2971177674},
    {1.0, -2.0, 1.0, 1.0, -1.9119834584, 0.9161853239},
};
/* DF-II transposed, same recurrence as scipy.signal.sosfilt. */
void ppg_bvp_bandpass(const float* ir, int n, float* out) {
    /* Priming at ir[0] == zero-state response to (ir - ir[0]) because the band-pass rejects DC.
     * Filtering the small AC deviation keeps float32 (the ESP32 FPU) accurate despite DC ~1e5. */
    float z[2][2] = {{0.0f, 0.0f}, {0.0f, 0.0f}};
    float x0 = n > 0 ? ir[0] : 0.0f;
    for (int i = 0; i < n; i++) {
        float x = ir[i] - x0;
        for (int s = 0; s < 2; s++) {
            const float* c = kSos[s];
            float y = c[0] * x + z[s][0];
            z[s][0] = c[1] * x - c[4] * y + z[s][1];
            z[s][1] = c[2] * x - c[5] * y;
            x = y;
        }
        out[i] = x;
    }
}

static float snr_db(const float* x, int n) {
    if (n < 10) return 0.0f;
    float mean = 0.0f, sig = 0.0f, noise = 0.0f;
    for (int i = 0; i < n; i++) mean += x[i];
    mean /= (float)n;
    for (int i = 0; i < n; i++) sig += (x[i] - mean) * (x[i] - mean);
    sig /= (float)n;
    for (int i = 1; i < n; i++) noise += (x[i] - x[i - 1]) * (x[i] - x[i - 1]);
    noise /= (float)(n - 1);
    if (noise <= 0.0f) return 40.0f;
    float snr = 10.0f * log10f(sig / noise);
    return snr < 0.0f ? 0.0f : (snr > 50.0f ? 50.0f : snr);
}

int ppg_dsp_analyse(const float* ir, int n, ppg_dsp_result_t* out) {
    static float bvp[MAX_WINDOW];
    if (!ir || !out || n <= 0 || n > MAX_WINDOW) return -1;
    memset(out, 0, sizeof(*out));
    ppg_bvp_bandpass(ir, n, bvp);

    float mean = 0.0f, var = 0.0f;
    for (int i = 0; i < n; i++) mean += bvp[i];
    mean /= (float)n;
    for (int i = 0; i < n; i++) var += (bvp[i] - mean) * (bvp[i] - mean);
    float sd = sqrtf(var / (float)n);
    out->snr_db = snr_db(bvp, n);
    float dc = 0.0f;
    for (int i = 0; i < n; i++) dc += ir[i] - ir[0]; /* offset from ir[0] keeps the sum exact */
    dc = fabsf(dc / (float)n + ir[0]);
    /* AC below 0.01% of DC (PI << real PPG's 0.5-5%) is float32 round-off, not a pulse */
    if (sd <= 1e-4f * dc || sd <= 0.0f) return 0;
    float thr = mean + 0.3f * sd;

    /* Local maxima above threshold; within MIN_DIST keep the taller one. */
    int idx[PPG_DSP_MAX_PEAKS];
    int np = 0;
    for (int i = SKIP_SAMPLES > 1 ? SKIP_SAMPLES : 1; i < n - 1; i++) {
        if (!(bvp[i] > bvp[i - 1] && bvp[i] >= bvp[i + 1] && bvp[i] > thr)) continue;
        if (np > 0 && i - idx[np - 1] < MIN_DIST_SAMPLES) {
            if (bvp[i] > bvp[idx[np - 1]]) idx[np - 1] = i;
        } else if (np < PPG_DSP_MAX_PEAKS) {
            idx[np++] = i;
        }
    }
    out->peak_count = np;
    for (int k = 0; k < np; k++) {  /* parabolic interpolation -> sub-sample timing */
        int i = idx[k];
        float a = bvp[i - 1], b = bvp[i], c = bvp[i + 1];
        float den = a - 2.0f * b + c;
        out->peak_pos[k] = (float)i + (den != 0.0f ? 0.5f * (a - c) / den : 0.0f);
    }

    /* Successive differences only between intervals that are neighbours in peak order:
     * a rejected (missed/extra-peak) interval breaks the chain instead of being skipped over. */
    const float ms = 1000.0f / PPG_DSP_FS_HZ;
    float iv[PPG_DSP_MAX_PEAKS];
    int ni = 0, nd = 0;
    float sum = 0.0f, rs = 0.0f, prev = -1.0f;
    for (int k = 1; k < np; k++) {
        float d = out->peak_pos[k] - out->peak_pos[k - 1];
        if (d >= MIN_DIST_SAMPLES && d <= MAX_DIST_SAMPLES) {
            iv[ni++] = d;
            sum += d;
            if (prev >= 0.0f) {
                rs += ((d - prev) * ms) * ((d - prev) * ms);
                nd++;
            }
            prev = d;
        } else {
            prev = -1.0f;
        }
    }
    out->interval_count = ni;
    if (np < 3 || ni < 2 || nd < 1) return 0;

    float mean_ms = sum / (float)ni * ms, ss = 0.0f;
    for (int k = 0; k < ni; k++) ss += (iv[k] * ms - mean_ms) * (iv[k] * ms - mean_ms);
    out->ppi_ms = mean_ms;
    out->hr_bpm = 60000.0f / mean_ms;
    out->rmssd_ms = sqrtf(rs / (float)nd);
    out->sdnn_ms = sqrtf(ss / (float)ni);
    out->valid = 1;
    return 0;
}
