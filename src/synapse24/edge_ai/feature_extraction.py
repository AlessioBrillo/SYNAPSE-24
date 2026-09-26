"""Feature extraction for SYNAPSE-24 Edge AI Triage Model.

Implements the EXACT 26-feature pipeline matching the WESAD-trained model:
- Chest ACC (700Hz): mean, std, spectral entropy, dominant freq x 3 axes = 12 features
- Wrist ACC (32Hz): mean, std, spectral entropy, dominant freq x 3 axes = 12 features
- Wrist BVP (64Hz): mean, std = 2 features

TOTAL: 26 features -- must match firmware/triage_inference.cpp and model_data.h
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from scipy.signal import welch
from scipy.stats import entropy

# Feature indices (must match firmware/triage_inference.cpp TRIAGE_NUM_FEATURES=26)
FEAT_CHEST_ACC_MEAN_X = 0
FEAT_CHEST_ACC_STD_X = 1
FEAT_CHEST_ACC_ENTROPY_X = 2
FEAT_CHEST_ACC_DOMFREQ_X = 3

FEAT_CHEST_ACC_MEAN_Y = 4
FEAT_CHEST_ACC_STD_Y = 5
FEAT_CHEST_ACC_ENTROPY_Y = 6
FEAT_CHEST_ACC_DOMFREQ_Y = 7

FEAT_CHEST_ACC_MEAN_Z = 8
FEAT_CHEST_ACC_STD_Z = 9
FEAT_CHEST_ACC_ENTROPY_Z = 10
FEAT_CHEST_ACC_DOMFREQ_Z = 11

FEAT_WRIST_ACC_MEAN_X = 12
FEAT_WRIST_ACC_STD_X = 13
FEAT_WRIST_ACC_ENTROPY_X = 14
FEAT_WRIST_ACC_DOMFREQ_X = 15

FEAT_WRIST_ACC_MEAN_Y = 16
FEAT_WRIST_ACC_STD_Y = 17
FEAT_WRIST_ACC_ENTROPY_Y = 18
FEAT_WRIST_ACC_DOMFREQ_Y = 19

FEAT_WRIST_ACC_MEAN_Z = 20
FEAT_WRIST_ACC_STD_Z = 21
FEAT_WRIST_ACC_ENTROPY_Z = 22
FEAT_WRIST_ACC_DOMFREQ_Z = 23

FEAT_WRIST_BVP_MEAN = 24
FEAT_WRIST_BVP_STD = 25

TRIAGE_NUM_FEATURES = 26

# Sampling rates per WESAD dataset
CHEST_ACC_FS = 700.0
WRIST_ACC_FS = 32.0
WRIST_BVP_FS = 64.0

# Window duration for feature computation (seconds)
WINDOW_DURATION_S = 4.0


def spectral_entropy(signal: npt.NDArray[np.float64], fs: float) -> float:
    """Compute normalized spectral entropy (Wiener entropy) of a signal in [0,1]."""
    if len(signal) < 4:
        return 0.0
    freqs, psd = welch(signal, fs=fs, nperseg=min(256, len(signal)))
    psd_sum = np.sum(psd)
    if psd_sum == 0:
        return 0.0
    psd_norm = psd / psd_sum
    # Shannon entropy in nats, normalized by max possible entropy (log(n_bins))
    ent = float(entropy(psd_norm + 1e-12))
    max_ent = np.log(len(psd_norm))
    return ent / max_ent if max_ent > 0 else 0.0


def dominant_frequency(signal: npt.NDArray[np.float64], fs: float) -> float:
    if len(signal) < 4:
        return 0.0
    freqs, psd = welch(signal, fs=fs, nperseg=min(256, len(signal)))
    idx = np.argmax(psd)
    return float(freqs[idx])


def compute_axis_features(
    signal: npt.NDArray[np.float64], fs: float
) -> tuple[float, float, float, float]:
    if len(signal) == 0:
        return 0.0, 0.0, 0.0, 0.0
    mean_val = float(np.mean(signal))
    std_val = float(np.std(signal))
    ent_val = spectral_entropy(signal, fs)
    domfreq_val = dominant_frequency(signal, fs)
    return mean_val, std_val, ent_val, domfreq_val


def extract_triage_features_wesad(
    chest_acc: npt.NDArray[np.float64],
    wrist_acc: npt.NDArray[np.float64],
    wrist_bvp: npt.NDArray[np.float64],
) -> np.ndarray:
    """Extract 26 features from WESAD-format data (training/validation)."""
    features = np.zeros(TRIAGE_NUM_FEATURES, dtype=np.float32)
    for axis in range(3):
        base = axis * 4
        if chest_acc.shape[0] > 0:
            mean_v, std_v, ent_v, dom_v = compute_axis_features(chest_acc[:, axis], CHEST_ACC_FS)
            features[base + 0] = mean_v
            features[base + 1] = std_v
            features[base + 2] = ent_v
            features[base + 3] = dom_v
    for axis in range(3):
        base = 12 + axis * 4
        if wrist_acc.shape[0] > 0:
            mean_v, std_v, ent_v, dom_v = compute_axis_features(wrist_acc[:, axis], WRIST_ACC_FS)
            features[base + 0] = mean_v
            features[base + 1] = std_v
            features[base + 2] = ent_v
            features[base + 3] = dom_v
    if wrist_bvp.shape[0] > 0:
        features[FEAT_WRIST_BVP_MEAN] = float(np.mean(wrist_bvp))
        features[FEAT_WRIST_BVP_STD] = float(np.std(wrist_bvp))
    return features


def extract_triage_features_live(
    imu_window: npt.NDArray[np.float64],
    ppg_window: npt.NDArray[np.float64] | None = None,
    ecg_window: npt.NDArray[np.float64] | None = None,
) -> np.ndarray:
    """Extract 26 features from LIVE sensor streams (forearm hub only).

    DOMAIN ADAPTATION: WESAD model trained on CHEST ACC (700Hz) + WRIST ACC (32Hz) + WRIST BVP (64Hz).
    Live hardware has FOREARM IMU (100Hz) + PPG (64Hz) + ECG (500Hz).
    Mapping: IMU ACC -> Chest ACC features; IMU GYRO -> Wrist ACC features; PPG IR -> Wrist BVP features.
    """
    features = np.zeros(TRIAGE_NUM_FEATURES, dtype=np.float32)
    if imu_window.shape[0] < 10:
        return features
    # Chest-like features from IMU ACC (features 0-11)
    for axis, sig in enumerate([imu_window[:, 0], imu_window[:, 1], imu_window[:, 2]]):
        base = axis * 4
        mean_v, std_v, ent_v, dom_v = compute_axis_features(sig, 100.0)
        features[base + 0] = mean_v
        features[base + 1] = std_v
        features[base + 2] = ent_v
        features[base + 3] = dom_v
    # Wrist-like features from IMU GYRO (features 12-23)
    for axis, sig in enumerate([imu_window[:, 3], imu_window[:, 4], imu_window[:, 5]]):
        base = 12 + axis * 4
        mean_v, std_v, ent_v, dom_v = compute_axis_features(sig, 100.0)
        features[base + 0] = mean_v
        features[base + 1] = std_v
        features[base + 2] = ent_v
        features[base + 3] = dom_v
    # BVP features from PPG IR (features 24-25)
    if ppg_window is not None and ppg_window.shape[0] > 0:
        bvp_proxy = ppg_window[:, 1]
        features[FEAT_WRIST_BVP_MEAN] = float(np.mean(bvp_proxy))
        features[FEAT_WRIST_BVP_STD] = float(np.std(bvp_proxy))
    return features


def extract_triage_features_from_lsl_streams(
    ecg_stream: npt.NDArray[np.float64],
    ppg_stream: npt.NDArray[np.float64],
    acc_stream: npt.NDArray[np.float64],
    gyro_stream: npt.NDArray[np.float64],
    mag_stream: npt.NDArray[np.float64],
    window_s: float = 4.0,
) -> np.ndarray:
    """Extract features from synchronized LSL streams (post-hoc analysis)."""
    n_acc = int(window_s * 100)
    n_ppg = int(window_s * 64)
    acc_win = acc_stream[-n_acc:] if len(acc_stream) >= n_acc else acc_stream
    gyro_win = gyro_stream[-n_acc:] if len(gyro_stream) >= n_acc else gyro_stream
    ppg_win = ppg_stream[-n_ppg:] if len(ppg_stream) >= n_ppg else ppg_stream
    min_len = min(len(acc_win), len(gyro_win))
    if min_len == 0:
        return np.zeros(TRIAGE_NUM_FEATURES, dtype=np.float32)
    imu_win = np.hstack([acc_win[:min_len], gyro_win[:min_len], np.zeros((min_len, 3))])
    return extract_triage_features_live(imu_win, ppg_win)


FIRMWARE_FEATURE_EXTRACTION_H = r"""#pragma once

