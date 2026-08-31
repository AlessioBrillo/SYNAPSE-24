"""WESAD dataset ingestion and preprocessing pipeline."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import Any

import numpy as np
import requests
from tqdm import tqdm

from synapse24.signal_quality import compute_ecg_quality, compute_ppg_quality

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
    with open(zip_path, "wb") as f, tqdm(
        total=total_size, unit="B", unit_scale=True, desc="WESAD"
    ) as pbar:
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
    - 'labels': Activity labels (resampled)
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


def process_wesad_subject(
    subject_id: str,
    data_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Process a single WESAD subject and compute quality metrics."""
    subject_dir = data_dir / subject_id
    data = load_wesad_subject(subject_dir)

    # Extract signals
    chest = extract_chest_signals(data)
    wrist = extract_wrist_signals(data)

    # Chest signals at 700 Hz
    fs_chest = 700
    # Wrist BVP at 64 Hz
    fs_wrist_bvp = 64
    # Wrist ACC at 32 Hz
    fs_wrist_acc = 32

    # Resample wrist ACC to match BVP for MAP computation
    from scipy.signal import resample
    wrist_acc_mag = compute_accel_magnitude(
        wrist["acc_x"], wrist["acc_y"], wrist["acc_z"]
    )
    if len(wrist_acc_mag) != len(wrist["bvp"]):
        wrist_acc_mag = resample(wrist_acc_mag, len(wrist["bvp"]))

    # Compute quality metrics for chest ECG
    ecg_quality = compute_ecg_quality(
        chest["ecg"], fs_chest, thresholds=None
    )

    # Compute quality metrics for wrist BVP
    ppg_quality = compute_ppg_quality(
        wrist["bvp"], fs_wrist_bvp, wrist_acc_mag
    )

    # Segment by activity labels
    # Labels: 1=baseline, 2=stress, 3=amusement, 4=meditation, 5=recovery, 6=fun, 7=rest
    label_names = {
        1: "baseline",
        2: "stress",
        3: "amusement",
        4: "meditation",
        5: "recovery",
        6: "fun",
        7: "rest",
    }

    results = {
        "subject_id": subject_id,
        "sampling_rates": {
            "chest_hz": fs_chest,
            "wrist_bvp_hz": fs_wrist_bvp,
            "wrist_acc_hz": fs_wrist_acc,
        },
        "ecg_quality": ecg_quality.to_dict(),
        "ppg_quality": ppg_quality,
        "segments": {},
    }

    # Process each labeled segment
    for label_val, label_name in label_names.items():
        chest_seg = segment_by_label(chest, chest["labels"], label_val)
        if len(chest_seg["ecg"]) > fs_chest * 10:  # At least 10 seconds
            # Recompute quality for this segment
            seg_ecg = compute_ecg_quality(
                chest_seg["ecg"], fs_chest
            )
            compute_accel_magnitude(
                chest_seg["acc_x"], chest_seg["acc_y"], chest_seg["acc_z"]
            )
            # Resample wrist BVP to segment length if needed
            wrist_seg = segment_by_label(wrist, resample_labels(chest["labels"], fs_chest, fs_wrist_bvp), label_val)
            if len(wrist_seg["bvp"]) > fs_wrist_bvp * 10:
                seg_ppg = compute_ppg_quality(
                    wrist_seg["bvp"], fs_wrist_bvp,
                    resample(wrist_acc_mag, len(wrist_seg["bvp"]))
                )
                results["segments"][label_name] = {
                    "duration_s": len(chest_seg["ecg"]) / fs_chest,
                    "ecg_quality": seg_ecg.to_dict(),
                    "ppg_quality": seg_ppg,
                }

    return results


def ingest_wesad(
    data_dir: Path = Path("data/wesad"),
    output_dir: Path = Path("data/processed"),
    subjects: list[str] | None = None,
) -> list[dict]:
    """Full WESAD ingestion pipeline.

    Args:
        data_dir: Directory for raw WESAD data
        output_dir: Directory for processed outputs
        subjects: List of subject IDs to process (default: all available)

    Returns:
        List of per-subject results
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
            result = process_wesad_subject(subject_id, data_dir, output_dir)
            all_results.append(result)

            # Save per-subject results
            import json
            with open(output_dir / f"{subject_id}_quality.json", "w") as f:
                json.dump(result, f, indent=2, default=str)

        except Exception:
            pass

    # Save summary
    summary = {
        "dataset": "WESAD",
        "subjects_processed": len(all_results),
        "subjects": [r["subject_id"] for r in all_results],
    }
    with open(output_dir / "wesad_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    return all_results
