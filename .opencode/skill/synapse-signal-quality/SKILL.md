---
name: synapse-signal-quality
description: Modality-specific SNR/SQI/MAP computation with literature-backed thresholds, automatic flagging for SYNAPSE-24 multimodal biosignals.
when_to_use: Computing signal quality metrics (ECG, PPG, EEG, fNIRS, EDA, IMU), setting thresholds, validating against literature baselines, automatic quality flagging in pipelines.
user-invocable: true
---

# SYNAPSE Signal Quality Skill

This skill provides the signal quality assessment framework for all SYNAPSE-24 modalities. It implements Architecture.md's "segnale-per-watt" principle with tier-aware thresholds and literature-backed validation.

## Modality-Specific Metrics

### ECG (R-Peak & HRV Quality)
| Metric | Formula | Tier 0 Threshold | Tier 1 Threshold | Literature |
|--------|---------|------------------|------------------|------------|
| **Sensitivity (Se)** | TP / (TP + FN) | ≥0.990 | ≥0.996 | MIT-BIH gold standard |
| **PPV** | TP / (TP + FP) | ≥0.990 | ≥0.996 | MIT-BIH gold standard |
| **RMSSD MAE** | \|RMSSD_detected - RMSSD_ref\| | ≤10 ms | ≤5 ms | HRV guidelines (Task Force 1996) |
| **SDNN MAE** | \|SDNN_detected - SDNN_ref\| | ≤15 ms | ≤10 ms | — |
| **SQI (composite)** | Weighted: Se(0.4)+PPV(0.4)+RMSSD(0.2) | ≥0.95 | ≥0.98 | — |

### PPG (Perfusion & Motion Artifact)
| Metric | Formula | Tier 0 Threshold | Tier 1 Threshold | Literature |
|--------|---------|------------------|------------------|------------|
| **Perfusion Index (PI)** | (AC/DC) × 100% | ≥0.5% | ≥1.0% | EmotiBit validation (Chen 2024) |
| **SQI** | 0.3×PI + 0.25×(1-Entropy) + 0.2×Kurtosis + 0.25×Regularity | ≥0.5 | ≥0.7 | Karlen et al. 2013; Orphanidou 2018 |
| **MAP** | 0.4×Flatness + 0.3×HF_ratio + 0.3×Accel_corr | ≤0.5 | ≤0.3 | Temko 2017; WESAD benchmark |
| **HR MAE vs ECG** | \|HR_ppg - HR_ecg\| | ≤5 bpm | ≤2 bpm | EmotiBit vs Brain Products |

### EEG (Spectral Quality)
| Metric | Formula | Tier 0 (in-ear) | Tier 1 (scalp) | Literature |
|--------|---------|-----------------|----------------|------------|
| **Spectral Flatness** | GeometricMean(PSD) / ArithmeticMean(PSD) | ≤0.6 | ≤0.3 | EEG quality standards |
| **Alpha Ratio** | AlphaPower / TotalPower (0.5-45Hz) | ≥0.15 | ≥0.30 | Eyes-closed resting |
| **Alpha/Beta Ratio** | AlphaPower / BetaPower | ≥1.0 | ≥1.5 | — |
| **Impedance Proxy** | 1 - SpectralFlatness (high-Z → flat) | — | ≤50 kΩ | Dry electrode literature |

### fNIRS (Optical Quality)
| Metric | Formula | Threshold | Literature |
|--------|---------|-----------|------------|
| **CV of DC** | Std(DC) / Mean(DC) per channel | ≤0.05 | OpenNIRScap paper |
| **SNR (AC/DC)** | 20×log10(AC_rms / DC_mean) | ≥10 dB | fNIRS standards |
| **Motion Artifact** | Correlation(AC, Accel) | ≤0.3 | Brigadoi 2014 |
| **Short-Channel Corr** | Corr(Long, Short) for superficial removal | ≥0.7 | Gagnon 2012 |

