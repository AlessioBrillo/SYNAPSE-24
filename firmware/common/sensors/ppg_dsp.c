#include "ppg_dsp.h"
#include <math.h>
#include <string.h>

#define MIN_DIST_SAMPLES  (PPG_DSP_FS_HZ * 60 / 180)  /* 180 BPM max */
#define MAX_DIST_SAMPLES  (PPG_DSP_FS_HZ * 60 / 30)   /*  30 BPM min */
#define SKIP_SAMPLES      (PPG_DSP_FS_HZ / 2)         /* filter start-up transient */
#define MAX_WINDOW        512

/* butter(2, [0.5, 8.0], 'bandpass', fs=50, output='sos') */
static const double kSos[2][6] = {
    {0.1311064399, 0.2622128798, 0.1311064399, 1.0, -0.7424725561, 0.2971177674},
    {1.0, -2.0, 1.0, 1.0, -1.9119834584, 0.9161853239},
};
/* sosfilt_zi(kSos) */
static const double kZi[2][2] = {{0.8144092681, -0.1498230763}, {-0.945515708, 0.945515708}};

/* DF-II transposed, same recurrence as scipy.signal.sosfilt. Double: IR DC ~1e5 counts. */
void ppg_bvp_bandpass(const float* ir, int n, float* out) {
    double z[2][2];
    double x0 = n > 0 ? ir[0] : 0.0;
    for (int s = 0; s < 2; s++) {
        z[s][0] = kZi[s][0] * x0;
        z[s][1] = kZi[s][1] * x0;
    }
    for (int i = 0; i < n; i++) {
        double x = ir[i];
        for (int s = 0; s < 2; s++) {
            const double* c = kSos[s];
            double y = c[0] * x + z[s][0];
            z[s][0] = c[1] * x - c[4] * y + z[s][1];
            z[s][1] = c[2] * x - c[5] * y;
            x = y;
        }
        out[i] = (float)x;
    }
}

static float snr_db(const float* x, int n) {
    if (n < 10) return 0.0f;
    double mean = 0.0, sig = 0.0, noise = 0.0;
    for (int i = 0; i < n; i++) mean += x[i];
    mean /= n;
    for (int i = 0; i < n; i++) sig += (x[i] - mean) * (x[i] - mean);
    sig /= n;
    for (int i = 1; i < n; i++) noise += (x[i] - x[i - 1]) * (x[i] - x[i - 1]);
    noise /= (n - 1);
    if (noise <= 0.0) return 40.0f;
    double snr = 10.0 * log10(sig / noise);
    return (float)(snr < 0.0 ? 0.0 : (snr > 50.0 ? 50.0 : snr));
}

int ppg_dsp_analyse(const float* ir, int n, ppg_dsp_result_t* out) {
    static float bvp[MAX_WINDOW];
    if (!ir || !out || n <= 0 || n > MAX_WINDOW) return -1;
    memset(out, 0, sizeof(*out));
    ppg_bvp_bandpass(ir, n, bvp);

    double mean = 0.0, var = 0.0;
    for (int i = 0; i < n; i++) mean += bvp[i];
    mean /= n;
    for (int i = 0; i < n; i++) var += (bvp[i] - mean) * (bvp[i] - mean);
    double sd = sqrt(var / n);
    out->snr_db = snr_db(bvp, n);
    double dc = 0.0;
    for (int i = 0; i < n; i++) dc += ir[i];
    dc = fabs(dc / n);
    /* AC below 0.01% of DC (PI << real PPG's 0.5-5%) is float32 round-off, not a pulse */
    if (sd <= 1e-4 * dc || sd <= 0.0) return 0;
    float thr = (float)(mean + 0.3 * sd);

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
        double a = bvp[i - 1], b = bvp[i], c = bvp[i + 1];
        double den = a - 2.0 * b + c;
        out->peak_pos[k] = (float)(i + (den != 0.0 ? 0.5 * (a - c) / den : 0.0));
    }

    float iv[PPG_DSP_MAX_PEAKS];
    int ni = 0;
    double sum = 0.0;
    for (int k = 1; k < np; k++) {
        float d = out->peak_pos[k] - out->peak_pos[k - 1];
        if (d >= MIN_DIST_SAMPLES && d <= MAX_DIST_SAMPLES) {
            iv[ni++] = d;
            sum += d;
        }
    }
    out->interval_count = ni;
    if (np < 3 || ni < 2) return 0;

    const double ms = 1000.0 / PPG_DSP_FS_HZ;
    double mean_ms = sum / ni * ms, rs = 0.0, ss = 0.0;
    for (int k = 1; k < ni; k++) rs += ((iv[k] - iv[k - 1]) * ms) * ((iv[k] - iv[k - 1]) * ms);
    for (int k = 0; k < ni; k++) ss += (iv[k] * ms - mean_ms) * (iv[k] * ms - mean_ms);
    out->ppi_ms = (float)mean_ms;
    out->hr_bpm = (float)(60000.0 / mean_ms);
    out->rmssd_ms = (float)sqrt(rs / (ni - 1));
    out->sdnn_ms = (float)sqrt(ss / ni);
    out->valid = 1;
    return 0;
}
