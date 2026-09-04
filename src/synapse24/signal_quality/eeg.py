"""EEG signal quality metrics including spectral flatness, alpha band analysis, and YASA sleep staging validation."""

from __future__ import annotations

from typing import Any

import numpy as np
import numpy.typing as npt
from scipy.signal import welch


def spectral_flatness(eeg_signal: npt.NDArray[np.float64], sampling_rate: int) -> float:
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


def alpha_band_power_ratio(eeg_signal: npt.NDArray[np.float64], sampling_rate: int) -> float:
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
    eeg_signal: npt.NDArray[np.float64],
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
    eeg_signal: npt.NDArray[np.float64],
    sampling_rate: int,
    state: str = "resting_eyes_closed",
) -> dict[str, Any]:
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
            sf <= expectations["flatness_max"] and alpha_ratio >= expectations["alpha_ratio_min"]
        ),
        "state": state,
    }


def compute_yasa_sleep_staging(
    eeg_signal: npt.NDArray[np.float64],
    sampling_rate: int,
    eog_signal: npt.NDArray[np.float64] | None = None,
    emg_signal: npt.NDArray[np.float64] | None = None,
    eeg_ch_name: str = "EEG",
    eog_ch_name: str = "EOG",
    emg_ch_name: str = "EMG",
) -> dict[str, Any]:
    """Run YASA sleep staging on EEG (+ optional EOG/EMG) and return hypnogram.

    Uses MNE Raw object as required by YASA >= 0.7.

    Args:
        eeg_signal: EEG signal (1D array, µV)
        sampling_rate: Sampling rate in Hz
        eog_signal: Optional EOG signal for improved staging
        emg_signal: Optional EMG signal for improved staging
        eeg_ch_name: EEG channel name for MNE Raw
        eog_ch_name: EOG channel name for MNE Raw
        emg_ch_name: EMG channel name for MNE Raw

    Returns:
        Dictionary with:
        - 'hypnogram': Predicted sleep stages (0=W, 1=N1, 2=N2, 3=N3, 4=REM, 5=MOVE, 9=UNK)
        - 'confidence': Per-epoch confidence scores
        - 'stages': Stage labels (strings)
        - 'sampling_rate': Hypnogram sampling rate (1/30 Hz = 30s epochs)
    """
    try:
        import mne  # type: ignore[import-untyped]
        import yasa  # type: ignore[import-untyped]
    except ImportError as e:
        raise RuntimeError(
            f"Required package not installed: {e}. Install with: pip install mne yasa"
        )

    # Build MNE Raw object from numpy arrays
    ch_names = [eeg_ch_name]
    ch_types = ["eeg"]
    data = [eeg_signal.reshape(1, -1)]

    if eog_signal is not None:
        ch_names.append(eog_ch_name)
        ch_types.append("eog")
        data.append(eog_signal.reshape(1, -1))

    if emg_signal is not None:
        ch_names.append(emg_ch_name)
        ch_types.append("emg")
        data.append(emg_signal.reshape(1, -1))

    info = mne.create_info(ch_names=ch_names, sfreq=sampling_rate, ch_types=ch_types)
    raw = mne.io.RawArray(np.vstack(data), info, verbose=False)

    # Run sleep staging
    sls = yasa.SleepStaging(
        raw,
        eeg_name=eeg_ch_name,
        eog_name=eog_ch_name if eog_signal is not None else None,
        emg_name=emg_ch_name if emg_signal is not None else None,
    )
    hypnogram_obj = sls.predict()
    proba = sls.predict_proba()

    # Extract hypnogram and confidence
    hypnogram = hypnogram_obj.hypno  # pandas Series with stage strings
    confidence = proba.max(axis=1).values

    # Map YASA string stages to integer codes (matching Sleep-EDF convention)
    stage_map = {
        "W": 0,
        "N1": 1,
        "N2": 2,
        "N3": 3,
        "REM": 4,
    }
    hypnogram_int = np.array([stage_map.get(s, 9) for s in hypnogram], dtype=np.int64)

    return {
        "hypnogram": hypnogram_int,
        "confidence": confidence,
        "stages": hypnogram.values,
        "sampling_rate": 1 / 30.0,  # 30-second epochs
    }