### EDA (Electrodermal Activity)
| Metric | Formula | Threshold | Literature |
|--------|---------|-----------|------------|
| **Tonic Level** | Mean(EDA) | 1-20 µS | — |
| **SCR Rate** | Peaks/min (after deconv) | 0.1-0.5 /min | — |
| **Artifact Ratio** | Samples >5µS/s slope / Total | ≤0.1 | WESAD benchmark |

### IMU (Motion Context)
| Metric | Formula | Purpose |
|--------|---------|---------|
| **Magnitude** | √(ax²+ay²+az²) | Activity level |
| **ENMO** | Euclidean Norm Minus One | Sleep/wake classification |
| **Posture** | Accel vector vs gravity | Supine/prone/side |

## Tier-Aware Threshold Configuration

```python
from synapse24.signal_quality import QualityThresholds, Tier

# Tier 0: Continuous, wearable, lower SNR acceptable
t0_thresholds = QualityThresholds.for_tier(Tier.T0)
# r_peak_sensitivity_min=0.990
# ppg_sqi_min=0.5
# spectral_flatness_max=0.6
# alpha_ratio_min=0.15

# Tier 1: High-density, rest/sleep, strict quality
t1_thresholds = QualityThresholds.for_tier(Tier.T1)
# r_peak_sensitivity_min=0.996
# ppg_sqi_min=0.7
# spectral_flatness_max=0.3
# alpha_ratio_min=0.30

# Tier 2: Calibration, user-initiated, configurable
t2_thresholds = QualityThresholds.for_tier(Tier.T2)
# All thresholds configurable per protocol
```

## Quality Evaluation Pipeline

```python
from synapse24.signal_quality import (
    compute_ecg_quality,
    compute_ppg_quality,
    compute_eeg_quality,
    SignalQualityMetrics,
)

# Process a segment
ecg_metrics = compute_ecg_quality(ecg_signal, fs=700, reference_peaks=ref_peaks)
ppg_metrics = compute_ppg_quality(ppg_signal, fs=64, accel_magnitude=accel)
eeg_metrics = compute_eeg_quality(eeg_signal, fs=500, state="resting_eyes_closed")

# Aggregate per segment
segment_quality = SignalQualityMetrics.from_modality_metrics(
    ecg=ecg_metrics,
    ppg=ppg_metrics,
    eeg=eeg_metrics,
    tier=Tier.T1,
)

# Overall pass/fail
if not segment_quality.overall_pass():
    logger.warning(f"Segment failed quality: {segment_quality.evaluate()}")
    # Flag for review, exclude from training, or trigger re-acquisition
```

## Automatic Flagging Rules

| Condition | Action |
|-----------|--------|
| ECG Se < threshold | Flag segment, use PPG-derived HR as fallback |
| PPG MAP > threshold | Exclude PPG from fusion, rely on ECG |
| EEG flatness > threshold | Mark channel bad, interpolate if <20% channels bad |
| fNIRS short-ch corr < threshold | Superficial signal contamination — flag |
| Multi-modal sync drift > 10ms | Re-align using LSL timestamps, log drift |

## Quality Gates

- [ ] All metrics computed per Architecture.md tier definitions
- [ ] Thresholds cited from peer-reviewed literature (docstring references)
- [ ] Unit tests with synthetic signals covering edge cases
- [ ] Integration test: WESAD baseline validation ≥80% 3-class
- [ ] Integration test: MIT-BIH R-peak Se/PPV ≥99.6%
- [ ] No hardcoded thresholds in pipeline code — all via `QualityThresholds`

## References
- MIT-BIH: Moody & Mark, PhysioNet 2001
- WESAD: Schmidt et al., ICMI 2018, DOI 10.1145/3242969.3242985
- EmotiBit: Chen et al., HardwareX 2024, DOI 10.1016/j.ohx.2024.100451
- Karlen et al., "Multiparameter PPG Quality", Physiol Meas 2013
- Orphanidou, "Signal Quality Indices", Springer 2018
- Temko, "PPG Motion Artifact Detection", IEEE TBME 2017
- OpenNIRScap: Kim et al., arXiv 2025
- Brigadoi et al., "Motion Artifact in fNIRS", NeuroImage 2014