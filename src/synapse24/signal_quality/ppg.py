"""PPG signal quality metrics including SQI, perfusion index, and motion artifact probability."""

from __future__ import annotations

import numpy as np
import numpy.typing as npt
from scipy.signal import butter, filtfilt, find_peaks, welch

CARDIAC_BAND_LOW_HZ = 0.5
"""Lower edge of the physiological PPG band (pulse fundamental + harmonics)."""

CARDIAC_BAND_HIGH_HZ = 8.0
"""Upper edge of the physiological PPG band (pulse fundamental + harmonics)."""


def perfusion_index(ppg_signal: npt.NDArray[np.float64]) -> float:
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

    return float((ac / dc) * 100)


def _bandpass_filter(
    signal: npt.NDArray[np.float64], lowcut: float, highcut: float, fs: int, order: int = 4
) -> npt.NDArray[np.float64]:
    """Apply zero-phase bandpass filter."""
    nyquist = 0.5 * fs
    low = lowcut / nyquist
    high = highcut / nyquist
    b, a = butter(order, [low, high], btype="band")
    return np.asarray(filtfilt(b, a, signal))


def _compute_spectral_entropy(signal: npt.NDArray[np.float64], fs: int) -> float:
    """Compute normalized spectral entropy."""
    freqs, psd = welch(signal, fs=fs, nperseg=min(256, len(signal)))
    psd_norm = psd / np.sum(psd)
    # Avoid log(0)
    psd_norm = psd_norm[psd_norm > 0]
    spec_entropy = -np.sum(psd_norm * np.log2(psd_norm))
    max_entropy = np.log2(len(psd_norm))
    return spec_entropy / max_entropy if max_entropy > 0 else 0


def _compute_spectral_snr_score(signal: npt.NDArray[np.float64], fs: int) -> float:
    """Compute the spectral SNR feature: fraction of power inside the cardiac band.

    The signal is mean-removed (DC carries no quality information) and a Welch
    periodogram is computed. The returned score is

        in-band power / (in-band power + out-of-band power)

    which lies in [0, 1] by construction: 1.0 means *all* of the signal power
    sits inside the cardiac band (0.5-8 Hz), 0.0 means none of it does.

    A clean pulse train concentrates its power in the cardiac band, so the
    score approaches 1. White noise spreads its power over the whole
    Nyquist range, so the score approaches the cardiac-band width fraction
    (e.g. ~0.15 at 100 Hz) and is therefore clearly separated from a clean
    trace.

    Note:
        The feature is sample-rate dependent by construction: at lower
        sampling rates the cardiac band covers a larger share of the
        spectrum, so the noise floor of this feature rises accordingly.
        It is used as a *relative* discriminator between clean and noisy
        traces of the same modality, not as an absolute power figure.

    Args:
        signal: Raw signal (mean removal is applied internally)
        fs: Sampling rate in Hz

    Returns:
        Spectral SNR score in [0, 1]
    """
    if fs <= 0 or len(signal) < 2:
        return 0.0

    x = np.asarray(signal, dtype=np.float64)
    x = x - np.mean(x)
    if not np.any(np.abs(x) > 0):
        return 0.0

    freqs, psd = welch(x, fs=fs, nperseg=min(256, len(x)))
    total_power = float(np.sum(psd))
    if total_power <= 0:
        return 0.0

    nyquist = 0.5 * fs
    high = min(CARDIAC_BAND_HIGH_HZ, nyquist * 0.95)
    low = min(CARDIAC_BAND_LOW_HZ, high * 0.5)
    if high <= low:
        return 0.0

    in_band = float(np.sum(psd[(freqs >= low) & (freqs <= high)]))
    out_band = float(np.sum(psd[(freqs < low) | (freqs > high)]))
    denominator = in_band + out_band
    if denominator <= 0:
        return 0.0

    return float(np.clip(in_band / denominator, 0.0, 1.0))


