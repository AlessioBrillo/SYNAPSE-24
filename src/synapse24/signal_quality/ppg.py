"""PPG signal quality metrics including SQI, perfusion index, and motion artifact probability."""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, filtfilt, find_peaks, welch
from scipy.stats import kurtosis


def perfusion_index(ppg_signal: np.ndarray) -> float:
    """Compute Perfusion Index (PI) = (AC / DC) * 100.

    AC = pulsatile component amplitude
    DC = non-pulsatile baseline

    Args:
        ppg_signal: PPG signal (1D array)

    Returns:
        Perfusion index as percentage
    """
    if len(ppg_signal) == 0:
        return 0.0

    dc = np.mean(ppg_signal)
    ac = np.max(ppg_signal) - np.min(ppg_signal)

    if dc == 0:
        return 0.0

    return (ac / dc) * 100


def _bandpass_filter(
    signal: np.ndarray, lowcut: float, highcut: float, fs: int, order: int = 4
) -> np.ndarray:
    """Apply zero-phase bandpass filter."""
    nyquist = 0.5 * fs
    low = lowcut / nyquist
    high = highcut / nyquist
    b, a = butter(order, [low, high], btype="band")
    return filtfilt(b, a, signal)


def _compute_spectral_entropy(signal: np.ndarray, fs: int) -> float:
    """Compute normalized spectral entropy."""
    freqs, psd = welch(signal, fs=fs, nperseg=min(256, len(signal)))
    psd_norm = psd / np.sum(psd)
    # Avoid log(0)
    psd_norm = psd_norm[psd_norm > 0]
    spec_entropy = -np.sum(psd_norm * np.log2(psd_norm))
    max_entropy = np.log2(len(psd_norm))
    return spec_entropy / max_entropy if max_entropy > 0 else 0


def compute_ppg_sqi(ppg_signal: np.ndarray, sampling_rate: int) -> float:
    """Compute PPG Signal Quality Index (SQI) using multi-feature approach.

    Combines:
    - Perfusion index (normalized)
    - Spectral entropy (lower = more periodic = better)
    - Kurtosis (physiological PPG has characteristic shape)
    - Peak-to-peak regularity

    Args:
        ppg_signal: Raw PPG signal
        sampling_rate: Sampling rate in Hz

    Returns:
        SQI score [0, 1] where 1 = highest quality
    """
    if len(ppg_signal) < sampling_rate:
        return 0.0

    # Bandpass filter for cardiac component (0.5-8 Hz)
    filtered = _bandpass_filter(ppg_signal, 0.5, 8.0, sampling_rate)

    # Feature 1: Perfusion index (normalized to [0, 1])
    pi = perfusion_index(filtered)
    pi_score = min(pi / 5.0, 1.0)  # PI > 5% is excellent

    # Feature 2: Spectral entropy (inverted: lower entropy = more periodic = better)
    spec_ent = _compute_spectral_entropy(filtered, sampling_rate)
    entropy_score = 1.0 - spec_ent

    # Feature 3: Kurtosis (physiological PPG has positive kurtosis ~2-4)
    sig_kurtosis = kurtosis(filtered)
    kurtosis_score = 1.0 - min(abs(sig_kurtosis - 3.0) / 3.0, 1.0)

    # Feature 4: Peak regularity (using autocorrelation at heart rate lag)
    # Estimate heart rate from peak detection
    peaks, _ = find_peaks(filtered, distance=int(sampling_rate * 0.4))
    if len(peaks) > 2:
        rr_intervals = np.diff(peaks) / sampling_rate
        rr_cv = np.std(rr_intervals) / np.mean(rr_intervals) if np.mean(rr_intervals) > 0 else 1
        regularity_score = 1.0 - min(rr_cv * 2, 1.0)
    else:
        regularity_score = 0.0

    # Weighted combination (weights from literature)
    sqi = (
        0.3 * pi_score
        + 0.25 * entropy_score
        + 0.2 * kurtosis_score
        + 0.25 * regularity_score
    )

    return float(np.clip(sqi, 0.0, 1.0))


def ppg_motion_artifact_probability(
    ppg_signal: np.ndarray,
    accel_magnitude: np.ndarray | None = None,
    sampling_rate: int = 100,
) -> float:
    """Compute Motion Artifact Probability (MAP) for PPG.

    Uses:
    - Accelerometer magnitude (if available)
    - Spectral analysis of PPG (motion artifacts have broad spectrum)
    - Correlation between PPG and accelerometer (if available)

    Args:
        ppg_signal: Raw PPG signal
        accel_magnitude: Optional 3-axis accelerometer magnitude
        sampling_rate: Sampling rate in Hz

    Returns:
        Probability [0, 1] of motion artifact contamination
    """
    if len(ppg_signal) < sampling_rate:
        return 1.0

    # Bandpass filter
    filtered = _bandpass_filter(ppg_signal, 0.5, 8.0, sampling_rate)

    # Feature 1: Spectral flatness (motion = flatter spectrum)
    freqs, psd = welch(filtered, fs=sampling_rate, nperseg=min(256, len(filtered)))
    psd_norm = psd / np.sum(psd)
    geometric_mean = np.exp(np.mean(np.log(psd_norm + 1e-10)))
    arithmetic_mean = np.mean(psd_norm)
    spectral_flatness = geometric_mean / arithmetic_mean if arithmetic_mean > 0 else 1.0

    # Feature 2: High-frequency energy ratio (motion = more HF energy)
    hf_mask = freqs > 5.0
    hf_energy = np.sum(psd[hf_mask])
    total_energy = np.sum(psd)
    hf_ratio = hf_energy / total_energy if total_energy > 0 else 0.5

    # Feature 3: Accelerometer correlation (if available)
    accel_score = 0.0
    if accel_magnitude is not None and len(accel_magnitude) == len(ppg_signal):
        # Cross-correlation at zero lag
        ppg_norm = (filtered - np.mean(filtered)) / (np.std(filtered) + 1e-10)
        accel_norm = (accel_magnitude - np.mean(accel_magnitude)) / (
            np.std(accel_magnitude) + 1e-10
        )
        correlation = np.abs(np.corrcoef(ppg_norm, accel_norm)[0, 1])
        accel_score = correlation if not np.isnan(correlation) else 0.0

    # Combine features
    # Spectral flatness: motion increases flatness
    # HF ratio: motion increases HF energy
    # Accel correlation: motion increases correlation
    map_score = (
        0.4 * spectral_flatness
        + 0.3 * min(hf_ratio * 3, 1.0)
        + 0.3 * accel_score
    )

    return float(np.clip(map_score, 0.0, 1.0))


def compute_ppg_quality(
    ppg_signal: np.ndarray,
    sampling_rate: int,
    accel_magnitude: np.ndarray | None = None,
    thresholds=None,
) -> dict:
    """Comprehensive PPG quality assessment.

    Args:
        ppg_signal: Raw PPG signal
        sampling_rate: Sampling rate in Hz
        accel_magnitude: Optional accelerometer magnitude for MAP
        thresholds: Quality thresholds

    Returns:
        Dictionary with PPG quality metrics
    """
    sqi = compute_ppg_sqi(ppg_signal, sampling_rate)
    pi = perfusion_index(ppg_signal)
    map_score = ppg_motion_artifact_probability(ppg_signal, accel_magnitude, sampling_rate)

    return {
        "ppg_sqi": sqi,
        "perfusion_index": pi,
        "motion_artifact_prob": map_score,
    }