#include <stdint.h>
#include <stdbool.h>
#include <math.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TRIAGE_NUM_FEATURES 26
#define TRIAGE_FEATURE_CHEST_ACC_MEAN_X     0
#define TRIAGE_FEATURE_CHEST_ACC_STD_X      1
#define TRIAGE_FEATURE_CHEST_ACC_ENTROPY_X  2
#define TRIAGE_FEATURE_CHEST_ACC_DOMFREQ_X  3
#define TRIAGE_FEATURE_CHEST_ACC_MEAN_Y     4
#define TRIAGE_FEATURE_CHEST_ACC_STD_Y      5
#define TRIAGE_FEATURE_CHEST_ACC_ENTROPY_Y  6
#define TRIAGE_FEATURE_CHEST_ACC_DOMFREQ_Y  7
#define TRIAGE_FEATURE_CHEST_ACC_MEAN_Z     8
#define TRIAGE_FEATURE_CHEST_ACC_STD_Z      9
#define TRIAGE_FEATURE_CHEST_ACC_ENTROPY_Z  10
#define TRIAGE_FEATURE_CHEST_ACC_DOMFREQ_Z  11
#define TRIAGE_FEATURE_WRIST_ACC_MEAN_X     12
#define TRIAGE_FEATURE_WRIST_ACC_STD_X      13
#define TRIAGE_FEATURE_WRIST_ACC_ENTROPY_X  14
#define TRIAGE_FEATURE_WRIST_ACC_DOMFREQ_X  15
#define TRIAGE_FEATURE_WRIST_ACC_MEAN_Y     16
#define TRIAGE_FEATURE_WRIST_ACC_STD_Y      17
#define TRIAGE_FEATURE_WRIST_ACC_ENTROPY_Y  18
#define TRIAGE_FEATURE_WRIST_ACC_DOMFREQ_Y  19
#define TRIAGE_FEATURE_WRIST_ACC_MEAN_Z     20
#define TRIAGE_FEATURE_WRIST_ACC_STD_Z      21
#define TRIAGE_FEATURE_WRIST_ACC_ENTROPY_Z  22
#define TRIAGE_FEATURE_WRIST_ACC_DOMFREQ_Z  23
#define TRIAGE_FEATURE_WRIST_BVP_MEAN       24
#define TRIAGE_FEATURE_WRIST_BVP_STD        25

