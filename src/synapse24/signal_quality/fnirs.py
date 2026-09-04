"""fNIRS signal quality metrics including CV of DC, SNR, motion artifact, and short-channel correlation."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt
from scipy.signal import butter, filtfilt, welch
from scipy.stats import pearsonr


def fnirs_cv_dc(long_channel: npt.NDArray[np.float64], short_channel: npt.NDArray[np.float64] | None = None) -> float:
    """Compute Coefficient of Variation of DC component.

    CV = std(DC) / mean(DC) per channel.
    Low CV indicates stable coupling; high CV indicates poor contact or motion.

    Args:
        long_channel: Long-separation channel data (deep tissue)
        short_channel: Optional short-separation channel (superficial)

    Returns:
        CV of DC component [0, 1+]
    """
    # Low-pass filter to get DC component (< 0.01 Hz)
    # For simplicity, use moving average window
    window = max(100, len(long_channel) // 100)
    dc = np.convolve(long_channel, np.ones(window) / window, mode="valid")

    if len(dc) == 0 or np.mean(dc) == 0:
        return 1.0

    cv = np.std(dc) / np.abs(np.mean(dc))
    return float(cv)


def fnirs_snr_db(long_channel: npt.NDArray[np.float64], sampling_rate: int) -> float:
    """Compute SNR in dB for fNIRS AC component.

    SNR = 20 * log10(AC_rms / DC_mean)
    AC = bandpass filtered (0.01-0.5 Hz for hemodynamic)
    DC = low-pass filtered (< 0.01 Hz)

    Args:
        long_channel: Long-separation channel data
        sampling_rate: Sampling rate in Hz

    Returns:
        SNR in dB
    """
    if len(long_channel) < sampling_rate:
        return 0.0

    # Bandpass for hemodynamic signal (0.01-0.5 Hz)
    nyquist = 0.5 * sampling_rate
    low = 0.01 / nyquist
    high = min(0.5 / nyquist, 0.99)

    if high <= low:
        return 0.0

    b, a = butter(4, [low, high], btype="band")
    ac = filtfilt(b, a, long_channel)

    # DC component
    b_dc, a_dc = butter(4, 0.01 / nyquist, btype="low")
    dc = filtfilt(b_dc, a_dc, long_channel)

    ac_rms = np.sqrt(np.mean(ac**2))
    dc_mean = np.mean(np.abs(dc))

    if dc_mean == 0:
        return 0.0

    return float(20 * np.log10(ac_rms / dc_mean))


def fnirs_motion_artifact_correlation(
    long_channel: npt.NDArray[np.float64],
    accel_magnitude: npt.NDArray[np.float64],
    sampling_rate: int,
) -> float:
    """Compute correlation between fNIRS and accelerometer (motion artifact indicator).

    High correlation indicates motion contamination.

    Args:
        long_channel: Long-separation fNIRS channel
        accel_magnitude: Accelerometer magnitude (resampled to match fNIRS)
        sampling_rate: Sampling rate in Hz

    Returns:
        Absolute correlation coefficient [0, 1]
    """
    if len(long_channel) != len(accel_magnitude):
        # Resample accel to match fNIRS
        from scipy.signal import resample

        accel_magnitude = resample(accel_magnitude, len(long_channel))

    # Bandpass both to hemodynamic band for fair comparison
    nyquist = 0.5 * sampling_rate
    low = 0.01 / nyquist
    high = min(0.5 / nyquist, 0.99)

    if high > low:
        b, a = butter(4, [low, high], btype="band")
        long_filt = filtfilt(b, a, long_channel)
        accel_filt = filtfilt(b, a, accel_magnitude)
    else:
        long_filt = long_channel
        accel_filt = accel_magnitude

    # Normalize
    long_norm = (long_filt - np.mean(long_filt)) / (np.std(long_filt) + 1e-10)
    accel_norm = (accel_filt - np.mean(accel_filt)) / (np.std(accel_filt) + 1e-10)

    try:
        corr, _ = pearsonr(long_norm, accel_norm)
        return float(np.abs(corr)) if not np.isnan(corr) else 0.0
    except Exception:
        return 0.0


def fnirs_short_channel_correlation(
    long_channel: npt.NDArray[np.float64],
    short_channel: npt.NDArray[np.float64],
) -> float:
    """Compute correlation between long and short separation channels.

    High correlation indicates superficial contamination (scalp blood flow).
    Used for short-channel regression to remove systemic artifacts.

    Args:
        long_channel: Long-separation channel (deep + superficial)
        short_channel: Short-separation channel (superficial only)

    Returns:
        Correlation coefficient [0, 1]
    """
    if len(long_channel) != len(short_channel):
        from scipy.signal import resample

        short_channel = resample(short_channel, len(long_channel))

    # Use bandpass filtered signals
    # For correlation, we want the hemodynamic band
    # Simple approach: correlate the raw signals after detrending
    long_detrended = long_channel - np.mean(long_channel)
    short_detrended = short_channel - np.mean(short_channel)

    long_norm = long_detrended / (np.std(long_detrended) + 1e-10)
    short_norm = short_detrended / (np.std(short_detrended) + 1e-10)

    try:
        corr, _ = pearsonr(long_norm, short_norm)
        return float(np.abs(corr)) if not np.isnan(corr) else 0.0
    except Exception:
        return 0.0


def compute_fnirs_quality(
    long_channels: npt.NDArray[np.float64],  # (n_channels, n_samples)
    short_channels: npt.NDArray[np.float64] | None = None,  # (n_short, n_samples)
    accel_magnitude: npt.NDArray[np.float64] | None = None,
    sampling_rate: int = 10,
    thresholds: object | None = None,
) -> dict[str, Any]:
    """Comprehensive fNIRS quality assessment.

    Args:
        long_channels: Long-separation channels (n_channels, n_samples)
        short_channels: Optional short-separation channels
        accel_magnitude: Optional accelerometer for motion artifact
        sampling_rate: Sampling rate in Hz
        thresholds: Quality thresholds

    Returns:
        Dictionary with fNIRS quality metrics
    """
    n_channels = long_channels.shape[0]
    channel_qualities = []

    for ch in range(n_channels):
        long_ch = long_channels[ch]

        # CV of DC
        cv_dc = fnirs_cv_dc(long_ch)

        # SNR
        snr = fnirs_snr_db(long_ch, sampling_rate)

        # Motion artifact
        motion_corr = 0.0
        if accel_magnitude is not None:
            motion_corr = fnirs_motion_artifact_correlation(long_ch, accel_magnitude, sampling_rate)

        # Short-channel correlation
        short_corr = 0.0
        if short_channels is not None and ch < short_channels.shape[0]:
            short_corr = fnirs_short_channel_correlation(long_ch, short_channels[ch])

        channel_qualities.append(
            {
                "channel": ch,
                "cv_dc": cv_dc,
                "snr_db": snr,
                "motion_corr": motion_corr,
                "short_ch_corr": short_corr,
            }
        )

    # Aggregate
    avg_cv_dc = np.mean([q["cv_dc"] for q in channel_qualities])
    avg_snr = np.mean([q["snr_db"] for q in channel_qualities])
    avg_motion_corr = np.mean([q["motion_corr"] for q in channel_qualities])
    avg_short_corr = np.mean([q["short_ch_corr"] for q in channel_qualities])

    return {
        "fnirs_cv_dc": float(avg_cv_dc),
        "fnirs_snr_db": float(avg_snr),
        "fnirs_motion_corr": float(avg_motion_corr),
        "fnirs_short_ch_corr": float(avg_short_corr),
        "per_channel": channel_qualities,
    }


def fnirs_hrf_snr(
    long_channel: npt.NDArray[np.float64],
    sampling_rate: int,
    hrf_band: tuple[float, float] = (0.01, 0.1),
) -> float:
    """Compute HRF-specific SNR using expected hemodynamic response band.

    Args:
        long_channel: Long-separation channel
        sampling_rate: Sampling rate
        hrf_band: Expected HRF frequency band

    Returns:
        SNR in dB for HRF band
    """
    freqs, psd = welch(long_channel, fs=sampling_rate, nperseg=min(1024, len(long_channel)))

    hrf_mask = (freqs >= hrf_band[0]) & (freqs <= hrf_band[1])
    noise_mask = (freqs > hrf_band[1]) & (freqs < 0.5)

    hrf_power = np.sum(psd[hrf_mask])
    noise_power = np.sum(psd[noise_mask])

    if noise_power == 0:
        return float("inf")

    return float(10 * np.log10(hrf_power / noise_power))
