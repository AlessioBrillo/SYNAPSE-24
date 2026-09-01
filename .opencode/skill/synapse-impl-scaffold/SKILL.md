---
name: synapse-impl-scaffold
description: Generate new modality ingestion pipeline with tests, quality metrics, XDF export for SYNAPSE-24.
when_to_use: Adding new dataset ingestion (Sleep-EDF, DEAP, fNIRS), new signal quality metric, new hardware board support.
user-invocable: true
---

# SYNAPSE Implementation Scaffold Skill

Generates complete, production-ready module structure for new SYNAPSE-24 components following architecture guardian conventions.

## Scaffold Targets

| Target | Command | Generates |
|--------|---------|-----------|
| Dataset Ingestion | `scaffold ingestion <dataset>` | `src/synapse24/ingestion/<dataset>.py`, tests, baseline validation |
| Signal Quality | `scaffold quality <modality>` | `src/synapse24/signal_quality/<modality>.py`, tests, thresholds |
| Hardware Board | `scaffold hardware <board>` | `src/synapse24/hardware/<board>.py`, config, synthetic profile |
| Acquisition Module | `scaffold acquisition <name>` | `src/synapse24/acquisition/<name>.py`, state machine, tests |

## Dataset Ingestion Scaffold

```bash
# Example: scaffold ingestion sleep_edf
scaffold ingestion sleep_edf
```

Generates:
```
src/synapse24/ingestion/sleep_edf.py     # Download, extract, process, ingest
tests/test_ingestion_sleep_edf.py        # Unit tests with synthetic data
tests/integration/test_sleep_edf.py      # Integration test (real data)
scripts/validate_sleep_edf.py            # Baseline validation script
```

