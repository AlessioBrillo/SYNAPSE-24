# SYNAPSE-24: 24/7 Multimodal Bio-Sensing Wearable Platform

[![License: AGPL v3 / Commercial](https://img.shields.io/badge/License-AGPL%20v3%20%2F%20Commercial-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![CI Status](https://github.com/AlessioBrillo/SYNAPSE-24/actions/workflows/ci.yml/badge.svg)](https://github.com/AlessioBrillo/SYNAPSE-24/actions)
[![GDPR Compliant](https://img.shields.io/badge/GDPR-Biosignal%20Privacy-success.svg)](PRIVACY.md)
[![IEC 62304 Aligned](https://img.shields.io/badge/IEC%2062304-Class%20B-orange.svg)](COMPLIANCE.md)

> **Phase 0: Software & Public Data Foundation — COMPLETE**
>
> Building complete signal processing, edge AI, and validation pipelines on public datasets before hardware deployment.

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

## Governance, Compliance & Licensing

- **Licensing:** Dual-licensed under **AGPLv3** (open-source) and **Commercial License** for proprietary enterprise deployments. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
- **Privacy (GDPR):** Biosignal handling and local processing standards. See [PRIVACY.md](PRIVACY.md).
- **Medical Compliance:** IEC 62304 aligned safety and traceability. See [COMPLIANCE.md](COMPLIANCE.md).
- **Security:** Vulnerability reporting. See [SECURITY.md](SECURITY.md).
- **Contributing:** Developer guidelines and DCO. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Commercial Inquiries

For enterprise licensing, custom embedded firmware integration, or clinical validation partnerships, contact:
- **Email:** licensing@synapse24.io
