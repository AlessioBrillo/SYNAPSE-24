#include "triage_dsp.h"
#include <math.h>
#include <string.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif
#define WELCH_MAX_NPERSEG 256

/* butter(2, [0.5, 8.0], 'bandpass', fs=50, output='sos') - mirrors feature_extraction.BVP_SOS */
static const double kSos[2][6] = {
    {0.1311064399, 0.2622128798, 0.1311064399, 1.0, -0.7424725561, 0.2971177674},
    {1.0, -2.0, 1.0, 1.0, -1.9119834584, 0.9161853239},
};
/* sosfilt_zi(kSos) */
static const double kZi[2][2] = {{0.8144092681, -0.1498230763}, {-0.945515708, 0.945515708}};

/* DF-II transposed, identical recurrence to scipy.signal.sosfilt. Double: IR DC ~1e5 counts. */
typedef struct { double z[2][2]; } bvp_filter_t;

static float bvp_filter_step(bvp_filter_t* f, float ir) {
    double x = ir;
    for (int s = 0; s < 2; s++) {
        const double* c = kSos[s];
        double y = c[0] * x + f->z[s][0];
        f->z[s][0] = c[1] * x - c[4] * y + f->z[s][1];
        f->z[s][1] = c[2] * x - c[5] * y;
        x = y;
    }
    return (float)x;
}

void triage_bvp_from_ir(const float* ir, int n, float* bvp_out) {
    bvp_filter_t f;
    for (int s = 0; s < 2; s++) {
        f.z[s][0] = kZi[s][0] * (n > 0 ? ir[0] : 0.0f);
        f.z[s][1] = kZi[s][1] * (n > 0 ? ir[0] : 0.0f);
    }
    for (int i = 0; i < n; i++) bvp_out[i] = bvp_filter_step(&f, ir[i]);
}

/* One-sided Welch PSD as scipy.signal.welch defaults: periodic Hann, nperseg=min(256,n),
 * 50% overlap, constant detrend, mean average. Density scale omitted: cancels in
 * entropy normalisation and argmax. Static scratch: NOT reentrant; triage_features.cpp serialises callers. */
static int welch_psd(const float* x, int n, float* psd) {
    static float seg[WELCH_MAX_NPERSEG], tc[WELCH_MAX_NPERSEG], ts[WELCH_MAX_NPERSEG];
    int nper = n < WELCH_MAX_NPERSEG ? n : WELCH_MAX_NPERSEG;
    int step = nper - nper / 2;
    int nseg = (n - nper) / step + 1;
    int nbins = nper / 2 + 1;
    for (int i = 0; i < nper; i++) {
        tc[i] = cosf(2.0f * (float)M_PI * (float)i / (float)nper);
        ts[i] = sinf(2.0f * (float)M_PI * (float)i / (float)nper);
    }
    memset(psd, 0, (size_t)nbins * sizeof(float));
    for (int s = 0; s < nseg; s++) {
        const float* xs = x + s * step;
        double mean = 0.0;
        for (int i = 0; i < nper; i++) mean += xs[i];
        mean /= nper;
        for (int i = 0; i < nper; i++) seg[i] = (float)(xs[i] - mean) * (0.5f - 0.5f * tc[i]);
        /* ponytail: O(N^2) DFT w/ twiddle table, ~240k MAC/inference at N=200; esp-dsp FFT if CPU budget bites */
        for (int k = 0; k < nbins; k++) {
            float re = 0.0f, im = 0.0f;
            for (int i = 0; i < nper; i++) {
                int idx = (k * i) % nper;
                re += seg[i] * tc[idx];
                im -= seg[i] * ts[idx];
            }
            float p = re * re + im * im;
            if (k != 0 && !(nper % 2 == 0 && k == nper / 2)) p *= 2.0f;
            psd[k] += p;
        }
    }
    return nbins;
}

static void axis_features(const float* x, int n, float fs, float out[4]) {
    static float psd[WELCH_MAX_NPERSEG / 2 + 1];
    double mean = 0.0, var = 0.0;
    for (int i = 0; i < n; i++) mean += x[i];
    mean /= n;
    for (int i = 0; i < n; i++) var += (x[i] - mean) * (x[i] - mean);
    out[0] = (float)mean;
    out[1] = (float)sqrt(var / n); /* np.std, ddof=0 */

    int nbins = welch_psd(x, n, psd);
    int nper = n < WELCH_MAX_NPERSEG ? n : WELCH_MAX_NPERSEG;
    double sum = 0.0;
    int kmax = 0;
    for (int k = 0; k < nbins; k++) {
        sum += psd[k];
        if (psd[k] > psd[kmax]) kmax = k;
    }
    double h = 0.0;
    if (sum > 0.0) { /* scipy.stats.entropy(psd/sum + 1e-12): renormalises to sum 1 */
        double norm = 1.0 + nbins * 1e-12;
        for (int k = 0; k < nbins; k++) {
            double p = (psd[k] / sum + 1e-12) / norm;
            h -= p * log(p);
        }
        h /= log((double)nbins);
    }
    out[2] = (float)h;
    out[3] = (float)kmax * fs / (float)nper; /* first argmax, as np.argmax */
}

void triage_features_from_window(const float* imu, int n_imu, const float* bvp, int n_bvp,
                                 float fs_imu, float out[TRIAGE_NUM_FEATURES]) {
    static float axis[TRIAGE_IMU_WIN];
    memset(out, 0, TRIAGE_NUM_FEATURES * sizeof(float));
    if (n_imu < 10 || n_imu > TRIAGE_IMU_WIN) return; /* Python: < 10 -> zeros */
    for (int a = 0; a < 6; a++) { /* 0-11 acc, 12-23 gyro */
        for (int i = 0; i < n_imu; i++) axis[i] = imu[i * 6 + a];
        axis_features(axis, n_imu, fs_imu, &out[a * 4]);
    }
    if (n_bvp > 0) { /* 24-25 BVP mean/std */
        double mean = 0.0, var = 0.0;
        for (int i = 0; i < n_bvp; i++) mean += bvp[i];
        mean /= n_bvp;
        for (int i = 0; i < n_bvp; i++) var += (bvp[i] - mean) * (bvp[i] - mean);
        out[24] = (float)mean;
        out[25] = (float)sqrt(var / n_bvp);
    }
}