def compute_ppg_sqi(ppg_signal: npt.NDArray[np.float64], sampling_rate: int) -> float:
    """Compute PPG Signal Quality Index (SQI) using a multi-feature approach.

    Combines four normalised features (weights sum to 1.0):
    - Perfusion index (normalized) - weight 0.25
    - Spectral entropy (inverted: lower entropy = more periodic = better) - weight 0.25
    - Spectral SNR (fraction of power inside the cardiac band) - weight 0.30
    - Peak-to-peak (RR) regularity - weight 0.20

    Why kurtosis was removed
    -----------------------
    The previous implementation scored ``1.0 - |k - 3| / 3`` on the kurtosis
    of the band-passed trace. That feature rewards the *absence* of a signal:

    * The score peaks at k = 3, which is the kurtosis of a **Gaussian**.
      Gaussian noise is the worst possible input for a quality index, so a
      noise-only trace scored 1.0 on this feature - the exact opposite of
      what an SQI must do.
    * Physiological PPG is not Gaussian. A pulse train is sharply peaked
      (kurtosis > 3), so clean pulses were *penalised* while noise was
      rewarded - the feature was inverted with respect to signal quality.
    * (Implementation detail that hid the bug: ``scipy.stats.kurtosis``
      returns the *excess* kurtosis (Gaussian = 0) while the score compares
      it against 3, so on the previous code the feature collapsed to ~0 for
      every ordinary signal. It was dead weight and a latent reward for
      noise the moment someone "fixed" the convention.)

    The replacement is a spectral SNR feature: the fraction of signal power
    that lies inside the cardiac band (0.5-8 Hz). A clean pulse train puts
    nearly all of its power there, while white noise spreads it over the
    whole Nyquist range, so the feature separates signal from noise instead
    of rewarding noise. It carries the largest weight (0.30) because it is
    the feature that actually discriminates a pulse signal from noise; the
    perfusion-index weight drops from 0.30 to 0.25 because the normalised PI
    saturates at 1.0 for every non-degenerate trace (its "DC" is a band-pass
    filter artefact, not a physiological baseline), so it carries little
    discriminative information.

    Args:
        ppg_signal: Raw PPG signal
        sampling_rate: Sampling rate in Hz

    Returns:
        SQI score [0, 1] where 1 = highest quality
    """
    if len(ppg_signal) < sampling_rate:
        return 0.0

    # A constant trace carries no pulse signal at all.
    if float(np.ptp(ppg_signal)) <= 0.0:
        return 0.0

    # Bandpass filter for cardiac component (0.5-8 Hz)
    filtered = _bandpass_filter(ppg_signal, 0.5, 8.0, sampling_rate)

    # Feature 1: Perfusion index (normalized to [0, 1]).
    # Clamped: the "DC" of a band-passed trace is a filter artefact whose sign
    # is arbitrary, and an unclamped negative value would drive the whole SQI
    # to zero for an otherwise clean trace.
    pi = perfusion_index(filtered)
    pi_score = float(np.clip(pi / 5.0, 0.0, 1.0))  # PI > 5% is excellent

    # Feature 2: Spectral entropy (inverted: lower entropy = more periodic = better)
    spec_ent = _compute_spectral_entropy(filtered, sampling_rate)
    entropy_score = 1.0 - spec_ent

    # Feature 3: Spectral SNR (fraction of power inside the cardiac band).
    # Replaces the kurtosis feature - see the docstring for the rationale.
    snr_score = _compute_spectral_snr_score(ppg_signal, sampling_rate)

    # Feature 4: Peak regularity (using autocorrelation at heart rate lag)
    # Estimate heart rate from peak detection
    peaks, _ = find_peaks(filtered, distance=int(sampling_rate * 0.4))
    if len(peaks) > 2:
        rr_intervals = np.diff(peaks) / sampling_rate
        rr_cv = np.std(rr_intervals) / np.mean(rr_intervals) if np.mean(rr_intervals) > 0 else 1
        regularity_score = 1.0 - min(rr_cv * 2, 1.0)
    else:
        regularity_score = 0.0

    # Weighted combination (weights sum to 1.0)
    sqi = 0.25 * pi_score + 0.25 * entropy_score + 0.3 * snr_score + 0.2 * regularity_score

    return float(np.clip(sqi, 0.0, 1.0))


