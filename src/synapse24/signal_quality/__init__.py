"""Signal quality metrics for multimodal biosignals."""

from .ecg import (
    compute_hrv_metrics,
    detect_r_peaks_neurokit,
    r_peak_detection_quality,
    rmssd_mae,
)
from .ppg import (
    compute_ppg_sqi,
    perfusion_index,
    ppg_motion_artifact_probability,
)
from .eeg import (
    spectral_flatness,
    alpha_band_power_ratio,
)
from .base import SignalQualityMetrics, QualityThresholds

__all__ = [
    "compute_hrv_metrics",
    "detect_r_peaks_neurokit",
    "r_peak_detection_quality",
    "rmssd_mae",
    "compute_ppg_sqi",
    "perfusion_index",
    "ppg_motion_artifact_probability",
    "spectral_flatness",
    "alpha_band_power_ratio",
    "SignalQualityMetrics",
    "QualityThresholds",
]