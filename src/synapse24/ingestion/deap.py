"""DEAP dataset ingestion and validation pipeline with LSL/XDF export."""

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
    compute_eeg_quality,
)
from synapse24.utils import (
    StreamConfig,
    create_marker_stream,
    create_quality_metadata_stream,
    create_stream_info,
    generate_synthetic_timestamps,
    write_xdf,
)

DEAP_URL = "https://www.eecs.qmul.ac.uk/mmv/datasets/deap/data_preprocessed_python.zip"
DEAP_SUBJECTS = [f"s{i:02d}" for i in range(1, 33)]  # 32 subjects


def download_deap(data_dir: Path) -> Path:
    """Download and extract DEAP dataset."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    zip_path = data_dir / "deap.zip"
    extract_dir = data_dir / "data_preprocessed_python"

    if extract_dir.exists() and any(extract_dir.iterdir()):
        return extract_dir

    print("Downloading DEAP dataset...")
    response = requests.get(DEAP_URL, stream=True)
    response.raise_for_status()

    total_size = int(response.headers.get("content-length", 0))
    with (
        open(zip_path, "wb") as f,
        tqdm(total=total_size, unit="B", unit_scale=True, desc="DEAP") as pbar,
    ):
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
            pbar.update(len(chunk))

    import zipfile

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(data_dir)

    zip_path.unlink()
    return extract_dir


def load_deap_subject(subject_dir: Path) -> dict[str, Any]:
    """Load a single DEAP subject's pickle file."""
    pkl_files = list(subject_dir.glob("*.dat"))
    if not pkl_files:
        raise FileNotFoundError(f"No .dat file in {subject_dir}")

    with open(pkl_files[0], "rb") as f:
        return dict(pickle.load(f, encoding="latin1"))


def extract_deap_signals(data: dict) -> dict[str, np.ndarray]:
    """Extract signals from DEAP data.

    Returns dict with keys:
    - 'eeg': 32-channel EEG at 128 Hz (32, n_samples)
    - 'peripheral': 8 peripheral channels (EOG, EMG, GSR, etc.) at 128 Hz
    - 'labels': 40 trials x 4 labels (valence, arousal, dominance, liking)
    """
    # DEAP format: data = (40 trials, 40 channels, 8064 samples)
    # Channels 0-31: EEG, 32-39: peripheral
    # Labels: (40 trials, 4) - valence, arousal, dominance, liking

    eeg_data = data["data"][:, :32, :]  # (40, 32, 8064)
    peripheral_data = data["data"][:, 32:, :]  # (40, 8, 8064)
    labels = data["labels"]  # (40, 4)

    # Concatenate trials along time axis
    n_trials, n_ch, n_samples = eeg_data.shape
    eeg_concat = eeg_data.transpose(1, 0, 2).reshape(n_ch, -1)  # (32, 40*8064)
    peripheral_concat = peripheral_data.transpose(1, 0, 2).reshape(8, -1)

    return {
        "eeg": eeg_concat,
        "peripheral": peripheral_concat,
        "labels": labels,
        "trial_samples": n_samples,
        "n_trials": n_trials,
    }


