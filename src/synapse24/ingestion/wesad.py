"""WESAD dataset ingestion and preprocessing pipeline with LSL/XDF export."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import requests
from scipy.signal import resample
from tqdm import tqdm

from synapse24.signal_quality import (
    QualityThresholds,
    SignalQualityMetrics,
    Tier,
    compute_ecg_quality,
    compute_ppg_quality,
)
from synapse24.utils import (
    create_marker_stream,
    create_quality_metadata_stream,
    generate_synthetic_timestamps,
    write_xdf,
)

WESAD_URL = "https://archive.ics.uci.edu/ml/machine-learning-databases/00465/WESAD.zip"
WESAD_SUBJECTS = [f"S{i}" for i in range(2, 18) if i != 12]  # S12 missing


def download_wesad(data_dir: Path) -> Path:
    """Download and extract WESAD dataset."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    zip_path = data_dir / "WESAD.zip"
    extract_dir = data_dir / "WESAD"

    if extract_dir.exists() and any(extract_dir.iterdir()):
        return extract_dir

    response = requests.get(WESAD_URL, stream=True)
    response.raise_for_status()

    total_size = int(response.headers.get("content-length", 0))
    with (
        open(zip_path, "wb") as f,
        tqdm(total=total_size, unit="B", unit_scale=True, desc="WESAD") as pbar,
    ):
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            pbar.update(len(chunk))

    import zipfile

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(data_dir)

    zip_path.unlink()
    return extract_dir


def load_wesad_subject(subject_dir: Path) -> dict[str, Any]:
    """Load a single WESAD subject's pickle file."""
    pkl_files = list(subject_dir.glob("*.pkl"))
    if not pkl_files:
        raise FileNotFoundError(f"No pickle file in {subject_dir}")

    with open(pkl_files[0], "rb") as f:
        return pickle.load(f, encoding="latin1")


def extract_chest_signals(data: dict) -> dict[str, np.ndarray]:
    """Extract chest-worn signals (RespiBAN) from WESAD data.

    Returns dict with keys:
    - 'ecg': ECG at 700 Hz
    - 'eda': EDA at 700 Hz
    - 'emg': EMG at 700 Hz
    - 'resp': Respiration at 700 Hz
    - 'temp': Temperature at 700 Hz
    - 'acc_x', 'acc_y', 'acc_z': 3-axis ACC at 700 Hz
    - 'labels': Activity labels at 700 Hz
    """
    chest = data["signal"]["chest"]
    return {
        "ecg": chest["ECG"].flatten(),
        "eda": chest["EDA"].flatten(),
        "emg": chest["EMG"].flatten(),
        "resp": chest["Resp"].flatten(),
        "temp": chest["Temp"].flatten(),
        "acc_x": chest["ACC"][:, 0],
        "acc_y": chest["ACC"][:, 1],
        "acc_z": chest["ACC"][:, 2],
        "labels": data["label"].flatten(),
    }


def extract_wrist_signals(data: dict) -> dict[str, np.ndarray]:
    """Extract wrist-worn signals (Empatica E4) from WESAD data.

    Returns dict with keys:
    - 'bvp': BVP/PPG at 64 Hz
    - 'eda': EDA at 4 Hz
    - 'temp': Temperature at 4 Hz
    - 'acc_x', 'acc_y', 'acc_z': 3-axis ACC at 32 Hz
    """
    wrist = data["signal"]["wrist"]
    return {
        "bvp": wrist["BVP"].flatten(),
        "eda": wrist["EDA"].flatten(),
        "temp": wrist["Temp"].flatten(),
        "acc_x": wrist["ACC"][:, 0],
        "acc_y": wrist["ACC"][:, 1],
        "acc_z": wrist["ACC"][:, 2],
    }


def resample_labels(labels: np.ndarray, original_rate: int, target_rate: int) -> np.ndarray:
    """Resample labels to target rate using nearest neighbor."""
    if original_rate == target_rate:
        return labels
    ratio = target_rate / original_rate
    indices = np.arange(0, len(labels) * ratio, ratio).astype(int)
    indices = np.clip(indices, 0, len(labels) - 1)
    return labels[indices]


