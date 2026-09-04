"""Base classes for signal quality assessment with tier-aware thresholds."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np
import numpy.typing as npt


class Tier(Enum):
    """Acquisition tier per Architecture.md §33-43."""

    T0 = 0  # Continuous H24: PPG, IMU, Temp, 1-2ch EEG in-ear
    T1 = 1  # High-density rest/sleep: EEG 6-16ch, fNIRS, ECG rest
    T2 = 2  # On-demand: Cognitive tasks, calibration, labeled data


@dataclass(frozen=True)
class QualityThresholds:
    """Configurable thresholds for signal quality classification.

    Thresholds are tier-aware per Architecture.md energy/SNR tradeoffs:
    - Tier 0: Continuous wearable, relaxed thresholds (lower SNR acceptable)
    - Tier 1: High-density during rest/sleep, strict thresholds (clean signal critical)
    - Tier 2: Calibration/labeling, configurable per protocol

    Literature citations in tier factory methods.
    """

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

    # fNIRS thresholds
    fnirs_cv_dc_max: float = 0.05
    fnirs_snr_min_db: float = 10.0
    fnirs_motion_corr_max: float = 0.3
    fnirs_short_ch_corr_min: float = 0.7

    # EDA thresholds
    eda_artifact_ratio_max: float = 0.1

    # Metadata
    tier: Tier = Tier.T1

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
            "fnirs_cv_dc_max": self.fnirs_cv_dc_max,
            "fnirs_snr_min_db": self.fnirs_snr_min_db,
            "fnirs_motion_corr_max": self.fnirs_motion_corr_max,
            "fnirs_short_ch_corr_min": self.fnirs_short_ch_corr_min,
            "eda_artifact_ratio_max": self.eda_artifact_ratio_max,
            "tier": self.tier.value,
        }

    @classmethod
    def for_tier(cls, tier: Tier) -> QualityThresholds:
        """Create tier-appropriate thresholds with literature-backed values.

        Tier 0 (Continuous H24):
        - Relaxed: Wearable context, motion artifacts expected, SNR lower
        - PPG SQI ≥0.5 (Karlen 2013: wearable PPG acceptable >0.5)
        - ECG Se/PPV ≥0.990 (MIT-BIH gold standard relaxed for ambulatory)
        - EEG flatness ≤0.6 (in-ear single-channel, lower spatial resolution)

        Tier 1 (High-density rest/sleep):
        - Strict: Clean signal critical for fusion, sleep staging, HRV
        - PPG SQI ≥0.7 (EmotiBit validation Chen 2024: research-grade)
        - ECG Se/PPV ≥0.996 (MIT-BIH gold standard Moody 2001)
        - RMSSD MAE ≤5ms (HRV Task Force 1996 guidelines)
        - EEG flatness ≤0.3 (high-density scalp, eyes-closed alpha dominant)
        - Alpha ratio ≥1.5 (resting eyes-closed alpha power > total)

        Tier 2 (On-demand calibration):
        - Configurable: Protocol-specific, typically stricter than T1
        - Defaults to T1 values, overridden by protocol config
        """
        if tier == Tier.T0:
            return cls(
                # ECG: Ambulatory relaxed (MIT-BIH gold standard is 0.996+)
                r_peak_sensitivity_min=0.990,
                r_peak_ppv_min=0.990,
                rmssd_mae_max_ms=10.0,  # HRV less precise in ambulatory
                # PPG: Wearable acceptable (Karlen et al. 2013; Orphanidou 2018)
                ppg_sqi_min=0.5,
                perfusion_index_min=0.01,  # 1% acceptable for wrist
                map_max=0.5,  # Higher motion artifact tolerance
                # EEG: In-ear single-channel (Guermandi et al. EMBC 2022)
                spectral_flatness_max=0.6,
                alpha_ratio_min=0.15,  # Reduced spatial sampling
                # fNIRS: Not typically acquired in T0
                fnirs_cv_dc_max=0.1,
                fnirs_snr_min_db=5.0,
                fnirs_motion_corr_max=0.5,
                fnirs_short_ch_corr_min=0.5,
                eda_artifact_ratio_max=0.2,
                tier=Tier.T0,
            )

        if tier == Tier.T1:
            return cls(
                # ECG: Research-grade (MIT-BIH Moody & Mark 2001)
                r_peak_sensitivity_min=0.996,
                r_peak_ppv_min=0.996,
                rmssd_mae_max_ms=5.0,  # HRV Task Force 1996
                # PPG: Research-grade (EmotiBit Chen et al. 2024; WESAD Schmidt 2018)
                ppg_sqi_min=0.7,
                perfusion_index_min=0.02,  # 2% (EmotiBit validation)
                map_max=0.3,  # WESAD benchmark
                # EEG: High-density sleep/rest (Sleep-EDF, YASA standards)
                spectral_flatness_max=0.3,
                alpha_ratio_min=1.5,  # Eyes-closed alpha dominance
                # fNIRS: Research-grade (OpenNIRScap Kim 2025; Brigadoi 2014)
                fnirs_cv_dc_max=0.05,
                fnirs_snr_min_db=10.0,
                fnirs_motion_corr_max=0.3,
                fnirs_short_ch_corr_min=0.7,
                # EDA: Clean rest (WESAD)
                eda_artifact_ratio_max=0.1,
                tier=Tier.T1,
            )

        if tier == Tier.T2:
            # Calibration: Default to T1, protocol overrides via config
            return cls.for_tier(Tier.T1)

        raise ValueError(f"Unknown tier: {tier}")


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

    # fNIRS
    fnirs_cv_dc: float | None = None
    fnirs_snr_db: float | None = None
    fnirs_motion_corr: float | None = None
    fnirs_short_ch_corr: float | None = None

    # EDA
    eda_artifact_ratio: float | None = None

    # Metadata
    sampling_rate_hz: float | None = None
    duration_s: float | None = None
    modality: str = ""
    tier: Tier = Tier.T1
    thresholds: QualityThresholds = field(
        default_factory=lambda: QualityThresholds.for_tier(Tier.T1)
    )

    def __post_init__(self) -> None:
        """Auto-align thresholds to tier after initialization."""
        if self.thresholds.tier != self.tier:
            self.thresholds = QualityThresholds.for_tier(self.tier)

    def _check_metric(self, value: float | None, threshold: float, comparison: str) -> bool | None:
        """Check a single metric against its threshold."""
        if value is None:
            return None
        if comparison == ">=":
            return value >= threshold
        if comparison == "<=":
            return value <= threshold
        raise ValueError(f"Unknown comparison: {comparison}")

    def evaluate(self) -> dict[str, bool]:
        """Evaluate all metrics against thresholds."""
        checks = [
            (
                "r_peak_sensitivity",
                self.r_peak_sensitivity,
                self.thresholds.r_peak_sensitivity_min,
                ">=",
            ),
            ("r_peak_ppv", self.r_peak_ppv, self.thresholds.r_peak_ppv_min, ">="),
            ("rmssd_mae", self.rmssd_mae_ms, self.thresholds.rmssd_mae_max_ms, "<="),
            ("ppg_sqi", self.ppg_sqi, self.thresholds.ppg_sqi_min, ">="),
            ("perfusion_index", self.perfusion_index, self.thresholds.perfusion_index_min, ">="),
            ("motion_artifact", self.motion_artifact_prob, self.thresholds.map_max, "<="),
            (
                "spectral_flatness",
                self.spectral_flatness,
                self.thresholds.spectral_flatness_max,
                "<=",
            ),
            ("alpha_ratio", self.alpha_band_ratio, self.thresholds.alpha_ratio_min, ">="),
            ("fnirs_cv_dc", self.fnirs_cv_dc, self.thresholds.fnirs_cv_dc_max, "<="),
            ("fnirs_snr", self.fnirs_snr_db, self.thresholds.fnirs_snr_min_db, ">="),
            (
                "fnirs_motion_artifact",
                self.fnirs_motion_corr,
                self.thresholds.fnirs_motion_corr_max,
                "<=",
            ),
            (
                "fnirs_short_ch_corr",
                self.fnirs_short_ch_corr,
                self.thresholds.fnirs_short_ch_corr_min,
                ">=",
            ),
            ("eda_artifact", self.eda_artifact_ratio, self.thresholds.eda_artifact_ratio_max, "<="),
        ]

        results = {}
        for name, value, threshold, comparison in checks:
            result = self._check_metric(value, threshold, comparison)
            if result is not None:
                results[name] = result
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
            "tier": self.tier.value,
            "tier_name": self.tier.name,
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
                "fnirs": {
                    "cv_dc": self.fnirs_cv_dc,
                    "snr_db": self.fnirs_snr_db,
                    "motion_corr": self.fnirs_motion_corr,
                    "short_ch_corr": self.fnirs_short_ch_corr,
                },
                "eda": {
                    "artifact_ratio": self.eda_artifact_ratio,
                },
            },
            "evaluations": evals,
            "overall_pass": self.overall_pass(),
            "thresholds": self.thresholds.to_dict(),
        }

    @classmethod
    def from_modality_metrics(
        cls,
        ecg: dict[str, Any] | None = None,
        ppg: dict[str, Any] | None = None,
        eeg: dict[str, Any] | None = None,
        fnirs: dict[str, Any] | None = None,
        eda: dict[str, Any] | None = None,
        tier: Tier = Tier.T1,
        sampling_rate_hz: float | None = None,
        duration_s: float | None = None,
    ) -> SignalQualityMetrics:
        """Aggregate modality-specific quality dicts into unified metrics."""
        metrics = cls(tier=tier, sampling_rate_hz=sampling_rate_hz, duration_s=duration_s)

        if ecg:
            metrics.modality = "ecg"
            metrics.r_peak_sensitivity = ecg.get("r_peak_sensitivity")
            metrics.r_peak_ppv = ecg.get("r_peak_ppv")
            metrics.rmssd_mae_ms = ecg.get("rmssd_mae_ms")
            metrics.hrv_metrics = ecg.get("hrv_metrics", {})

        if ppg:
            metrics.modality = "ppg" if not metrics.modality else f"{metrics.modality}+ppg"
            metrics.ppg_sqi = ppg.get("ppg_sqi")
            metrics.perfusion_index = ppg.get("perfusion_index")
            metrics.motion_artifact_prob = ppg.get("motion_artifact_prob")

        if eeg:
            metrics.modality = "eeg" if not metrics.modality else f"{metrics.modality}+eeg"
            metrics.spectral_flatness = eeg.get("spectral_flatness")
            metrics.alpha_band_ratio = eeg.get("alpha_band_ratio")

        if fnirs:
            metrics.modality = "fnirs" if not metrics.modality else f"{metrics.modality}+fnirs"
            metrics.fnirs_cv_dc = fnirs.get("cv_dc")
            metrics.fnirs_snr_db = fnirs.get("snr_db")
            metrics.fnirs_motion_corr = fnirs.get("motion_corr")
            metrics.fnirs_short_ch_corr = fnirs.get("short_ch_corr")

        if eda:
            metrics.modality = "eda" if not metrics.modality else f"{metrics.modality}+eda"
            metrics.eda_artifact_ratio = eda.get("artifact_ratio")

        return metrics


def compute_snr(signal: npt.NDArray[np.float64], noise: npt.NDArray[np.float64]) -> float:
    """Compute Signal-to-Noise Ratio in dB."""
    signal_power = np.mean(signal**2)
    noise_power = np.mean(noise**2)
    if noise_power == 0:
        return float("inf")
    return float(10 * np.log10(signal_power / noise_power))
