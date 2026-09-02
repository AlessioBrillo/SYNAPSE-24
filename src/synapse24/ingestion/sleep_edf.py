"""Sleep-EDF Expanded dataset ingestion and validation pipeline with LSL/XDF export."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

import mne
import numpy as np
import requests
from tqdm import tqdm

from synapse24.signal_quality import (
    QualityThresholds,
    SignalQualityMetrics,
    Tier,
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

SLEEP_EDF_URL = "https://physionet.org/files/sleep-edfx/1.0.0/"
SLEEP_EDF_SUBJECTS = [f"SC4{i:03d}E0" for i in range(1, 200)]  # SC4001E0-SC4197E0


def download_sleep_edf(data_dir: Path) -> Path:
    """Download Sleep-EDF Expanded dataset using MNE."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Check if already downloaded
    if any(data_dir.glob("*.edf")):
        return data_dir

    # Use MNE's built-in downloader
    try:
        mne.datasets.sleep_physionet.age.fetch_data(
            subjects=list(range(1, 200)),
            path=data_dir,
            force_update=False,
            verbose=False,
        )
    except Exception as e:
        # Fallback: manual download
        print(f"MNE download failed: {e}, trying manual...")
        _download_sleep_edf_manual(data_dir)

    return data_dir


def _download_sleep_edf_manual(data_dir: Path) -> None:
    """Manual download fallback."""
    base_url = "https://physionet.org/files/sleep-edfx/1.0.0/sleep-cassette/"
    for subject in range(1, 200):
        subject_id = f"SC4{subject:03d}"
        for suffix in ["E0", "EC"]:
            for ext in [".edf", ".hyp"]:
                filename = f"{subject_id}{suffix}{ext}"
                url = base_url + filename
                try:
                    response = requests.get(url, timeout=30)
                    if response.status_code == 200:
                        (data_dir / filename).write_bytes(response.content)
                except Exception:
                    pass


def load_sleep_edf_subject(subject_id: str, data_dir: Path) -> dict[str, Any]:
    """Load a single Sleep-EDF subject (PSG + Hypnogram).

    Returns dict with:
    - 'eeg_fpz_cz': EEG Fpz-Cz (n_samples,)
    - 'eeg_pz_oz': EEG Pz-Oz (n_samples,)
    - 'eog': EOG (n_samples,)
    - 'emg': EMG (n_samples,)
    - 'hypnogram': Sleep stages per 30s epoch (n_epochs,)
    - 'fs': Sampling rate (100 Hz)
    - 'subject_id': Subject identifier
    """
    subject_dir = data_dir
    psg_file = subject_dir / f"{subject_id}-PSG.edf"
    hyp_file = subject_dir / f"{subject_id}-Hypnogram.edf"

    if not psg_file.exists():
        # Try alternative naming
        psg_files = list(subject_dir.glob(f"{subject_id}*.edf"))
        psg_files = [f for f in psg_files if "Hypnogram" not in f.name]
        if psg_files:
            psg_file = psg_files[0]
        else:
            raise FileNotFoundError(f"PSG file not found for {subject_id}")

    if not hyp_file.exists():
        hyp_files = list(subject_dir.glob(f"{subject_id}*Hypnogram*.edf"))
        if hyp_files:
            hyp_file = hyp_files[0]

    # Load PSG
    raw = mne.io.read_raw_edf(psg_file, preload=True, verbose=False)
    raw.pick_types(eeg=True, eog=True, emg=True)

    # Extract channels
    ch_names = raw.ch_names
    data = raw.get_data()

    # Map channels
    eeg_fpz_cz = np.array([])
    eeg_pz_oz = np.array([])
    eog = np.array([])
    emg = np.array([])

    for i, ch in enumerate(ch_names):
        ch_lower = ch.lower()
        if "fpz" in ch_lower and "cz" in ch_lower:
            eeg_fpz_cz = data[i]
        elif "pz" in ch_lower and "oz" in ch_lower:
            eeg_pz_oz = data[i]
        elif "eog" in ch_lower:
            eog = data[i]
        elif "emg" in ch_lower or "chin" in ch_lower:
            emg = data[i]

    fs = int(raw.info["sfreq"])

    # Load hypnogram
    hypnogram = np.array([])
    if hyp_file.exists():
        try:
            annot = mne.read_annotations(hyp_file)
            # Convert annotations to 30s epoch stages
            stage_map = {
                "Sleep stage W": 0,
                "Sleep stage 1": 1,
                "Sleep stage 2": 2,
                "Sleep stage 3": 3,
                "Sleep stage 4": 4,
                "Sleep stage R": 5,
                "Movement time": 6,
            }
            epoch_duration = 30  # seconds
            n_epochs = int(np.ceil(len(eeg_fpz_cz) / (fs * epoch_duration)))
            hypnogram = np.zeros(n_epochs, dtype=int)

            for annot_item in annot:
                onset = annot_item["onset"]
                duration = annot_item["duration"]
                desc = annot_item["description"]
                stage = stage_map.get(desc, 0)
                start_epoch = int(onset / epoch_duration)
                end_epoch = int((onset + duration) / epoch_duration)
                hypnogram[start_epoch:end_epoch] = stage
        except Exception:
            pass

    return {
        "eeg_fpz_cz": eeg_fpz_cz,
        "eeg_pz_oz": eeg_pz_oz,
        "eog": eog,
        "emg": emg,
        "hypnogram": hypnogram,
        "fs": fs,
        "subject_id": subject_id,
    }


