#pragma once
/* Pure C99 PPG DSP (no ESP-IDF deps): host-testable against ground-truth beat times.
 * Used by ppg_processor.cpp (HR/HRV) and triage_dsp.c (BVP features). */
#ifdef __cplusplus
extern "C" {
#endif

#define PPG_DSP_FS_HZ     50   /* filter coefficients are designed for this rate */
#define PPG_DSP_MAX_PEAKS 32

/* 2nd-order Butterworth band-pass 0.5-8 Hz @ 50 Hz, steady-state primed at ir[0]
 * (== scipy butter(2,[0.5,8],fs=50,'bandpass',output='sos') + sosfilt_zi*ir[0]).
 * Input is raw counts (DC ~1e5); output is the AC component. n may be 0. */
void ppg_bvp_bandpass(const float* ir, int n, float* out);

typedef struct {
    float hr_bpm, rmssd_ms, sdnn_ms, ppi_ms, snr_db;
    int peak_count, interval_count;
    int valid;                          /* >= 3 peaks and >= 2 plausible intervals */
    float peak_pos[PPG_DSP_MAX_PEAKS];  /* fractional sample index of each peak (parabolic) */
} ppg_dsp_result_t;

/* Analyse one window of raw IR at PPG_DSP_FS_HZ. Returns 0 on success, -1 on bad args. */
int ppg_dsp_analyse(const float* ir, int n, ppg_dsp_result_t* out);

#ifdef __cplusplus
}
#endif