def _align_accel_to_ppg(
    accel_magnitude: npt.NDArray[np.float64] | None,
    n_ppg: int,
    sampling_rate: int,
) -> npt.NDArray[np.float64] | None:
    """Align the accelerometer magnitude onto the PPG time base.

    The accelerometer and the PPG are assumed to share the same nominal
    sample rate; only the number of buffered samples may differ (different
    window edges, different buffer trimming, a stream that started later).
    The accelerometer time base is therefore rebuilt as ``i / sampling_rate``
    and linearly interpolated (``numpy.interp``) onto the PPG time base
    ``j / sampling_rate``. Values outside the accelerometer time span are
    clamped to the nearest accelerometer sample, which is the standard
    zero-order-hold behaviour at the window edges.

    Callers whose accelerometer runs at a *different* rate must resample it
    themselves (as the WESAD ingestion does) and pass the aligned trace.

    Args:
        accel_magnitude: Accelerometer magnitude trace
        n_ppg: Number of PPG samples to align onto
        sampling_rate: Nominal sample rate shared by both modalities (Hz)

    Returns:
        Aligned accelerometer magnitude, or None when the accelerometer
        carries no usable motion information (absent, fewer than two
        samples, or constant/degenerate).
    """
    if accel_magnitude is None:
        return None

    accel = np.asarray(accel_magnitude, dtype=np.float64).ravel()
    if accel.size < 2 or n_ppg < 1 or sampling_rate <= 0:
        return None

    # A constant magnitude carries no motion information at all.
    if np.allclose(accel, accel[0]):
        return None

    if accel.size != n_ppg:
        accel_time = np.arange(accel.size, dtype=np.float64) / sampling_rate
        ppg_time = np.arange(n_ppg, dtype=np.float64) / sampling_rate
        accel = np.interp(ppg_time, accel_time, accel)

    # Interpolation can still collapse to a constant (e.g. a single step).
    if not np.any(np.abs(accel - np.mean(accel)) > 0):
        return None

    return accel


def ppg_motion_artifact_probability(
    ppg_signal: npt.NDArray[np.float64],
    accel_magnitude: npt.NDArray[np.float64] | None = None,
    sampling_rate: int = 100,
) -> float:
    """Compute Motion Artifact Probability (MAP) for PPG.

    Uses:
    - Accelerometer magnitude (if available)
    - Spectral analysis of PPG (motion artifacts have broad spectrum)
    - Correlation between PPG and accelerometer (if available)

    The accelerometer is *not* discarded when its length differs from the
    PPG length: it is resampled onto the PPG time base with linear
    interpolation, assuming the accelerometer and the PPG share the same
    nominal sample rate (see ``_align_accel_to_ppg``). The accelerometer
    contribution is dropped (scored 0.0) only when the accelerometer is
    absent, has fewer than two samples, or is constant/degenerate - i.e.
    when it carries no usable motion information.

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

    # Feature 3: Accelerometer correlation (resampled onto the PPG grid)
    accel_score = 0.0
    accel = _align_accel_to_ppg(accel_magnitude, len(ppg_signal), sampling_rate)
    if accel is not None and np.std(filtered) > 0:
        # Cross-correlation at zero lag
        ppg_norm = (filtered - np.mean(filtered)) / (np.std(filtered) + 1e-10)
        accel_norm = (accel - np.mean(accel)) / (np.std(accel) + 1e-10)
        correlation = np.abs(np.corrcoef(ppg_norm, accel_norm)[0, 1])
        accel_score = correlation if not np.isnan(correlation) else 0.0

    # Combine features
    # Spectral flatness: motion increases flatness
    # HF ratio: motion increases HF energy
    # Accel correlation: motion increases correlation
    map_score = 0.4 * spectral_flatness + 0.3 * min(hf_ratio * 3, 1.0) + 0.3 * accel_score

    return float(np.clip(map_score, 0.0, 1.0))


def compute_ppg_quality(
    ppg_signal: npt.NDArray[np.float64],
    sampling_rate: int,
    accel_magnitude: npt.NDArray[np.float64] | None = None,
    thresholds: object | None = None,
) -> dict[str, float]:
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