def process_sleep_edf_subject(
    subject_id: str,
    data_dir: Path,
    output_dir: Path,
    tier: Tier = Tier.T1,
) -> dict[str, Any]:
    """Process a single Sleep-EDF subject and compute quality metrics with XDF export.

    Sleep data is high-density EEG during sleep → Tier 1 thresholds apply.
    """
    data = load_sleep_edf_subject(subject_id, data_dir)
    fs = data["fs"]
    thresholds = QualityThresholds.for_tier(tier)

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

    # Build XDF streams
    streams = []

    # EEG Fpz-Cz
    info_fpz = create_stream_info(
        StreamConfig(
            name=f"SYNAPSE_EEG_FPZ_CZ_{subject_id}",
            stream_type="EEG_T1",
            channel_count=1,
            sampling_rate=fs,
            channel_names=["EEG_Fpz-Cz"],
            channel_units=["µV"],
            tier=tier.value,
            metadata={"dataset": "Sleep-EDF", "subject": subject_id, "electrode": "Fpz-Cz"},
        )
    )
    timestamps_fpz = generate_synthetic_timestamps(len(data["eeg_fpz_cz"]), fs)
    streams.append(
        {
            "info": info_fpz,
            "data": data["eeg_fpz_cz"].reshape(-1, 1).astype(np.float32),
            "timestamps": timestamps_fpz.astype(np.float64),
        }
    )

    # EEG Pz-Oz
    info_pz = create_stream_info(
        StreamConfig(
            name=f"SYNAPSE_EEG_PZ_OZ_{subject_id}",
            stream_type="EEG_T1",
            channel_count=1,
            sampling_rate=fs,
            channel_names=["EEG_Pz-Oz"],
            channel_units=["µV"],
            tier=tier.value,
            metadata={"dataset": "Sleep-EDF", "subject": subject_id, "electrode": "Pz-Oz"},
        )
    )
    streams.append(
        {
            "info": info_pz,
            "data": data["eeg_pz_oz"].reshape(-1, 1).astype(np.float32),
            "timestamps": timestamps_fpz.astype(np.float64),
        }
    )

    # EOG if available
    if len(data["eog"]) > 0:
        info_eog = create_stream_info(
            StreamConfig(
                name=f"SYNAPSE_EOG_{subject_id}",
                stream_type="EOG_T1",
                channel_count=1,
                sampling_rate=fs,
                channel_names=["EOG"],
                channel_units=["µV"],
                tier=tier.value,
                metadata={"dataset": "Sleep-EDF", "subject": subject_id},
            )
        )
        streams.append(
            {
                "info": info_eog,
                "data": data["eog"].reshape(-1, 1).astype(np.float32),
                "timestamps": timestamps_fpz.astype(np.float64),
            }
        )

    # EMG if available
    if len(data["emg"]) > 0:
        info_emg = create_stream_info(
            StreamConfig(
                name=f"SYNAPSE_EMG_{subject_id}",
                stream_type="EMG_T1",
                channel_count=1,
                sampling_rate=fs,
                channel_names=["EMG"],
                channel_units=["µV"],
                tier=tier.value,
                metadata={"dataset": "Sleep-EDF", "subject": subject_id},
            )
        )
        streams.append(
            {
                "info": info_emg,
                "data": data["emg"].reshape(-1, 1).astype(np.float32),
                "timestamps": timestamps_fpz.astype(np.float64),
            }
        )

    # Hypnogram markers
    if len(data["hypnogram"]) > 0:
        stage_names = {
            0: "W",
            1: "N1",
            2: "N2",
            3: "N3",
            4: "N4",
            5: "REM",
            6: "MOVEMENT",
        }
        epoch_times = np.arange(len(data["hypnogram"])) * 30 + timestamps_fpz[0]
        markers = [
            (float(t), stage_names.get(int(s), f"UNKNOWN_{s}"))
            for t, s in zip(epoch_times, data["hypnogram"])
        ]
        streams.append(create_marker_stream(markers, f"SYNAPSE_Hypnogram_{subject_id}"))

    # Overall quality metadata
    valid_epochs = [eq for eq in epoch_qualities if eq["eeg_fpz_cz"].get("quality_pass")]
    avg_flatness = np.mean([eq["eeg_fpz_cz"]["spectral_flatness"] for eq in valid_epochs])
    avg_alpha_ratio = np.mean([eq["eeg_fpz_cz"]["alpha_band_ratio"] for eq in valid_epochs])

    overall_quality = SignalQualityMetrics(
        spectral_flatness=float(avg_flatness),
        alpha_band_ratio=float(avg_alpha_ratio),
        sampling_rate_hz=fs,
        duration_s=len(data["eeg_fpz_cz"]) / fs,
        modality="eeg",
        tier=tier,
        thresholds=thresholds,
    )
    overall_quality_dict = overall_quality.to_dict()
    overall_quality_dict["subject_id"] = subject_id
    overall_quality_dict["dataset"] = "Sleep-EDF"
    overall_quality_dict["epochs"] = len(epoch_qualities)
    overall_quality_dict["valid_epochs"] = len(valid_epochs)

    streams.append(
        create_quality_metadata_stream(overall_quality_dict, f"SYNAPSE_Metadata_{subject_id}")
    )

    # Write XDF
    xdf_path = output_dir / f"{subject_id}_sleep_edf.xdf"
    write_xdf(xdf_path, streams)

    # Save per-subject quality JSON
    with open(output_dir / f"{subject_id}_quality.json", "w") as f:
        json.dump(
            {
                "subject_id": subject_id,
                "xdf_path": str(xdf_path),
                "fs": fs,
                "n_epochs": n_epochs,
                "epoch_qualities": [
                    {
                        "epoch": eq["epoch"],
                        "stage": eq["stage"],
                        "eeg_fpz_cz": eq["eeg_fpz_cz"],
                        "eeg_pz_oz": eq["eeg_pz_oz"],
                    }
                    for eq in epoch_qualities
                ],
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
        "n_epochs": n_epochs,
        "epoch_qualities": epoch_qualities,
        "overall_quality": overall_quality_dict,
    }


def ingest_sleep_edf(
    data_dir: Path = Path("data/sleep_edf"),
    output_dir: Path = Path("data/processed"),
    subjects: list[str] | None = None,
    tier: Tier = Tier.T1,
) -> list[dict]:
    """Full Sleep-EDF ingestion pipeline with XDF export."""
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    download_sleep_edf(data_dir)

    if subjects is None:
        subjects = SLEEP_EDF_SUBJECTS

    all_results = []
    for subject_id in tqdm(subjects, desc="Processing Sleep-EDF subjects"):
        try:
            result = process_sleep_edf_subject(subject_id, data_dir, output_dir, tier)
            all_results.append(result)
        except Exception as e:
            print(f"Failed to process {subject_id}: {e}")

    # Save summary
    summary = {
        "dataset": "Sleep-EDF Expanded",
        "subjects_processed": len(all_results),
        "subjects": [r["subject_id"] for r in all_results],
        "tier": tier.name,
    }
    with open(output_dir / "sleep_edf_summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    return all_results