def compute_yasa_kappa(
    yasa_hypnogram: npt.NDArray[np.int64],
    gold_hypnogram: npt.NDArray[np.int64],
    yasa_times: npt.NDArray[np.float64] | None = None,
    gold_times: npt.NDArray[np.float64] | None = None,
) -> dict[str, float | str]:
    """Compute Cohen's kappa between YASA predicted and gold standard hypnograms.

    Args:
        yasa_hypnogram: YASA predicted stages (integer codes per epoch)
        gold_hypnogram: Gold standard hypnogram (integer codes per epoch)
        yasa_times: Optional timestamps for YASA epochs (for alignment)
        gold_times: Optional timestamps for gold epochs (for alignment)

    Returns:
        Dictionary with Cohen's kappa, accuracy, and per-stage metrics
    """
    from sklearn.metrics import accuracy_score, cohen_kappa_score, confusion_matrix

    # Align hypnograms if timestamps provided
    if yasa_times is not None and gold_times is not None:
        # Simple nearest-neighbor alignment
        aligned_yasa = []
        aligned_gold = []
        for gt, gs in zip(gold_times, gold_hypnogram):
            idx = np.argmin(np.abs(yasa_times - gt))
            aligned_yasa.append(yasa_hypnogram[idx])
            aligned_gold.append(gs)
        yasa_aligned = np.array(aligned_yasa)
        gold_aligned = np.array(aligned_gold)
    else:
        # Assume same length and alignment
        min_len = min(len(yasa_hypnogram), len(gold_hypnogram))
        yasa_aligned = yasa_hypnogram[:min_len]
        gold_aligned = gold_hypnogram[:min_len]

    # Filter out UNK (9) and MOVE (6) from gold standard for fair comparison
    valid_mask = (gold_aligned != 9) & (gold_aligned != 6)
    yasa_valid = yasa_aligned[valid_mask]
    gold_valid = gold_aligned[valid_mask]

    if len(gold_valid) == 0:
        return {"kappa": 0.0, "accuracy": 0.0, "n_epochs": 0, "error": "No valid epochs"}

    # Overall metrics
    kappa = cohen_kappa_score(gold_valid, yasa_valid)
    accuracy = accuracy_score(gold_valid, yasa_valid)

    # Per-stage metrics
    stage_names = {0: "W", 1: "N1", 2: "N2", 3: "N3", 4: "REM"}
    cm = confusion_matrix(gold_valid, yasa_valid, labels=[0, 1, 2, 3, 4])
    per_stage = {}
    for i, stage_name in stage_names.items():
        if i < cm.shape[0]:
            tp = cm[i, i]
            fn = cm[i, :].sum() - tp
            fp = cm[:, i].sum() - tp
            precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
            per_stage[stage_name] = {
                "precision": float(precision),
                "recall": float(recall),
                "f1": float(f1),
                "support": int(cm[i, :].sum()),
            }

    return {
        "kappa": float(kappa),
        "accuracy": float(accuracy),
        "n_epochs": int(len(gold_valid)),
        "per_stage": per_stage,
        "confusion_matrix": cm.tolist(),
    }


def validate_sleep_staging_against_gold(
    eeg_signal: npt.NDArray[np.float64],
    sampling_rate: int,
    gold_hypnogram: npt.NDArray[np.int64],
    gold_times: npt.NDArray[np.float64],
    eog_signal: npt.NDArray[np.float64] | None = None,
    emg_signal: npt.NDArray[np.float64] | None = None,
) -> dict[str, Any]:
    """Full sleep staging validation: run YASA and compare against gold standard.

    This is the main entry point for Sleep-EDF baseline validation.

    Args:
        eeg_signal: EEG signal (primary channel, e.g., Fpz-Cz)
        sampling_rate: Sampling rate in Hz
        gold_hypnogram: Gold standard hypnogram (integer codes per 30s epoch)
        gold_times: Timestamps for each gold hypnogram epoch (seconds from start)
        eog_signal: Optional EOG signal
        emg_signal: Optional EMG signal

    Returns:
        Dictionary with YASA results, Cohen's kappa, and pass/fail against threshold
    """
    # Run YASA
    yasa_result = compute_yasa_sleep_staging(eeg_signal, sampling_rate, eog_signal, emg_signal)

    # Compute Cohen's kappa
    kappa_result = compute_yasa_kappa(
        yasa_result["hypnogram"],
        gold_hypnogram,
        yasa_times=np.arange(len(yasa_result["hypnogram"])) * 30.0,
        gold_times=gold_times,
    )

    # Target: Cohen's kappa >= 0.75 (substantial agreement per Landis & Koch)
    target_kappa = 0.75
    kappa_val = (
        float(kappa_result["kappa"]) if isinstance(kappa_result["kappa"], (int, float)) else 0.0
    )
    target_met = kappa_val >= target_kappa

    return {
        "yasa_hypnogram": yasa_result["hypnogram"].tolist(),
        "yasa_confidence": yasa_result["confidence"].tolist(),
        "yasa_stages": yasa_result["stages"],
        "kappa": kappa_result["kappa"],
        "accuracy": kappa_result["accuracy"],
        "n_epochs": kappa_result["n_epochs"],
        "per_stage": kappa_result.get("per_stage", {}),
        "target_kappa": target_kappa,
        "target_met": target_met,
        "gold_hypnogram": gold_hypnogram.tolist(),
        "gold_times": gold_times.tolist(),
    }
