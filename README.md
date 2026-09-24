# SYNAPSE-24: 24/7 Multimodal Bio-Sensing Wearable Platform

> **Phase 0: Software & Public Data Foundation — COMPLETE**
>
> Building the complete signal processing, edge AI, and validation pipeline on public datasets before any hardware procurement.

## Architecture Overview

This repository implements the **Phase 0** foundation per [Architecture.md](Architecture.md) and [Roadmap.md](Roadmap.md):

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        SYNAPSE-24 Phase 0 Pipeline                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  Public Datasets          Signal Processing          Edge AI                │
│  ┌──────────────┐         ┌──────────────────┐    ┌──────────────────┐    │
│  │ WESAD        │────────▶│ NeuroKit2        │    │ Edge Impulse     │    │
│  │ (15 subjects,│         │ MNE-Python       │    │ TFLM Quantization│    │
│  │  ECG/EDA/ACC)│         │ BioSPPy          │    │ ESP32 Deploy     │    │
│  └──────────────┘         │ HeartPy/pyHRV    │    └──────────────────┘    │
│  ┌──────────────┐         │ YASA (sleep)     │                            │
│  │ MIT-BIH      │────────▶│                  │    Validation             │
│  │ (48 records, │         │ Quality Metrics: │    ┌──────────────────┐    │
│  │  gold R-peaks)│        │ • ECG: Se/PPV,   │    │ WESAD 3-class    │    │
│  └──────────────┘         │   RMSSD MAE      │    │ stress ≥80%      │    │
│  ┌──────────────┐         │ • PPG: SQI, PI,  │    │ MIT-BIH R-peak   │    │
│  │ Sleep-EDF,   │         │   MAP            │    │ Se≥99.6%, PPV≥99.6%│   │
│  │ DEAP, fNIRS  │         │ • EEG: SF, α/β   │    └──────────────────┘    │
│  └──────────────┘         └──────────────────┘                            │
│                                                                             │
│  Synchronization & Storage                                                 │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Lab Streaming Layer (LSL) → XDF (Extensible Data Format)           │   │
│  │ • Millisecond-precision multi-stream sync                          │   │
│  │ • Per-stream metadata (type, units, sampling rate, device info)    │   │
│  │ • Embedded signal quality metrics per segment                      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

### Installation

```bash
# Clone and enter
git clone https://github.com/AlessioBrillo/SYNAPSE-24.git
cd SYNAPSE-24

# Install with uv (fast, reproducible)
uv sync --dev

# Or with pip
pip install -e ".[dev]"
```

### Run Full Pipeline

```bash
# 1. Ingest and process WESAD + MIT-BIH (downloads ~2GB on first run)
uv run python scripts/ingest_datasets.py --dataset both

# 2. Validate against published baselines
uv run python scripts/validate_baseline.py --dataset both

# 3. Run test suite
uv run pytest --cov=src --cov-fail-under=80
```

### Expected Baseline Results

| Metric | Target | Published Benchmark |
|--------|--------|---------------------|
| WESAD 3-class stress accuracy | ≥80% | 80% (Schmidt et al., ICMI 2018) |
| MIT-BIH R-peak Sensitivity | ≥99.6% | 99.6%+ (standard) |
| MIT-BIH R-peak PPV | ≥99.6% | 99.6%+ (standard) |
| RMSSD MAE | <5 ms | — |

## Project Structure

