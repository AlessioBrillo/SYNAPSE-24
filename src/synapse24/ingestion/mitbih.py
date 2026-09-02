"""MIT-BIH Arrhythmia Database ingestion and validation pipeline with LSL/XDF export."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import wfdb
from tqdm import tqdm

from synapse24.signal_quality import (
    QualityThresholds,
    SignalQualityMetrics,
    Tier,
    compute_ecg_quality,
    detect_r_peaks_neurokit,
    r_peak_detection_quality,
    rmssd_mae,
)
from synapse24.utils import (
    StreamConfig,
    create_marker_stream,
    create_quality_metadata_stream,
    create_stream_info,
    generate_synthetic_timestamps,
    write_xdf,
)

MITBIH_RECORDS = [
    "100",
    "101",
    "102",
    "103",
    "104",
    "105",
    "106",
    "107",
    "108",
    "109",
    "111",
    "112",
    "113",
    "114",
    "115",
    "116",
    "117",
    "118",
    "119",
    "121",
    "122",
    "123",
    "124",
    "200",
    "201",
    "202",
    "203",
    "205",
    "207",
    "208",
    "209",
    "210",
    "212",
    "213",
    "214",
    "215",
    "217",
    "219",
    "220",
    "221",
    "222",
    "223",
    "228",
    "230",
    "231",
    "232",
    "233",
    "234",
]

MITBIH_URL = "https://physionet.org/files/mitdb/1.0.0/"


def download_mitbih(data_dir: Path) -> Path:
    """Download MIT-BIH Arrhythmia Database using wfdb."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Check if already downloaded
    if any(data_dir.glob("*.dat")):
        return data_dir

    for record in tqdm(MITBIH_RECORDS, desc="Records"):
        with contextlib.suppress(Exception):
            wfdb.dl_database("mitdb", str(data_dir), records=[record])

    return data_dir


