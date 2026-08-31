# SYNAPSE-24: 24/7 Multimodal Bio-Sensing Wearable Platform

> **Phase 0: Software & Public Data Foundation**
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
│       ├── ingestion/      # Public dataset pipelines
│       │   ├── __init__.py
│       │   ├── wesad.py    # WESAD download, processing, quality
│       │   └── mitbih.py   # MIT-BIH download, R-peak validation
│       ├── signal_quality/ # SNR, SQI, HRV, artifact metrics
│       │   ├── __init__.py
│       │   ├── base.py     # QualityThresholds, SignalQualityMetrics
│       │   ├── ecg.py      # R-peaks, HRV, validation
│       │   ├── ppg.py      # SQI, perfusion index, MAP
│       │   └── eeg.py      # Spectral flatness, alpha ratio
│       └── utils/
│           ├── __init__.py
│           └── xdf.py      # LSL StreamInfo, XDF I/O
├── scripts/
│   ├── ingest_datasets.py  # Main ingestion entry point
│   └── validate_baseline.py# Baseline validation entry point
├── tests/
│   ├── test_signal_quality.py  # Unit tests for quality metrics
│   └── test_ingestion.py       # Pipeline tests (synthetic data)
├── .github/
│   └── workflows/
│       └── ci.yml          # CI/CD: lint, typecheck, test, validate
└── data/
    ├── wesad/              # Raw WESAD (gitignored)
    ├── mitbih/             # Raw MIT-BIH (gitignored)
    └── processed/          # Output XDF + JSON (gitignored)
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

## Development

### Code Quality

```bash
# Format
uv run ruff format .

# Lint
uv run ruff check .

# Type check
uv run mypy --strict src/

# Test with coverage
uv run pytest --cov=src --cov-fail-under=80
```

### Pre-commit Hooks

```bash
uv run pre-commit install
uv run pre-commit run --all-files
```

## Configuration

All configuration via environment variables or `config.yaml`:

```yaml
data_dir: "data"
output_dir: "data/processed"
wesad:
  subjects: ["S2", "S3", ...]  # default: all 15
mitbih:
  records: ["100", "101", ...]  # default: all 48
quality:
  r_peak_tolerance_ms: 50
  ppg_sqi_min: 0.7
  eeg_flatness_max: 0.5
```

## Roadmap Alignment

| Phase | Timeline | Focus | Status |
|-------|----------|-------|--------|
| **Phase 0** | Weeks 1-4 | Software stack, public data, baselines | ✅ **In Progress** |
| Phase 1 | Months 2-5 | €90 ECG+PPG+IMU rig, LSL sync | ⏳ Planned |
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

**SYNAPSE-24** — Maximizing physiological data quality, quantity, and diversity for pattern recognition.#   C I   T r i g g e r  
 