```
SYNAPSE-24/
├── Architecture.md          # System architecture (governing document)
├── Roadmap.md              # Phase-based execution plan (governing document)
├── pyproject.toml          # Dependencies, tool config (ruff, mypy, pytest)
├── src/
│   └── synapse24/
│       ├── __init__.py
│       ├── acquisition/    # Tiered acquisition, state machine, clock sync
│       │   ├── __init__.py
│       │   ├── clock_sync.py       # Multi-pod clock synchronization
│       │   ├── coordinator.py      # Sensor pod coordination
│       │   ├── immobility.py       # IMU-based immobility detection
│       │   ├── live_lsl_sync.py    # Live LSL stream synchronization
│       │   ├── live_validator.py   # Live signal quality validation
│       │   ├── night_window.py     # Sleep window scheduler
│       │   ├── power_budget.py     # Energy budget management
│       │   ├── state_machine.py    # T0/T1/T2 tier state machine
│       │   ├── sync_marker_stream.py # Sync marker management
│       │   └── tier1_coordinator.py # Tier 1 EEG coordination
│       ├── config/         # Configuration loader
│       │   ├── __init__.py
│       │   └── loader.py   # Hardware config with env var expansion
│       ├── edge_ai/        # Edge AI / TinyML pipeline
│       │   ├── __init__.py
│       │   ├── deployment.py       # TFLM deployment artifacts
│       │   ├── model.py            # Model configuration
│       │   ├── quantization.py     # INT8/Float16 quantization
│       │   ├── training.py         # Model training
│       │   ├── wesad_int8_closure.py # WESAD INT8 closure gate
│       │   └── wesad_fusion_closure.py # WESAD fusion closure
│       ├── hardware/       # Hardware abstraction layer
│       │   ├── __init__.py
│       │   ├── base.py           # Board adapter base classes
│       │   ├── cerelog.py        # Cerelog ESP-EEG adapter
│       │   ├── emotibit.py       # EmotiBit adapter
│       │   ├── esp32_tier0.py    # ESP32-S3 Tier 0 adapter
│       │   ├── inear_eeg.py      # In-ear EEG adapter
│       │   ├── registry.py       # Device registry
│       │   └── synthetic.py      # Synthetic board for CI/testing
│       ├── ingestion/      # Public dataset pipelines
│       │   ├── __init__.py
│       │   ├── cerelog_eeg.py    # Cerelog EEG ingestion
│       │   ├── deap.py           # DEAP dataset ingestion
│       │   ├── mitbih.py         # MIT-BIH ingestion & R-peak validation
│       │   ├── sleep_edf.py      # Sleep-EDF ingestion & sleep staging
│       │   ├── wesad.py          # WESAD ingestion & processing
│       │   └── wesad_surrogate.py # WESAD surrogate windows for fusion
│       ├── signal_quality/ # SNR, SQI, HRV, artifact metrics
│       │   ├── __init__.py
│       │   ├── base.py           # QualityThresholds, SignalQualityMetrics
│       │   ├── ecg.py            # R-peaks, HRV, validation
│       │   ├── eeg.py            # Spectral flatness, alpha ratio
│       │   ├── fnirs.py          # fNIRS quality metrics
│       │   └── ppg.py            # SQI, perfusion index, MAP
│       └── utils/          # LSL/XDF utilities
│           ├── __init__.py
│           ├── xdf.py            # XDF I/O, validation, round-trip
│           └── xdf_correction.py # Timestamp drift correction
├── scripts/
│   ├── __init__.py
│   ├── download_datasets.py      # Dataset downloader with verification
│   ├── hardware_bringup.py       # Hardware bringup orchestration
│   ├── ingest_datasets.py        # Main ingestion entry point
│   ├── quantize_and_deploy.py    # Quantization & deployment
│   ├── reproduce_wesad_fusion.py # WESAD fusion reproduction
│   ├── train_triage.py           # Triage model training
│   ├── validate_baseline.py      # Baseline validation entry point
│   ├── validate_live_sync.py     # Live sync validation
│   ├── validate_live_tier0.py    # Live Tier 0 validation
│   ├── validate_phase0_exit.py   # Phase 0 exit gate
│   └── validate_phase1_entry.py  # Phase 1 entry gate (hardware bringup)
├── tests/
│   ├── fixtures/                 # Test fixtures & synthetic data
│   ├── test_acquisition_unit.py     # Unit tests for acquisition
│   ├── test_canonical_wesad_windows.py # WESAD window tests
│   ├── test_cerelog_eeg.py          # Cerelog EEG tests
│   ├── test_clock_sync.py           # Clock sync tests
│   ├── test_dataset_integrity.py    # Dataset integrity checks
│   ├── test_edge_ai.py              # Edge AI tests (requires TensorFlow)
│   ├── test_inear_eeg.py            # In-ear EEG tests
│   ├── test_ingestion.py            # Ingestion pipeline tests
│   ├── test_lsl_live_sync.py        # Live LSL sync tests
│   ├── test_mitbih_closure_gate.py  # MIT-BIH closure gate
│   ├── test_motion_gating_blocks_tier1.py # Motion gating tests
│   ├── test_per_tier_broadcast.py   # Per-tier sync broadcast tests
│   ├── test_phase0_exit_gate.py     # Phase 0 exit gate tests
│   ├── test_phase0_real_data_closure.py # Real data closure tests
│   ├── test_phase1_dry_run.py       # Phase 1 dry-run tests
│   ├── test_power_budget_physics.py # Power budget physics tests
│   ├── test_signal_quality.py       # Signal quality unit tests
│   ├── test_sleep_edf_real_data_closure.py # Sleep-EDF closure tests
│   ├── test_tier_promotion_cycle.py # Tier promotion cycle tests
│   ├── test_tier_durations.py       # Tier duration tracking tests
│   ├── test_tier1_xdf_drift_correction.py # Tier 1 XDF drift correction
│   ├── test_wesad_fusion_closure.py # WESAD fusion closure tests
│   ├── test_wesad_int8_closure.py   # WESAD INT8 closure tests
│   └── test_xdf_correction.py       # XDF timestamp correction tests
├── config/
│   ├── hardware.yaml            # Canonical hardware configuration
│   └── hardware_bringup.yaml    # Bringup-specific overrides
├── .github/
│   └── workflows/
│       └── ci.yml               # CI/CD: lint, typecheck, test, validate
└── data/
    ├── wesad/                   # Raw WESAD (gitignored)
    ├── mitbih/                  # Raw MIT-BIH (gitignored)
    └── processed/               # Output XDF + JSON (gitignored)
```