### Template: `sleep_edf.py`
```python
"""Sleep-EDF dataset ingestion pipeline."""

from __future__ import annotations
from pathlib import Path
from typing import Any
import numpy as np
import mne
from tqdm import tqdm

from synapse24.signal_quality import (
    compute_eeg_quality,
    compute_ecg_quality,
    QualityThresholds,
    Tier,
)
from synapse24.utils import create_stream_info, write_xdf


SLEEP_EDF_URL = "https://physionet.org/files/sleep-edfx/1.0.0/"
SLEEP_EDF_SUBJECTS = [f"SC4{i:03d}E0" for i in range(1, 200)]  # 197 subjects


def download_sleep_edf(data_dir: Path) -> Path:
    """Download Sleep-EDF Expanded dataset."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    # ... wfdb or mne download logic
    return data_dir


def load_sleep_edf_subject(subject_id: str, data_dir: Path) -> dict[str, Any]:
    """Load single Sleep-EDF subject (PSG + Hypnogram)."""
    # ... mne.io.read_raw_edf + annotation parsing
    return {
        "eeg_fpz_cz": np.ndarray,  # (n_samples,)
        "eeg_pz_oz": np.ndarray,  # (n_samples,)
        "eog": np.ndarray,  # (n_samples,)
        "emg": np.ndarray,  # (n_samples,)
        "hypnogram": np.ndarray,  # (n_epochs,) sleep stages 0-5
        "fs": 100,  # Hz
        "subject_id": subject_id,
    }


def process_sleep_edf_subject(
    subject_id: str,
    data_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Process subject: quality metrics per epoch, XDF export."""
    data = load_sleep_edf_subject(subject_id, data_dir)
    fs = data["fs"]
    thresholds = QualityThresholds.for_tier(Tier.T1)  # Sleep = Tier 1

    # Per-epoch quality (30s epochs)
    epoch_samples = 30 * fs
    n_epochs = len(data["eeg_fpz_cz"]) // epoch_samples

    epoch_qualities = []
    for i in range(n_epochs):
        start = i * epoch_samples
        end = start + epoch_samples

        eeg_fpz = data["eeg_fpz_cz"][start:end]
        eeg_pz = data["eeg_pz_oz"][start:end]

        q_fpz = compute_eeg_quality(eeg_fpz, fs, state="sleep")
        q_pz = compute_eeg_quality(eeg_pz, fs, state="sleep")

        epoch_qualities.append(
            {
                "epoch": i,
                "stage": int(data["hypnogram"][i]) if i < len(data["hypnogram"]) else -1,
                "eeg_fpz_cz": q_fpz,
                "eeg_pz_oz": q_pz,
            }
        )

    # Export XDF
    streams = []
    # EEG streams
    for ch_name, ch_data in [("EEG_Fpz-Cz", data["eeg_fpz_cz"]), ("EEG_Pz-Oz", data["eeg_pz_oz"])]:
        info = create_stream_info(
            name=f"SYNAPSE_{ch_name}",
            stream_type="EEG_T1",
            channel_count=1,
            sampling_rate=fs,
            channel_names=[ch_name],
            channel_units=["µV"],
            tier=1,
        )
        timestamps = np.arange(len(ch_data)) / fs
        streams.append({"info": info, "data": ch_data.reshape(-1, 1), "timestamps": timestamps})

    # Hypnogram markers
    marker_info = create_stream_info(
        name="SYNAPSE_Hypnogram",
        stream_type="Markers",
        channel_count=1,
        sampling_rate=0,
        channel_names=["stage"],
        channel_units=[""],
        tier=1,
    )
    # ... marker timestamps from epoch boundaries

    # Quality metadata stream
    # ...

    xdf_path = output_dir / f"{subject_id}_sleep_edf.xdf"
    write_xdf(xdf_path, streams)

    return {
        "subject_id": subject_id,
        "fs": fs,
        "n_epochs": n_epochs,
        "epoch_qualities": epoch_qualities,
        "xdf_path": str(xdf_path),
    }


def ingest_sleep_edf(
    data_dir: Path = Path("data/sleep_edf"),
    output_dir: Path = Path("data/processed"),
    subjects: list[str] | None = None,
) -> list[dict]:
    """Full Sleep-EDF ingestion pipeline."""
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    download_sleep_edf(data_dir)

    if subjects is None:
        subjects = SLEEP_EDF_SUBJECTS

    all_results = []
    for subject_id in tqdm(subjects, desc="Processing Sleep-EDF"):
        try:
            result = process_sleep_edf_subject(subject_id, data_dir, output_dir)
            all_results.append(result)
        except Exception as e:
            print(f"Failed {subject_id}: {e}")

    # Summary
    summary = {"dataset": "Sleep-EDF", "subjects_processed": len(all_results)}
    # ... save summary

    return all_results
```

## Signal Quality Scaffold

```bash
scaffold quality fnirs
```

Generates `src/synapse24/signal_quality/fnirs.py` with:
- `compute_fnirs_quality()` — CV of DC, SNR, motion artifact, short-channel correlation
- `fnirs_motion_artifact_probability()` — correlation with accelerometer
- Tier-aware thresholds in `QualityThresholds`
- Tests in `tests/test_signal_quality_fnirs.py`

## Hardware Board Scaffold

```bash
scaffold hardware custom_esp32_ads1299
```

Generates:
- `src/synapse24/hardware/custom_esp32_ads1299.py` — BrainFlow board adapter
- `config/hardware/custom_esp32_ads1299.yaml` — Board config
- `tests/hardware/test_custom_esp32_ads1299.py` — Synthetic board profile

## Quality Gates for Scaffolds

Every generated module MUST:
- [ ] Follow synapse-architecture-guardian conventions
- [ ] Include Google-style docstrings with Args/Returns/Raises
- [ ] Have `@dataclass(frozen=True)` for config objects
- [ ] Include type hints for all public functions
- [ ] Have unit tests with synthetic data (no network)
- [ ] Have integration test placeholder (real data)
- [ ] Register in appropriate `__init__.py`
- [ ] Add baseline validation if applicable
- [ ] Update README.md dataset/modality table

## References
- Ergodix impl-scaffold skill (pattern source)
- Architecture.md, Roadmap.md (governing)
- Existing scaffolds: `src/synapse24/ingestion/wesad.py`, `src/synapse24/signal_quality/ecg.py`