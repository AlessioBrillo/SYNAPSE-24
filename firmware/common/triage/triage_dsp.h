#pragma once
#ifdef __cplusplus
extern "C" {
#endif

#ifndef TRIAGE_NUM_FEATURES
#define TRIAGE_NUM_FEATURES 26
#endif
#define TRIAGE_IMU_FS_HZ 50   /* must equal feature_extraction.TRIAGE_IMU_FS_HZ */
#define TRIAGE_PPG_FS_HZ 50
#define TRIAGE_WINDOW_S  4
#define TRIAGE_IMU_WIN   (TRIAGE_IMU_FS_HZ * TRIAGE_WINDOW_S)
#define TRIAGE_PPG_WIN   (TRIAGE_PPG_FS_HZ * TRIAGE_WINDOW_S)

/* Zero-initialised == unprimed; first step primes to steady state (scipy sosfilt_zi * x0). */
typedef struct { double z[2][2]; int primed; } triage_bvp_filter_t;

float triage_bvp_filter_step(triage_bvp_filter_t* f, float ir);

/* imu: n_imu x 6 row-major (ax,ay,az,gx,gy,gz), oldest first, n_imu <= TRIAGE_IMU_WIN.
 * bvp: already band-passed, oldest first. Mirrors extract_triage_features_live(). */
void triage_features_from_window(const float* imu, int n_imu, const float* bvp, int n_bvp,
                                 float fs_imu, float out[TRIAGE_NUM_FEATURES]);

#ifdef __cplusplus
}
#endif