## Signal Quality Framework

### ECG Metrics
- **R-peak Sensitivity/PPV** vs. annotated beats (tolerance: 50ms)
- **RMSSD MAE** between detected and reference RR intervals
- **HRV**: time-domain (SDNN, RMSSD, pNN50) + frequency-domain (LF, HF, LF/HF)

### PPG Metrics
- **SQI** (Signal Quality Index): weighted combination of perfusion index, spectral entropy, kurtosis, peak regularity
- **Perfusion Index**: (AC/DC) × 100%
- **MAP** (Motion Artifact Probability): spectral flatness + HF energy + accelerometer correlation

### EEG Metrics
- **Spectral Flatness** (Wiener entropy): tonal vs. noisy spectrum
- **Alpha Band Ratio**: alpha power / total power (eyes-closed target >0.3)
- **Band Powers**: delta, theta, alpha, beta, gamma

### fNIRS Metrics
- **CV DC**: coefficient of variation of DC component
- **SNR**: signal-to-noise ratio in dB
- **Motion Correlation**: correlation with accelerometer
- **Short Channel Correlation**: superficial signal regression quality

## LSL / XDF Integration

Every processed dataset outputs **XDF files** with:

```python
# Stream types produced:
- SYNAPSE_ECG (ECG, µV, 700Hz)
- SYNAPSE_PPG (PPG, a.u., 64Hz)
- SYNAPSE_ACC (ACC, g, 32/700Hz)
- SYNAPSE_EDA (EDA, µS, 4/700Hz)
- SYNAPSE_Markers (Markers, string, irregular)
- SYNAPSE_Metadata (JSON quality metrics per segment)
```

Validate XDF:
```python
from synapse24.utils import validate_xdf

summary = validate_xdf(Path("data/processed/S2_wesad.xdf"))
```

## Tiered Acquisition (Architecture.md §33-43)