#define TRIAGE_LIVE_IMU_FS_HZ     100
#define TRIAGE_LIVE_PPG_FS_HZ     64

typedef struct {
    float features[TRIAGE_NUM_FEATURES];
    int feature_count;
    int64_t timestamp_us;
} triage_features_t;

esp_err_t triage_features_compute_live(
    const void* imu_ring_buffer,
    const void* ppg_ring_buffer,
    triage_features_t* features_out
);

#ifdef __cplusplus
}
#endif
"""


def generate_firmware_header(output_path: str) -> None:
    with open(output_path, "w") as f:
        f.write(FIRMWARE_FEATURE_EXTRACTION_H)


if __name__ == "__main__":
    np.random.seed(42)
    chest_acc = np.random.randn(2800, 3) * 0.1
    wrist_acc = np.random.randn(128, 3) * 0.1
    wrist_bvp = np.random.randn(256) * 0.01
    feats = extract_triage_features_wesad(chest_acc, wrist_acc, wrist_bvp)
    print(f"WESAD features shape: {feats.shape}")
    imu_live = np.random.randn(30, 9) * 0.1
    ppg_live = np.random.randn(10, 2) * 1000
    feats_live = extract_triage_features_live(imu_live, ppg_live)
    print(f"Live features shape: {feats_live.shape}")
    generate_firmware_header("firmware/triage_features.h")
    print("Generated firmware/triage_features.h")
