"""Base classes for signal quality assessment."""

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class QualityThresholds:
    """Configurable thresholds for signal quality classification."""

    # ECG/R-peak thresholds
    r_peak_sensitivity_min: float = 0.996
    r_peak_ppv_min: float = 0.996
    rmssd_mae_max_ms: float = 5.0

    # PPG thresholds
    ppg_sqi_min: float = 0.7
    perfusion_index_min: float = 0.02
    map_max: float = 0.3

    # EEG thresholds
    spectral_flatness_max: float = 0.5
    alpha_ratio_min: float = 1.5

    def to_dict(self) -> dict[str, float]:
        return {
            "r_peak_sensitivity_min": self.r_peak_sensitivity_min,
            "r_peak_ppv_min": self.r_peak_ppv_min,
            "rmssd_mae_max_ms": self.rmssd_mae_max_ms,
            "ppg_sqi_min": self.ppg_sqi_min,
            "perfusion_index_min": self.perfusion_index_min,
            "map_max": self.map_max,
            "spectral_flatness_max": self.spectral_flatness_max,
            "alpha_ratio_min": self.alpha_ratio_min,
        }


@dataclass
class SignalQualityMetrics:
    """Container for all signal quality metrics with pass/fail evaluation."""

    # ECG
    r_peak_sensitivity: float | None = None
    r_peak_ppv: float | None = None
    rmssd_mae_ms: float | None = None
    hrv_metrics: dict[str, float] = field(default_factory=dict)

    # PPG
    ppg_sqi: float | None = None
    perfusion_index: float | None = None
    motion_artifact_prob: float | None = None

    # EEG
    spectral_flatness: float | None = None
    alpha_band_ratio: float | None = None

    # Metadata
    sampling_rate_hz: float | None = None
    duration_s: float | None = None
    modality: str = ""
    thresholds: QualityThresholds = field(default_factory=QualityThresholds)

    def evaluate(self) -> dict[str, bool]:
        """Evaluate all metrics against thresholds."""
        results = {}
        if self.r_peak_sensitivity is not None:
            results["r_peak_sensitivity"] = (
                self.r_peak_sensitivity >= self.thresholds.r_peak_sensitivity_min
            )
        if self.r_peak_ppv is not None:
            results["r_peak_ppv"] = self.r_peak_ppv >= self.thresholds.r_peak_ppv_min
        if self.rmssd_mae_ms is not None:
            results["rmssd_mae"] = self.rmssd_mae_ms <= self.thresholds.rmssd_mae_max_ms
        if self.ppg_sqi is not None:
            results["ppg_sqi"] = self.ppg_sqi >= self.thresholds.ppg_sqi_min
        if self.perfusion_index is not None:
            results["perfusion_index"] = self.perfusion_index >= self.thresholds.perfusion_index_min
        if self.motion_artifact_prob is not None:
            results["motion_artifact"] = self.motion_artifact_prob <= self.thresholds.map_max
        if self.spectral_flatness is not None:
            results["spectral_flatness"] = (
                self.spectral_flatness <= self.thresholds.spectral_flatness_max
            )
        if self.alpha_band_ratio is not None:
            results["alpha_ratio"] = self.alpha_band_ratio >= self.thresholds.alpha_ratio_min
        return results

    def overall_pass(self) -> bool:
        """Return True if all evaluated metrics pass."""
        evaluations = self.evaluate()
        return all(evaluations.values()) if evaluations else False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        evals = self.evaluate()
        return {
            "modality": self.modality,
            "sampling_rate_hz": self.sampling_rate_hz,
            "duration_s": self.duration_s,
            "metrics": {
                "ecg": {
                    "r_peak_sensitivity": self.r_peak_sensitivity,
                    "r_peak_ppv": self.r_peak_ppv,
                    "rmssd_mae_ms": self.rmssd_mae_ms,
                    "hrv_metrics": self.hrv_metrics,
                },
                "ppg": {
                    "ppg_sqi": self.ppg_sqi,
                    "perfusion_index": self.perfusion_index,
                    "motion_artifact_prob": self.motion_artifact_prob,
                },
                "eeg": {
                    "spectral_flatness": self.spectral_flatness,
                    "alpha_band_ratio": self.alpha_band_ratio,
                },
            },
            "evaluations": evals,
            "overall_pass": self.overall_pass(),
            "thresholds": self.thresholds.to_dict(),
        }


def compute_snr(signal: np.ndarray, noise: np.ndarray) -> float:
    """Compute Signal-to-Noise Ratio in dB."""
    signal_power = np.mean(signal**2)
    noise_power = np.mean(noise**2)
    if noise_power == 0:
        return float("inf")
    return 10 * np.log10(signal_power / noise_power)
