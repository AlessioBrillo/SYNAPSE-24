---
name: synapse-dataset-ingestion
description: Unified pipeline for PhysioNet (MIT-BIH, Sleep-EDF, PTB-XL), WESAD, DEAP, fNIRS datasets with LSL/XDF output and quality metrics.
when_to_use: Adding new public datasets, processing PhysioNet archives, WESAD/DEAP multimodal fusion, Sleep-EDF sleep staging, fNIRS motion artifact data.
user-invocable: true
---

# SYNAPSE Dataset Ingestion Skill

This skill standardizes ingestion of all public datasets used in SYNAPSE-24 Phase 0. Every dataset follows the same pipeline: **Download → Extract → Quality Assessment → LSL Stream → XDF Export**.

## Supported Datasets

| Dataset | Modalities | Subjects/Records | Sampling Rates | Use Case |
|---------|------------|------------------|----------------|----------|
| **MIT-BIH Arrhythmia** | ECG (MLII, V1) | 48 records | 360 Hz | R-peak validation, arrhythmia detection |
| **PTB-XL** | 12-lead ECG | 21,837 records | 100/500 Hz | Large-scale ECG pretraining |
| **Sleep-EDF Expanded** | EEG (Fpz-Cz, Pz-Oz), EOG, EMG | 197 PSGs | 100 Hz | Sleep staging, EEG quality |
| **WESAD** | Chest: ECG/EDA/EMG/Resp/Temp/ACC (700Hz); Wrist: BVP/EDA/Temp/ACC (64/4/32Hz) | 15 subjects | Multi-rate | Multimodal stress/affect fusion |
| **DEAP** | EEG (32ch), Peripheral (PPG, EDA, Resp, Temp) | 32 subjects | 128 Hz | EEG+peripheral fusion, valence/arousal |
| **fNIRS Motion Artifact** | fNIRS + EEG + ACC | Multiple | 10 Hz / 1 kHz | fNIRS artifact removal benchmark |

## Unified Ingestion Interface

```python
from synapse24.ingestion import DatasetIngestionPipeline

pipeline = DatasetIngestionPipeline(
    data_root=Path("data"),
    output_root=Path("data/processed"),
    quality_thresholds=QualityThresholds.for_tier(Tier.T1),  # Strict for validation
)

# Ingest any supported dataset
results = pipeline.ingest(
    dataset="wesad",
    subjects=["S2", "S3", "S4"],  # Optional subset
    export_xdf=True,
    export_lsl=True,  # Stream via LSL for real-time testing
)

# Results contain:
# - Per-subject quality metrics (JSON)
# - XDF file path
# - LSL stream metadata
# - Baseline validation results
```

## Dataset-Specific Processing

### MIT-BIH Arrhythmia
```python
from synapse24.ingestion.mitbih import ingest_mitbih

results = ingest_mitbih(
    data_dir=Path("data/mitbih"),
    output_dir=Path("data/processed"),
    records=["100", "101", "102"],  # Or None for all 48
)

# Output per record:
# - {record_id}_quality.json: Se, PPV, RMSSD MAE, HRV metrics
# - {record_id}_mitbih.xdf: ECG stream + reference annotations + quality metadata
# - mitbih_summary.json: Aggregate statistics
```

### WESAD
```python
from synapse24.ingestion.wesad import ingest_wesad

results = ingest_wesad(
    data_dir=Path("data/wesad"),
    output_dir=Path("data/processed"),
    subjects=["S2", "S3"],  # S12 missing
)

# Output per subject:
# - {subject_id}_quality.json: ECG/PPG quality per activity segment
# - {subject_id}_wesad.xdf: 7 streams (ECG, EDA, EMG, Resp, Temp, ACC_chest, BVP_wrist, ACC_wrist) + markers + quality
# - wesad_summary.json: Subject list, processing status
```

### Sleep-EDF (New)
```python
from synapse24.ingestion.sleep_edf import ingest_sleep_edf

results = ingest_sleep_edf(
    data_dir=Path("data/sleep_edf"),
    output_dir=Path("data/processed"),
    subjects=["SC4001E0", "SC4002E0"],  # Or all
)

# Output:
# - EEG streams (Fpz-Cz, Pz-Oz) at 100Hz
# - Hypnogram annotations as LSL Marker stream
# - Sleep staging quality (YASA) per epoch
# - Spectral flatness / alpha ratio per 30s epoch
```