def process_deap_subject(
    subject_id: str,
    data_dir: Path,
    output_dir: Path,
    tier: Tier = Tier.T1,
) -> dict[str, Any]:
    """Process a single DEAP subject and compute quality metrics with XDF export."""
    subject_dir = data_dir / subject_id
    data = load_deap_subject(subject_dir)

    # Extract signals
    signals = extract_deap_signals(data)
    fs = 128  # DEAP sampling rate

    # Tier-aware thresholds
    thresholds = QualityThresholds.for_tier(tier)

    # Build XDF streams
    streams = []

    # EEG streams (32 channels)
    eeg_data = signals["eeg"]  # (32, n_samples)
    n_samples = eeg_data.shape[1]

    info_eeg = create_stream_info(
        StreamConfig(
            name=f"SYNAPSE_EEG_{subject_id}",
            stream_type="EEG_T1",
            channel_count=32,
            sampling_rate=fs,
            channel_names=[f"EEG_{i:02d}" for i in range(32)],
            channel_units=["µV"] * 32,
            tier=tier.value,
            metadata={"dataset": "DEAP", "subject": subject_id},
        )
    )
    timestamps = generate_synthetic_timestamps(n_samples, fs)
    streams.append(
        {
            "info": info_eeg,
            "data": eeg_data.T.astype(np.float32),  # (n_samples, 32)
            "timestamps": timestamps.astype(np.float64),
        }
    )

    # Peripheral streams
    peripheral_data = signals["peripheral"]  # (8, n_samples)
    peripheral_names = ["EOG_H", "EOG_V", "EMG_Z", "EMG_T", "GSR", "RESP", "PLETH", "TEMP"]
    peripheral_types = ["EOG", "EOG", "EMG", "EMG", "EDA", "RESP", "PPG", "TEMP"]

    for i in range(8):
        info_periph = create_stream_info(
            StreamConfig(
                name=f"SYNAPSE_{peripheral_names[i]}_{subject_id}",
                stream_type=f"{peripheral_types[i]}_T1",
                channel_count=1,
                sampling_rate=fs,
                channel_names=[peripheral_names[i]],
                channel_units=["µV"] if peripheral_types[i] in ["EOG", "EMG"] else ["a.u."],
                tier=tier.value,
                metadata={"dataset": "DEAP", "subject": subject_id, "channel": peripheral_names[i]},
            )
        )
        streams.append(
            {
                "info": info_periph,
                "data": peripheral_data[i].reshape(-1, 1).astype(np.float32),
                "timestamps": timestamps.astype(np.float64),
            }
        )

    # Trial markers
    trial_markers = []
    trial_duration = signals["trial_samples"] / fs
    for trial_idx in range(signals["n_trials"]):
        trial_start = trial_idx * trial_duration + timestamps[0]
        valence = signals["labels"][trial_idx, 0]
        arousal = signals["labels"][trial_idx, 1]
        trial_markers.append(
            (float(trial_start), f"Trial_{trial_idx}_V{valence:.2f}_A{arousal:.2f}")
        )

    streams.append(create_marker_stream(trial_markers, f"SYNAPSE_Trials_{subject_id}"))

    # Compute quality metrics per trial
    trial_qualities = []
    for trial_idx in range(signals["n_trials"]):
        start = trial_idx * signals["trial_samples"]
        end = start + signals["trial_samples"]

        trial_eeg = eeg_data[:, start:end]
        trial_qual = {}

        for ch in range(32):
            q = compute_eeg_quality(trial_eeg[ch], fs, state="task")
            trial_qual[f"EEG_{ch:02d}"] = q

        trial_qualities.append(
            {
                "trial": trial_idx,
                "valence": float(signals["labels"][trial_idx, 0]),
                "arousal": float(signals["labels"][trial_idx, 1]),
                "dominance": float(signals["labels"][trial_idx, 2]),
                "liking": float(signals["labels"][trial_idx, 3]),
                "eeg_quality": trial_qual,
            }
        )

    # Overall quality
    all_flatness = []
    all_alpha = []
    for tq in trial_qualities:
        for ch_q in tq["eeg_quality"].values():
            all_flatness.append(ch_q["spectral_flatness"])
            all_alpha.append(ch_q["alpha_band_ratio"])

    overall_quality = SignalQualityMetrics(
        spectral_flatness=float(np.mean(all_flatness)),
        alpha_band_ratio=float(np.mean(all_alpha)),
        sampling_rate_hz=fs,
        duration_s=n_samples / fs,
        modality="eeg",
        tier=tier,
        thresholds=thresholds,
    )
    overall_quality_dict = overall_quality.to_dict()
    overall_quality_dict["subject_id"] = subject_id
    overall_quality_dict["dataset"] = "DEAP"
    overall_quality_dict["trials"] = trial_qualities

    streams.append(
        create_quality_metadata_stream(overall_quality_dict, f"SYNAPSE_Metadata_{subject_id}")
    )

    # Write XDF
    xdf_path = output_dir / f"{subject_id}_deap.xdf"
    write_xdf(xdf_path, streams)

    # Save per-subject quality JSON
    with open(output_dir / f"{subject_id}_quality.json", "w") as f:
        json.dump(
            {
                "subject_id": subject_id,
                "xdf_path": str(xdf_path),
                "fs": fs,
                "n_trials": signals["n_trials"],
                "trial_qualities": trial_qualities,
                "overall_quality": overall_quality_dict,
            },
            f,
            indent=2,
            default=str,
        )

    return {
        "subject_id": subject_id,
        "xdf_path": str(xdf_path),
        "fs": fs,
        "n_trials": signals["n_trials"],
        "trial_qualities": trial_qualities,
        "overall_quality": overall_quality_dict,
    }


def ingest_deap(
    data_dir: Path = Path("data/deap"),
    output_dir: Path = Path("data/processed"),
    subjects: list[str] | None = None,
    tier: Tier = Tier.T1,
) -> list[dict]:
    """Full DEAP ingestion pipeline with XDF export."""
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    download_deap(data_dir)

    if subjects is None:
        subjects = DEAP_SUBJECTS

    all_results = []
    for subject_id in tqdm(subjects, desc="Processing DEAP subjects"):
        try:
            result = process_deap_subject(subject_id, data_dir, output_dir, tier)
            all_results.append(result)
        except Exception as e:
            print(f"Failed to process {subject_id}: {e}")

    # Save summary
    summary = {
        "dataset": "DEAP",
        "subjects_processed": len(all_results),
        "subjects": [r["subject_id"] for r in all_results],
        "tier": tier.name,
    }
    with open(output_dir / "deap_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    return all_results
