"""ECG signal quality metrics and R-peak detection."""

from __future__ import annotations

import numpy as np
import neurokit2 as nk
from scipy.signal import find_peaks

from .base import SignalQualityMetrics, QualityThresholds


def detect_r_peaks_neurokit(
    ecg_signal: np.ndarray, sampling_rate: int
) -> np.ndarray:
    """Detect R-peaks using NeuroKit2's ECG processing pipeline.

    Args:
        ecg_signal: Raw ECG signal (1D array)
        sampling_rate: Sampling rate in Hz

    Returns:
        Array of R-peak indices
    """
    _, rpeaks = nk.ecg_peaks(ecg_signal, sampling_rate=sampling_rate)
    return rpeaks["ECG_R_Peaks"]


def r_peak_detection_quality(
    detected_peaks: np.ndarray,
    reference_peaks: np.ndarray,
    sampling_rate: int,
    tolerance_ms: int = 50,
) -> tuple[float, float]:
    """Compute sensitivity and positive predictive value for R-peak detection.

    Args:
        detected_peaks: Indices of detected R-peaks
        reference_peaks: Indices of reference/ground-truth R-peaks
        sampling_rate: Sampling rate in Hz
        tolerance_ms: Matching tolerance in milliseconds

    Returns:
        Tuple of (sensitivity, positive_predictive_value)
    """
    if len(detected_peaks) == 0 or len(reference_peaks) == 0:
        return 0.0, 0.0

    tolerance_samples = int(tolerance_ms * sampling_rate / 1000)

    # True positives: detected peaks within tolerance of reference
    tp = 0
    matched_ref = set()
    for d_peak in detected_peaks:
        distances = np.abs(reference_peaks - d_peak)
        min_dist_idx = np.argmin(distances)
        if distances[min_dist_idx] <= tolerance_samples and min_dist_idx not in matched_ref:
            tp += 1
            matched_ref.add(min_dist_idx)

    sensitivity = tp / len(reference_peaks) if len(reference_peaks) > 0 else 0.0
    ppv = tp / len(detected_peaks) if len(detected_peaks) > 0 else 0.0

    return sensitivity, ppv


def compute_hrv_metrics(
    r_peaks: np.ndarray, sampling_rate: int
) -> dict[str, float]:
    """Compute time and frequency domain HRV metrics from R-peaks.

    Args:
        r_peaks: Array of R-peak indices
        sampling_rate: Sampling rate in Hz

    Returns:
        Dictionary of HRV metrics
    """
    if len(r_peaks) < 2:
        return {}

    # RR intervals in seconds
    rr_intervals = np.diff(r_peaks) / sampling_rate
    rr_ms = rr_intervals * 1000

    # Time domain
    hrv = {
        "mean_rr_ms": float(np.mean(rr_ms)),
        "sdnn_ms": float(np.std(rr_ms, ddof=1)),
        "rmssd_ms": float(np.sqrt(np.mean(np.diff(rr_ms) ** 2))),
        "nn50": int(np.sum(np.abs(np.diff(rr_ms)) > 50)),
        "pnn50": float(np.sum(np.abs(np.diff(rr_ms)) > 50) / len(rr_ms) * 100),
        "hr_mean_bpm": float(60000 / np.mean(rr_ms)),
    }

    # Frequency domain (using NeuroKit2)
    try:
        hrv_freq = nk.hrv_frequency(r_peaks, sampling_rate=sampling_rate, show=False)
        hrv.update({
            "lf_power": float(hrv_freq["HRV_LF"].iloc[0]),
            "hf_power": float(hrv_freq["HRV_HF"].iloc[0]),
            "lf_hf_ratio": float(hrv_freq["HRV_LFHF"].iloc[0]),
        })
    except Exception:
        pass

    return hrv


def rmssd_mae(
    detected_peaks: np.ndarray,
    reference_peaks: np.ndarray,
    sampling_rate: int,
) -> float:
    """Compute MAE of RMSSD between detected and reference R-peaks.

    Args:
        detected_peaks: Detected R-peak indices
        reference_peaks: Reference R-peak indices
        sampling_rate: Sampling rate in Hz

    Returns:
        MAE in milliseconds
    """
    hrv_detected = compute_hrv_metrics(detected_peaks, sampling_rate)
    hrv_reference = compute_hrv_metrics(reference_peaks, sampling_rate)

    rmssd_detected = hrv_detected.get("rmssd_ms", 0)
    rmssd_reference = hrv_reference.get("rmssd_ms", 0)

    return abs(rmssd_detected - rmssd_reference)


def compute_ecg_quality(
    ecg_signal: np.ndarray,
    sampling_rate: int,
    reference_peaks: np.ndarray | None = None,
    thresholds: QualityThresholds | None = None,
) -> SignalQualityMetrics:
    """Comprehensive ECG quality assessment.

    Args:
        ecg_signal: Raw ECG signal
        sampling_rate: Sampling rate in Hz
        reference_peaks: Optional ground-truth R-peaks for validation
        thresholds: Quality thresholds

    Returns:
        SignalQualityMetrics with ECG assessments
    """
    if thresholds is None:
        thresholds = QualityThresholds()

    metrics = SignalQualityMetrics(
        sampling_rate_hz=sampling_rate,
        duration_s=len(ecg_signal) / sampling_rate,
        modality="ecg",
        thresholds=thresholds,
    )

    # Detect R-peaks
    detected_peaks = detect_r_peaks_neurokit(ecg_signal, sampling_rate)

    # Compute HRV metrics
    metrics.hrv_metrics = compute_hrv_metrics(detected_peaks, sampling_rate)

    # Validate against reference if provided
    if reference_peaks is not None and len(reference_peaks) > 0:
        sensitivity, ppv = r_peak_detection_quality(
            detected_peaks, reference_peaks, sampling_rate
        )
        metrics.r_peak_sensitivity = sensitivity
        metrics.r_peak_ppv = ppv
        metrics.rmssd_mae_ms = rmssd_mae(detected_peaks, reference_peaks, sampling_rate)

    return metrics