def load_mitbih_record(record_id: str, data_dir: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    """Load a single MIT-BIH record.

    Returns:
        Tuple of (ecg_signal, reference_peaks, metadata)
    """
    record_path = data_dir / record_id
    record = wfdb.rdrecord(str(record_path))
    annotation = wfdb.rdann(str(record_path), "atr")

    # ECG signal (MLII lead typically)
    ecg_signal = record.p_signal[:, 0] if record.p_signal.ndim > 1 else record.p_signal.flatten()
    fs = record.fs

    # Reference R-peaks (normal beats only for clean evaluation)
    # Normal beat annotations: 'N', 'L', 'R', 'e', 'j'
    normal_beats = ["N", "L", "R", "e", "j"]
    reference_peaks = annotation.sample[np.isin(annotation.symbol, normal_beats)]

    metadata = {
        "record_id": record_id,
        "fs": fs,
        "duration_s": len(ecg_signal) / fs,
        "n_samples": len(ecg_signal),
        "n_reference_beats": len(reference_peaks),
        "age": record.comments[0] if record.comments else None,
        "sex": record.comments[1] if len(record.comments) > 1 else None,
    }

    return ecg_signal, reference_peaks, metadata


def process_mitbih_record(
    record_id: str,
    data_dir: Path,
    output_dir: Path,
    tier: Tier = Tier.T1,  # MIT-BIH is clinical gold standard → Tier 1 strict
) -> dict[str, Any]:
    """Process a single MIT-BIH record and validate R-peak detection with XDF export.

    MIT-BIH is the clinical gold standard for ECG validation.
    Tier 1 strict thresholds apply (Se/PPV ≥ 0.996, RMSSD MAE ≤ 5ms).
    """
    ecg_signal, reference_peaks, metadata = load_mitbih_record(record_id, data_dir)
    fs = metadata["fs"]

    # Tier-aware thresholds (MIT-BIH is research-grade clinical data)
    thresholds = QualityThresholds.for_tier(tier)

    # Detect R-peaks using NeuroKit2
    detected_peaks = detect_r_peaks_neurokit(ecg_signal, fs)

    # Compute quality metrics
    sensitivity, ppv = r_peak_detection_quality(detected_peaks, reference_peaks, fs)
    mae = rmssd_mae(detected_peaks, reference_peaks, fs)

    # Full ECG quality assessment
    ecg_quality = compute_ecg_quality(
        ecg_signal, fs, reference_peaks=reference_peaks, thresholds=thresholds
    )

    # Build XDF streams
    streams = []

    # ECG stream
    ecg_timestamps = generate_synthetic_timestamps(len(ecg_signal), fs)
    ecg_config = StreamConfig(
        name=f"SYNAPSE_ECG_MITBIH_{record_id}",
        stream_type="ECG_T1",
        channel_count=1,
        sampling_rate=fs,
        channel_names=["ECG_MLII"],
        channel_units=["µV"],
        tier=tier.value,
        metadata={"dataset": "MIT-BIH", "record": record_id, "lead": "MLII"},
    )
    streams.append(
        {
            "info": create_stream_info(ecg_config),
            "data": ecg_signal.reshape(-1, 1).astype(np.float32),
            "timestamps": ecg_timestamps.astype(np.float64),
        }
    )

    # Marker stream: reference R-peaks (annotations)
    ref_peak_times = reference_peaks.astype(np.float64) / fs + ecg_timestamps[0]
    markers = [(float(t), "R") for t in ref_peak_times]
    streams.append(create_marker_stream(markers, f"SYNAPSE_Markers_MITBIH_{record_id}"))

    # Quality metadata stream
    overall_quality = SignalQualityMetrics(
        r_peak_sensitivity=sensitivity,
        r_peak_ppv=ppv,
        rmssd_mae_ms=mae,
        hrv_metrics=ecg_quality.hrv_metrics,
        modality="ecg",
        tier=tier,
        sampling_rate_hz=fs,
        duration_s=len(ecg_signal) / fs,
        thresholds=thresholds,
    )
    overall_quality_dict = overall_quality.to_dict()
    overall_quality_dict["record_id"] = record_id
    overall_quality_dict["dataset"] = "MIT-BIH"
    overall_quality_dict["metadata"] = metadata

    streams.append(
        create_quality_metadata_stream(overall_quality_dict, f"SYNAPSE_Metadata_MITBIH_{record_id}")
    )

    # Write XDF
    xdf_path = output_dir / f"{record_id}_mitbih.xdf"
    write_xdf(xdf_path, streams)

    # Prepare return result (for backward compatibility)
    return {
        "record_id": record_id,
        "xdf_path": str(xdf_path),
        "metadata": metadata,
        "detected_peaks": len(detected_peaks),
        "reference_peaks": len(reference_peaks),
        "r_peak_sensitivity": sensitivity,
        "r_peak_ppv": ppv,
        "rmssd_mae_ms": mae,
        "ecg_quality": ecg_quality.to_dict(),
        "overall_quality": overall_quality_dict,
    }


def ingest_mitbih(
    data_dir: Path = Path("data/mitbih"),
    output_dir: Path = Path("data/processed"),
    records: list[str] | None = None,
    tier: Tier = Tier.T1,
) -> list[dict]:
    """Full MIT-BIH ingestion and validation pipeline with XDF export.

    Args:
        data_dir: Directory for raw MIT-BIH data
        output_dir: Directory for processed outputs
        records: List of record IDs to process (default: all 48 records)
        tier: Acquisition tier for threshold selection (default: T1 for clinical gold standard)

    Returns:
        List of per-record results with XDF paths and quality metrics
    """
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    download_mitbih(data_dir)

    if records is None:
        records = MITBIH_RECORDS

    all_results = []
    for record_id in tqdm(records, desc="Processing MIT-BIH records"):
        try:
            result = process_mitbih_record(record_id, data_dir, output_dir, tier)
            all_results.append(result)

            # Save per-record results (backward compatibility)
            with open(output_dir / f"{record_id}_quality.json", "w") as f:
                json.dump(result, f, indent=2, default=str)

        except Exception as e:
            print(f"Failed to process {record_id}: {e}")

    # Compute aggregate statistics
    sensitivities = [r["r_peak_sensitivity"] for r in all_results]
    ppvs = [r["r_peak_ppv"] for r in all_results]
    maes = [r["rmssd_mae_ms"] for r in all_results]

    summary = {
        "dataset": "MIT-BIH Arrhythmia",
        "records_processed": len(all_results),
        "aggregate_metrics": {
            "mean_sensitivity": float(np.mean(sensitivities)),
            "std_sensitivity": float(np.std(sensitivities)),
            "min_sensitivity": float(np.min(sensitivities)),
            "mean_ppv": float(np.mean(ppvs)),
            "std_ppv": float(np.std(ppvs)),
            "min_ppv": float(np.min(ppvs)),
            "mean_rmssd_mae_ms": float(np.mean(maes)),
            "std_rmssd_mae_ms": float(np.std(maes)),
        },
        "records": [r["record_id"] for r in all_results],
        "tier": tier.name,
    }

    with open(output_dir / "mitbih_summary.json", "w") as f:
        json.dump(summary, f, indent=2, default=str)

    return all_results
