"""EEG signal quality metrics including spectral flatness and alpha band analysis."""

from __future__ import annotations

import numpy as np
from scipy.signal import welch
from scipy.stats import entropy


def spectral_flatness(eeg_signal: np.ndarray, sampling_rate: int) -> float:
    """Compute spectral flatness (Wiener entropy) of EEG signal.

    Low flatness = tonal/peaked spectrum (good EEG with clear rhythms)
    High flatness = flat/noisy spectrum (poor quality, muscle artifact, disconnection)

    Args:
        eeg_signal: EEG signal (1D array)
        sampling_rate: Sampling rate in Hz

    Returns:
        Spectral flatness [0, 1] where 0 = purely tonal, 1 = white noise
    """
    if len(eeg_signal) < sampling_rate:
        return 1.0

    freqs, psd = welch(eeg_signal, fs=sampling_rate, nperseg=min(1024, len(eeg_signal)))

    # Restrict to physiological EEG range (0.5-45 Hz)
    mask = (freqs >= 0.5) & (freqs <= 45.0)
    psd_band = psd[mask]

    if len(psd_band) == 0:
        return 1.0

    # Avoid log(0)
    psd_safe = psd_band + 1e-10
    geometric_mean = np.exp(np.mean(np.log(psd_safe)))
    arithmetic_mean = np.mean(psd_safe)

    if arithmetic_mean == 0:
        return 1.0

    return float(geometric_mean / arithmetic_mean)


def alpha_band_power_ratio(
    eeg_signal: np.ndarray, sampling_rate: int
) -> float:
    """Compute alpha band power ratio (alpha / total power).

    For eyes-closed resting state, alpha (8-13 Hz) should dominate.
    Ratio > 1.5 indicates good quality alpha rhythm.
    Ratio < 1.0 suggests poor signal or eyes-open/drowsy state.

    Args:
        eeg_signal: EEG signal (1D array)
        sampling_rate: Sampling rate in Hz

    Returns:
        Alpha band power ratio
    """
    if len(eeg_signal) < sampling_rate:
        return 0.0

    freqs, psd = welch(eeg_signal, fs=sampling_rate, nperseg=min(1024, len(eeg_signal)))

    alpha_mask = (freqs >= 8.0) & (freqs <= 13.0)
    total_mask = (freqs >= 0.5) & (freqs <= 45.0)

    alpha_power = np.sum(psd[alpha_mask])
    total_power = np.sum(psd[total_mask])

    if total_power == 0:
        return 0.0

    return float(alpha_power / total_power)


def band_power(
    eeg_signal: np.ndarray,
    sampling_rate: int,
    band: tuple[float, float],
) -> float:
    """Compute power in a specific frequency band.

    Args:
        eeg_signal: EEG signal
        sampling_rate: Sampling rate in Hz
        band: (low_freq, high_freq) tuple

    Returns:
        Band power (µV²)
    """
    freqs, psd = welch(eeg_signal, fs=sampling_rate, nperseg=min(1024, len(eeg_signal)))
    mask = (freqs >= band[0]) & (freqs <= band[1])
    return float(np.sum(psd[mask]))


def compute_eeg_quality(
    eeg_signal: np.ndarray,
    sampling_rate: int,
    state: str = "resting_eyes_closed",
) -> dict:
    """Comprehensive EEG quality assessment.

    Args:
        eeg_signal: Raw EEG signal
        sampling_rate: Sampling rate in Hz
        state: Recording state ("resting_eyes_closed", "resting_eyes_open", "task")

    Returns:
        Dictionary with EEG quality metrics
    """
    sf = spectral_flatness(eeg_signal, sampling_rate)
    alpha_ratio = alpha_band_power_ratio(eeg_signal, sampling_rate)

    # Band powers
    bands = {
        "delta": (0.5, 4.0),
        "theta": (4.0, 8.0),
        "alpha": (8.0, 13.0),
        "beta": (13.0, 30.0),
        "gamma": (30.0, 45.0),
    }
    band_powers = {name: band_power(eeg_signal, sampling_rate, b) for name, b in bands.items()}

    # Expected ratios for different states
    expected_ratios = {
        "resting_eyes_closed": {"alpha_ratio_min": 0.3, "flatness_max": 0.3},
        "resting_eyes_open": {"alpha_ratio_min": 0.15, "flatness_max": 0.4},
        "task": {"alpha_ratio_min": 0.05, "flatness_max": 0.5},
    }
    expectations = expected_ratios.get(state, expected_ratios["resting_eyes_closed"])

    return {
        "spectral_flatness": sf,
        "alpha_band_ratio": alpha_ratio,
        "band_powers": band_powers,
        "quality_pass": (
            sf <= expectations["flatness_max"]
            and alpha_ratio >= expectations["alpha_ratio_min"]
        ),
        "state": state,
    }