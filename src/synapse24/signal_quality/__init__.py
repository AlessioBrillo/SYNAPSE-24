"""Signal quality metrics for multimodal biosignals."""

from .base import QualityThresholds, SignalQualityMetrics, Tier, compute_snr
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
    compute_yasa_kappa,
    compute_yasa_sleep_staging,
    spectral_flatness,
    validate_sleep_staging_against_gold,
)
from .fnirs import (
    compute_fnirs_quality,
    fnirs_cv_dc,
    fnirs_hrf_snr,
    fnirs_motion_artifact_correlation,
    fnirs_short_channel_correlation,
    fnirs_snr_db,
)
from .ppg import (
    compute_ppg_quality,
    compute_ppg_sqi,
    perfusion_index,
    ppg_motion_artifact_probability,
)

__all__ = [
    "alpha_band_power_ratio",
    "compute_ecg_quality",
    "compute_eeg_quality",
    "compute_fnirs_quality",
    "compute_hrv_metrics",
    "compute_ppg_quality",
    "compute_ppg_sqi",
    "compute_snr",
    "detect_r_peaks_neurokit",
    "fnirs_cv_dc",
    "fnirs_hrf_snr",
    "fnirs_motion_artifact_correlation",
    "fnirs_short_channel_correlation",
    "fnirs_snr_db",
    "perfusion_index",
    "ppg_motion_artifact_probability",
    "r_peak_detection_quality",
    "rmssd_mae",
    "spectral_flatness",
    "SignalQualityMetrics",
    "QualityThresholds",
    "Tier",
]
