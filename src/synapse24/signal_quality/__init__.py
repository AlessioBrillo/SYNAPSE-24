"""Signal quality metrics for multimodal biosignals."""

from .base import QualityThresholds, SignalQualityMetrics
from .ecg import (
    compute_ecg_quality,
    compute_hrv_metrics,
    detect_r_peaks_neurokit,
    r_peak_detection_quality,
    rmssd_mae,
)
from .eeg import (
    alpha_band_power_ratio,
    compute_eeg_quality,
    spectral_flatness,
)
from .ppg import (
    compute_ppg_quality,
    compute_ppg_sqi,
    perfusion_index,
    ppg_motion_artifact_probability,
)

__all__ = [
    "compute_ecg_quality",
    "compute_eeg_quality",
    "compute_hrv_metrics",
    "compute_ppg_quality",
    "compute_ppg_sqi",
    "detect_r_peaks_neurokit",
    "perfusion_index",
    "ppg_motion_artifact_probability",
    "r_peak_detection_quality",
    "rmssd_mae",
    "spectral_flatness",
    "alpha_band_power_ratio",
    "SignalQualityMetrics",
    "QualityThresholds",
]