### DEAP (New)
```python
from synapse24.ingestion.deap import ingest_deap

results = ingest_deap(
    data_dir=Path("data/deap"),
    output_dir=Path("data/processed"),
    subjects=range(1, 33),
)

# Output:
# - 32-channel EEG streams (128Hz)
# - Peripheral: PPG, EDA, Resp, Temp
# - Valence/Arousal/Dominance labels as markers
# - EEG quality per trial (3s baseline + 60s stimulus)
```

## LSL Streaming for Real-Time Testing

```python
from synapse24.ingestion import LSLReplayServer

# Replay any processed dataset via LSL for algorithm testing
server = LSLReplayServer(
    xdf_path=Path("data/processed/S2_wesad.xdf"),
    real_time=True,  # Match original timing
    loop=False,
)

server.start()
# Streams available: SYNAPSE_ECG, SYNAPSE_PPG, SYNAPSE_ACC, SYNAPSE_Markers, SYNAPSE_Metadata
# Connect with any LSL client (BrainFlow, MNE, OpenViBE, custom)
```

## XDF Export Specification

Every dataset exports to XDF 1.0 with standardized streams:

```
XDF File Structure:
├── Stream 0: SYNAPSE_ECG (ECG, µV, 700Hz/360Hz/100Hz)
├── Stream 1: SYNAPSE_PPG (PPG, a.u., 64Hz/128Hz)
├── Stream 2: SYNAPSE_EEG (EEG, µV, 100Hz/128Hz/500Hz) [multi-channel]
├── Stream 3: SYNAPSE_ACC (ACC, g, 32Hz/700Hz) [3-channel]
├── Stream 4: SYNAPSE_EDA (EDA, µS, 4Hz/700Hz)
├── Stream 5: SYNAPSE_Resp (Resp, a.u., 700Hz)
├── Stream 6: SYNAPSE_Temp (Temp, °C, 1Hz/4Hz/700Hz)
├── Stream 7: SYNAPSE_Markers (Markers, string, irregular) [annotations, events]
└── Stream 8: SYNAPSE_Metadata (Metadata, JSON, irregular) [quality metrics per segment]
```

Each stream's `info.desc()` contains:
```xml
<channels>
  <channel>
    <label>ECG</label>
    <unit>µV</unit>
    <type>ECG</type>
  </channel>
</channels>
<device>
  <manufacturer>SYNAPSE-24</manufacturer>
  <model>Phase0-PublicData</model>
</device>
<acquisition>
  <software>synapse24.ingestion</software>
  <version>0.1.0</version>
  <tier>1</tier>          <!-- Tier 0/1/2 -->
  <dataset>WESAD</dataset> <!-- Source dataset -->
  <subject>S2</subject>    <!-- Subject ID -->
  <session>baseline</session> <!-- Segment label -->
</acquisition>
```

## Quality Gates

- [ ] All datasets download via script (no manual steps)
- [ ] XDF validates with `pyxdf.load_xdf()` — zero errors
- [ ] LSL replay produces monotonic timestamps per stream
- [ ] Quality metrics match published baselines (WESAD ≥80%, MIT-BIH ≥99.6%)
- [ ] Per-segment quality embedded in XDF metadata stream
- [ ] New datasets follow same interface (single `ingest_<dataset>()` function)
- [ ] Synthetic data fallback for CI (no network dependency)

## Implementation Checklist for New Dataset

1. Create `src/synapse24/ingestion/<dataset>.py`
2. Implement: `download_<dataset>()`, `load_<dataset>_subject()`, `extract_<dataset>_signals()`, `process_<dataset>_subject()`, `ingest_<dataset>()`
3. Add to `src/synapse24/ingestion/__init__.py`
4. Add tests in `tests/test_ingestion.py` (synthetic data)
5. Add baseline validation in `scripts/validate_baseline.py`
6. Document in README.md dataset table
7. Update CI workflow to cache new dataset

## References
- Roadmap.md §116-121 (dataset list), §129-133 (order of operations)
- PhysioNet: https://physionet.org/
- WESAD: UCI ML Repository 00465
- DEAP: http://www.eecs.qmul.ac.uk/mmv/datasets/deap/
- Sleep-EDF: https://physionet.org/content/sleep-edfx/1.0.0/
- fNIRS Motion Artifact: https://physionet.org/content/fnirs-motion-artifact/1.0.0/