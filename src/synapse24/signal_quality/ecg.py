"""ECG signal quality metrics and R-peak detection."""

from __future__ import annotations

from typing import Any

import neurokit2 as nk
import numpy as np
import numpy.typing as npt

from .base import QualityThresholds, SignalQualityMetrics

MIN_LF_HF_WINDOW_S = 120.0
"""Minimum analysed window duration for an interpretable LF/HF ratio (seconds).

The LF band (0.04-0.15 Hz) has a period of 7-25 s, so a 30 s window resolves
barely one LF cycle while the HF band (0.15-0.4 Hz) gets a couple. An LF/HF
ratio computed from such a window is not methodologically interpretable: it is
dominated by the spectral interpolation of the estimator, not by autonomic
balance. Below this duration the ratio is therefore *not reported at all*
rather than reported with a caveat.
"""


def detect_r_peaks_neurokit(
    ecg_signal: npt.NDArray[np.float64], sampling_rate: int
) -> npt.NDArray[np.int64]:
    """Detect R-peaks using NeuroKit2's ECG processing pipeline.

    Args:
        ecg_signal: Raw ECG signal (1D array)
        sampling_rate: Sampling rate in Hz

    Returns:
        Array of R-peak indices
    """
    _, rpeaks = nk.ecg_peaks(ecg_signal, sampling_rate=sampling_rate)
    return np.asarray(rpeaks["ECG_R_Peaks"])


def r_peak_detection_quality(
    detected_peaks: npt.NDArray[np.int64],
    reference_peaks: npt.NDArray[np.int64],
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
    r_peaks: npt.NDArray[np.int64],
    sampling_rate: int,
    window_duration_s: float | None = None,
) -> dict[str, Any]:
    """Compute time and frequency domain HRV metrics from R-peaks.

    LF/HF gating contract
    ----------------------
    The frequency-domain LF/HF ratio is only methodologically interpretable
    when the analysed window is at least ``MIN_LF_HF_WINDOW_S`` (120 s) long.
    The behaviour therefore depends on ``window_duration_s``:

    - duration known and ``>= MIN_LF_HF_WINDOW_S``: ``lf_hf_ratio`` is
      reported and ``lf_hf_status`` is ``"ok"``.
    - duration known and ``< MIN_LF_HF_WINDOW_S``: ``lf_hf_ratio`` is
      **omitted entirely** (the key is absent, not None) and
      ``lf_hf_status`` is ``"window_too_short"``. ``window_duration_s``
      reports the analysed window length.
    - duration unknown (``None``): the ratio is computed as before and
      ``lf_hf_status`` is ``"unknown_window"``.

    Downstream consumers must therefore use ``hrv.get("lf_hf_ratio", 0)``
    and must never assume the key exists.

    Note:
        Only the derived ratio is withheld. ``lf_power`` / ``hf_power`` (the
        raw band powers) are still reported when the frequency-domain
        estimator succeeds, so a short window is visibly flagged through
        ``lf_hf_status`` instead of silently producing an undefined ratio.

    Args:
        r_peaks: Array of R-peak indices
        sampling_rate: Sampling rate in Hz
        window_duration_s: Duration of the analysed window in seconds, or
            None when it is unknown (legacy behaviour: LF/HF is computed)

    Returns:
        Dictionary of HRV metrics. Always contains ``lf_hf_status`` when
        at least two R-peaks were available; contains ``lf_hf_ratio`` only
        when the window is long enough (or its duration is unknown).
    """
    if len(r_peaks) < 2:
        return {}

    # RR intervals in seconds
    rr_intervals = np.diff(r_peaks) / sampling_rate
    rr_ms = rr_intervals * 1000

    # Time domain
    hrv: dict[str, Any] = {
        "mean_rr_ms": float(np.mean(rr_ms)),
        "sdnn_ms": float(np.std(rr_ms, ddof=1)),
        "rmssd_ms": float(np.sqrt(np.mean(np.diff(rr_ms) ** 2))),
        "nn50": int(np.sum(np.abs(np.diff(rr_ms)) > 50)),
        "pnn50": float(np.sum(np.abs(np.diff(rr_ms)) > 50) / len(rr_ms) * 100),
        "hr_mean_bpm": float(60000 / np.mean(rr_ms)),
    }

    duration_known = window_duration_s is not None
    window_too_short = (
        window_duration_s is not None and float(window_duration_s) < MIN_LF_HF_WINDOW_S
    )
    if window_duration_s is not None:
        hrv["window_duration_s"] = float(window_duration_s)

    # Frequency domain (using NeuroKit2)
    try:
        hrv_freq = nk.hrv_frequency(r_peaks, sampling_rate=sampling_rate, show=False)
        hrv.update(
            {
                "lf_power": float(hrv_freq["HRV_LF"].iloc[0]),
                "hf_power": float(hrv_freq["HRV_HF"].iloc[0]),
            }
        )
        if not window_too_short:
            hrv["lf_hf_ratio"] = float(hrv_freq["HRV_LFHF"].iloc[0])
    except Exception:
        pass

    if window_too_short:
        hrv["lf_hf_status"] = "window_too_short"
    elif duration_known:
        hrv["lf_hf_status"] = "ok"
    else:
        hrv["lf_hf_status"] = "unknown_window"

    return hrv


def rmssd_mae(
    detected_peaks: npt.NDArray[np.int64],
    reference_peaks: npt.NDArray[np.int64],
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

    rmssd_detected = float(hrv_detected.get("rmssd_ms", 0))
    rmssd_reference = float(hrv_reference.get("rmssd_ms", 0))

    return abs(rmssd_detected - rmssd_reference)


def compute_ecg_quality(
    ecg_signal: npt.NDArray[np.float64],
    sampling_rate: int,
    reference_peaks: npt.NDArray[np.int64] | None = None,
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
        sensitivity, ppv = r_peak_detection_quality(detected_peaks, reference_peaks, sampling_rate)
        metrics.r_peak_sensitivity = sensitivity
        metrics.r_peak_ppv = ppv
        metrics.rmssd_mae_ms = rmssd_mae(detected_peaks, reference_peaks, sampling_rate)

    return metrics