def compute_accel_magnitude(acc_x: np.ndarray, acc_y: np.ndarray, acc_z: np.ndarray) -> np.ndarray:
    """Compute 3D accelerometer magnitude."""
    return np.sqrt(acc_x**2 + acc_y**2 + acc_z**2)


def segment_by_label(
    signals: dict[str, np.ndarray],
    labels: np.ndarray,
    label_value: int,
) -> dict[str, np.ndarray]:
    """Extract signal segments for a specific label."""
    mask = labels == label_value
    return {k: v[mask] for k, v in signals.items()}


def _create_stream(
    name: str,
    stream_type: str,
    data: np.ndarray,
    sampling_rate: float,
    channel_names: list[str],
    channel_units: list[str],
    tier: int,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Create a stream dictionary for XDF export."""
    config = {
        "name": name,
        "type": stream_type,
        "channel_count": len(channel_names),
        "sampling_rate": sampling_rate,
        "channel_names": channel_names,
        "channel_units": channel_units,
        "tier": tier,
        "metadata": metadata,
    }
    from synapse24.utils import create_stream_info_from_dict

    timestamps = generate_synthetic_timestamps(len(data), sampling_rate)
    return {
        "info": create_stream_info_from_dict(config),
        "data": data.reshape(-1, len(channel_names)).astype(np.float32),
        "timestamps": timestamps.astype(np.float64),
    }


def _build_chest_streams(
    chest: dict[str, np.ndarray],
    subject_id: str,
    tier: Tier,
    fs_chest: int,
) -> list[dict[str, Any]]:
    """Build XDF streams for chest signals."""
    streams = []
    metadata = {"dataset": "WESAD", "subject": subject_id, "placement": "chest"}

    # ECG
    streams.append(
        _create_stream(
            f"SYNAPSE_ECG_CHEST_{subject_id}",
            "ECG_T1",
            chest["ecg"],
            fs_chest,
            ["ECG"],
            ["µV"],
            tier.value,
            metadata,
        )
    )

    # EDA
    streams.append(
        _create_stream(
            f"SYNAPSE_EDA_CHEST_{subject_id}",
            "EDA_T1",
            chest["eda"],
            fs_chest,
            ["EDA"],
            ["µS"],
            tier.value,
            metadata,
        )
    )

    acc_data = np.column_stack([chest["acc_x"], chest["acc_y"], chest["acc_z"]])
    streams.append(
        _create_stream(
            f"SYNAPSE_ACC_CHEST_{subject_id}",
            "ACC_T1",
            acc_data,
            fs_chest,
            ["ACC_X", "ACC_Y", "ACC_Z"],
            ["g", "g", "g"],
            tier.value,
            metadata,
        )
    )

    # Resp
    streams.append(
        _create_stream(
            f"SYNAPSE_RESP_CHEST_{subject_id}",
            "Resp_T1",
            chest["resp"],
            fs_chest,
            ["RESP"],
            ["a.u."],
            tier.value,
            metadata,
        )
    )

    # Temp
    streams.append(
        _create_stream(
            f"SYNAPSE_TEMP_CHEST_{subject_id}",
            "Temp_T1",
            chest["temp"],
            fs_chest,
            ["TEMP"],
            ["°C"],
            tier.value,
            metadata,
        )
    )

    return streams


def _build_wrist_streams(
    wrist: dict[str, np.ndarray],
    subject_id: str,
    fs_wrist_bvp: int,
    fs_wrist_acc: int,
) -> list[dict[str, Any]]:
    """Build XDF streams for wrist signals (Tier 0)."""
    streams = []
    metadata = {"dataset": "WESAD", "subject": subject_id, "placement": "wrist"}

    # BVP/PPG (Tier 0)
    streams.append(
        _create_stream(
            f"SYNAPSE_PPG_WRIST_{subject_id}",
            "PPG_T0",
            wrist["bvp"],
            fs_wrist_bvp,
            ["BVP"],
            ["a.u."],
            Tier.T0.value,
            metadata,
        )
    )

    # ACC (3-channel, Tier 0)
    acc_data = np.column_stack([wrist["acc_x"], wrist["acc_y"], wrist["acc_z"]])
    streams.append(
        _create_stream(
            f"SYNAPSE_ACC_WRIST_{subject_id}",
            "ACC_T0",
            acc_data,
            fs_wrist_acc,
            ["ACC_X", "ACC_Y", "ACC_Z"],
            ["g", "g", "g"],
            Tier.T0.value,
            metadata,
        )
    )

    # EDA (Tier 0)
    streams.append(
        _create_stream(
            f"SYNAPSE_EDA_WRIST_{subject_id}",
            "EDA_T0",
            wrist["eda"],
            4,
            ["EDA"],
            ["µS"],
            Tier.T0.value,
            metadata,
        )
    )

    # Temp (Tier 0)
    streams.append(
        _create_stream(
            f"SYNAPSE_TEMP_WRIST_{subject_id}",
            "Temp_T0",
            wrist["temp"],
            4,
            ["TEMP"],
            ["°C"],
            Tier.T0.value,
            metadata,
        )
    )

    return streams


def _build_marker_stream(
    chest: dict[str, np.ndarray], ecg_timestamps: np.ndarray, fs_chest: int, subject_id: str
) -> dict[str, Any]:
    """Build marker stream from activity labels."""
    label_names = {
        1: "baseline",
        2: "stress",
        3: "amusement",
        4: "meditation",
        5: "recovery",
        6: "fun",
        7: "rest",
    }

    label_timestamps = (
        np.where(np.diff(chest["labels"], prepend=chest["labels"][0]) != 0)[0].astype(np.float64)
        / fs_chest
    )
    label_timestamps = label_timestamps + ecg_timestamps[0]
    markers = [
        (float(ts), label_names.get(int(chest["labels"][int(ts * fs_chest)]), "unknown"))
        for ts in label_timestamps
        if int(ts * fs_chest) < len(chest["labels"])
    ]
    return create_marker_stream(markers, f"SYNAPSE_Markers_{subject_id}")


def _compute_segment_qualities(
    chest: dict[str, np.ndarray],
    wrist: dict[str, np.ndarray],
    wrist_acc_mag: np.ndarray,
    fs_chest: int,
    fs_wrist_bvp: int,
    thresholds: QualityThresholds,
) -> dict[str, dict]:
    """Compute quality metrics for each labeled segment."""
    label_names = {
        1: "baseline",
        2: "stress",
        3: "amusement",
        4: "meditation",
        5: "recovery",
        6: "fun",
        7: "rest",
    }

    segment_qualities = {}
    for label_val, label_name in label_names.items():
        chest_seg = segment_by_label(chest, chest["labels"], label_val)
        if len(chest_seg["ecg"]) > fs_chest * 10:
            seg_ecg = compute_ecg_quality(chest_seg["ecg"], fs_chest, thresholds=thresholds)
            chest_acc_mag = compute_accel_magnitude(
                chest_seg["acc_x"], chest_seg["acc_y"], chest_seg["acc_z"]
            )
            wrist_seg = segment_by_label(
                wrist, resample_labels(chest["labels"], fs_chest, fs_wrist_bvp), label_val
            )
            if len(wrist_seg["bvp"]) > fs_wrist_bvp * 10:
                wrist_acc_seg = resample(wrist_acc_mag, len(wrist_seg["bvp"]))
                seg_ppg = compute_ppg_quality(
                    wrist_seg["bvp"], fs_wrist_bvp, wrist_acc_seg, thresholds=thresholds
                )
                segment_qualities[label_name] = {
                    "duration_s": len(chest_seg["ecg"]) / fs_chest,
                    "ecg_quality": seg_ecg.to_dict(),
                    "ppg_quality": seg_ppg,
                }

    return segment_qualities


def process_wesad_subject(
    subject_id: str,
    data_dir: Path,
    output_dir: Path,
    tier: Tier = Tier.T1,
) -> dict[str, Any]:
    """Process a single WESAD subject and compute quality metrics with XDF export.

    WESAD chest data (RespiBAN) represents research-grade resting/stress recordings
    → Tier 1 (high-density, clean context) thresholds apply.
    """
    subject_dir = data_dir / subject_id
    data = load_wesad_subject(subject_dir)

    # Extract signals
    chest = extract_chest_signals(data)
    wrist = extract_wrist_signals(data)

    # Sampling rates
    fs_chest = 700
    fs_wrist_bvp = 64
    fs_wrist_acc = 32

    # Resample wrist ACC to match BVP for MAP computation
    wrist_acc_mag = compute_accel_magnitude(wrist["acc_x"], wrist["acc_y"], wrist["acc_z"])
    if len(wrist_acc_mag) != len(wrist["bvp"]):
        wrist_acc_mag = resample(wrist_acc_mag, len(wrist["bvp"]))

    # Tier-aware thresholds
    thresholds = QualityThresholds.for_tier(tier)

    # Compute quality metrics
    ecg_quality = compute_ecg_quality(chest["ecg"], fs_chest, thresholds=thresholds)
    ppg_quality = compute_ppg_quality(
        wrist["bvp"], fs_wrist_bvp, wrist_acc_mag, thresholds=thresholds
    )

    # Build all streams
    streams = []

    # Chest streams
    streams.extend(_build_chest_streams(chest, subject_id, tier, fs_chest))

    # Wrist streams
    streams.extend(_build_wrist_streams(wrist, subject_id, fs_wrist_bvp, fs_wrist_acc))

    # ECG timestamps for marker alignment
    ecg_timestamps = generate_synthetic_timestamps(len(chest["ecg"]), fs_chest)

    # Marker stream
    streams.append(_build_marker_stream(chest, ecg_timestamps, fs_chest, subject_id))

    # Segment qualities
    segment_qualities = _compute_segment_qualities(
        chest, wrist, wrist_acc_mag, fs_chest, fs_wrist_bvp, thresholds
    )

    # Overall quality metadata
    overall_quality = SignalQualityMetrics.from_modality_metrics(
        ecg=ecg_quality.to_dict()["metrics"]["ecg"],
        ppg=ppg_quality,
        tier=tier,
        sampling_rate_hz=fs_chest,
        duration_s=len(chest["ecg"]) / fs_chest,
    )
    overall_quality_dict = overall_quality.to_dict()
    overall_quality_dict["segments"] = segment_qualities
    overall_quality_dict["subject_id"] = subject_id
    overall_quality_dict["dataset"] = "WESAD"

    streams.append(
        create_quality_metadata_stream(overall_quality_dict, f"SYNAPSE_Metadata_{subject_id}")
    )

    # Write XDF
    xdf_path = output_dir / f"{subject_id}_wesad.xdf"
    write_xdf(xdf_path, streams)

    # Prepare return result (for backward compatibility)
    return {
        "subject_id": subject_id,
        "xdf_path": str(xdf_path),
        "sampling_rates": {
            "chest_hz": fs_chest,
            "wrist_bvp_hz": fs_wrist_bvp,
            "wrist_acc_hz": fs_wrist_acc,
        },
        "ecg_quality": ecg_quality.to_dict(),
        "ppg_quality": ppg_quality,
        "segments": segment_qualities,
        "overall_quality": overall_quality_dict,
    }


def ingest_wesad(
    data_dir: Path = Path("data/wesad"),
    output_dir: Path = Path("data/processed"),
    subjects: list[str] | None = None,
    tier: Tier = Tier.T1,
) -> list[dict]:
    """Full WESAD ingestion pipeline with XDF export.

    Args:
        data_dir: Directory for raw WESAD data
        output_dir: Directory for processed outputs
        subjects: List of subject IDs to process (default: all available)
        tier: Acquisition tier for threshold selection (default: T1 for WESAD rest/stress)

    Returns:
        List of per-subject results with XDF paths and quality metrics
    """
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    download_wesad(data_dir)

    if subjects is None:
        subjects = WESAD_SUBJECTS

    all_results = []
    for subject_id in tqdm(subjects, desc="Processing WESAD subjects"):
        try:
            result = process_wesad_subject(subject_id, data_dir, output_dir, tier)
            all_results.append(result)

            # Save per-subject quality JSON (backward compatibility)
            with open(output_dir / f"{subject_id}_quality.json", "w") as f:
                json.dump(result, f, indent=2, default=str)

        except Exception as e:
            print(f"Failed to process {subject_id}: {e}")

    # Save summary
    summary = {
        "dataset": "WESAD",
        "subjects_processed": len(all_results),
        "subjects": [r["subject_id"] for r in all_results],
        "tier": tier.name,
    }
    with open(output_dir / "wesad_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    return all_results