| Tier | Modalities | When | Power | Purpose |
|------|------------|------|-------|---------|
| **T0** | PPG, IMU, Temp, 1-2ch EEG (in-ear) | Continuous H24 | ~5 mW avg | Always-on monitoring |
| **T1** | EEG 6-16ch, fNIRS, ECG rest | Immobility / Sleep | ~50 mW avg | High-density neuro |
| **T2** | Cognitive tasks, calibration | User-initiated | ~100 mW burst | Labeled data collection |

**Motion Gate (Architecture.md §74)**: T0→T1 promotion requires measured-clean PPG (SQI ≥0.5, MAP ≤0.5) + immobility + power budget.

**Clock Sync (Architecture.md §92)**: Multi-pod synchronization via LSL markers + accelerometer correlation. T0 budget: ≤10ms residual drift. T1 budget: ≤1ms.

## Development

### Code Quality

```bash
# Format
uv run ruff format .

# Lint
uv run ruff check .

# Type check
uv run mypy --package synapse24 --config-file pyproject.toml

# Test with coverage
uv run pytest --cov=src --cov-fail-under=80
```

### Pre-commit Hooks

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

### Configuration

All configuration via environment variables or YAML files in `config/`:

- `config/hardware.yaml` — Canonical hardware configuration (pods, sync, power, motion, immobility, night window)
- `config/hardware_bringup.yaml` — Bringup-specific overrides (firmware, BLE, validation, triage model, logging)

Environment variables for secrets (BLE MACs, serial ports):
```bash
export SYNAPSE_FOREARM_BLE="aa:bb:cc:dd:ee:ff"
export SYNAPSE_FOREARM_SERIAL="COM3"
export SYNAPSE_HEAD_BLE="aa:bb:cc:dd:ee:ff"
export SYNAPSE_HEAD_SERIAL="/dev/ttyUSB0"
```

## Phase 0 Exit Criteria ✅

- [x] WESAD 3-class stress classification ≥80% accuracy (GroupKFold CV)
- [x] MIT-BIH R-peak Sensitivity ≥99.6%, PPV ≥99.6%
- [x] RMSSD MAE <5 ms
- [x] Sleep-EDF sleep staging Cohen's κ per-subject floor
- [x] Multi-pod LSL/XDF zero-drop round-trip verification
- [x] Tier 0 sync ≤10ms residual drift, Tier 1 sync ≤1ms
- [x] Edge AI triage model INT8 quantization with measured accuracy drop
- [x] Power budget physics validated for 24h T0 + 10h T1
- [x] Full test suite: lint, typecheck, 80%+ coverage, all tests pass

## Roadmap Alignment

| Phase | Timeline | Focus | Status |
|-------|----------|-------|--------|
| **Phase 0** | Weeks 1-4 | Software stack, public data, baselines | ✅ **COMPLETE** |
| Phase 1 | Months 2-5 | €90 ECG+PPG+IMU rig, LSL sync | 🔄 **Ready to Start** |
| Phase 2 | Months 5-12 | Real EEG (Cerelog/PiEEG), multimodal sync | ⏳ Planned |
| Phase 3 | Months 12-24+ | Edge AI fusion, fNIRS, 24/7 wearability | ⏳ Planned |

## References

- **Architecture**: [Architecture.md](Architecture.md) — Decoupled sensor/hub, tiered acquisition, edge triage
- **Roadmap**: [Roadmap.md](Roadmap.md) — Budget, milestones, dataset references
- **WESAD**: Schmidt et al., ICMI 2018, DOI 10.1145/3242969.3242985
- **MIT-BIH**: Moody & Mark, 2001, PhysioNet
- **LSL**: Kothe et al., Imaging Neuroscience 2025, DOI 10.1162/IMAG.a.136
- **NeuroKit2**: Makowski et al., Behav Res Methods 2021, DOI 10.3758/s13428-020-01516-y

## License

MIT License — see [LICENSE](LICENSE) for details.

---

**SYNAPSE-24** — Maximizing physiological data quality, quantity, and diversity for pattern recognition.