"""Sleep-EDF Expanded dataset ingestion and validation pipeline with LSL/XDF export.

Sleep-EDF Expanded contains 197 whole-night polysomnograms (PSGs) with:
- EEG: Fpz-Cz, Pz-Oz at 100 Hz
- Hypnogram: 30-second epochs, sleep stages W/N1/N2/N3/REM (irregular/marker stream)
- Additional channels: EOG, EMG, airflow, etc.

Architecture.md Tier 1: High-density rest/sleep EEG (6-16ch) during sleep windows.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import requests
from tqdm import tqdm

logger = logging.getLogger(__name__)

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

# Sleep-EDF Expanded on PhysioNet
SLEEP_EDF_URL = "https://physionet.org/files/sleep-edfx/1.0.0/"
SLEEP_EDF_RECORDS = [
    # Sleep Cassette (SC) - healthy subjects
    "SC4001E0-PSG.edf",
    "SC4001EC-Hypnogram.edf",
    "SC4002E0-PSG.edf",
    "SC4002EC-Hypnogram.edf",
    "SC4011E0-PSG.edf",
    "SC4011EC-Hypnogram.edf",
    "SC4012E0-PSG.edf",
    "SC4012EC-Hypnogram.edf",
    "SC4021E0-PSG.edf",
    "SC4021EC-Hypnogram.edf",
    "SC4022E0-PSG.edf",
    "SC4022EC-Hypnogram.edf",
    "SC4031E0-PSG.edf",
    "SC4031EC-Hypnogram.edf",
    "SC4032E0-PSG.edf",
    "SC4032EC-Hypnogram.edf",
    "SC4041E0-PSG.edf",
    "SC4041EC-Hypnogram.edf",
    "SC4042E0-PSG.edf",
    "SC4042EC-Hypnogram.edf",
    "SC4051E0-PSG.edf",
    "SC4051EC-Hypnogram.edf",
    "SC4052E0-PSG.edf",
    "SC4052EC-Hypnogram.edf",
    "SC4061E0-PSG.edf",
    "SC4061EC-Hypnogram.edf",
    "SC4062E0-PSG.edf",
    "SC4062EC-Hypnogram.edf",
    "SC4071E0-PSG.edf",
    "SC4071EC-Hypnogram.edf",
    "SC4072E0-PSG.edf",
    "SC4072EC-Hypnogram.edf",
    "SC4081E0-PSG.edf",
    "SC4081EC-Hypnogram.edf",
    "SC4082E0-PSG.edf",
    "SC4082EC-Hypnogram.edf",
    "SC4091E0-PSG.edf",
    "SC4091EC-Hypnogram.edf",
    "SC4092E0-PSG.edf",
    "SC4092EC-Hypnogram.edf",
    "SC4101E0-PSG.edf",
    "SC4101EC-Hypnogram.edf",
    "SC4102E0-PSG.edf",
    "SC4102EC-Hypnogram.edf",
    "SC4111E0-PSG.edf",
    "SC4111EC-Hypnogram.edf",
    "SC4112E0-PSG.edf",
    "SC4112EC-Hypnogram.edf",
    "SC4121E0-PSG.edf",
    "SC4121EC-Hypnogram.edf",
    "SC4122E0-PSG.edf",
    "SC4122EC-Hypnogram.edf",
    "SC4131E0-PSG.edf",
    "SC4131EC-Hypnogram.edf",
    "SC4132E0-PSG.edf",
    "SC4132EC-Hypnogram.edf",
    "SC4141E0-PSG.edf",
    "SC4141EC-Hypnogram.edf",
    "SC4142E0-PSG.edf",
    "SC4142EC-Hypnogram.edf",
    "SC4151E0-PSG.edf",
    "SC4151EC-Hypnogram.edf",
    "SC4152E0-PSG.edf",
    "SC4152EC-Hypnogram.edf",
    "SC4161E0-PSG.edf",
    "SC4161EC-Hypnogram.edf",
    "SC4162E0-PSG.edf",
    "SC4162EC-Hypnogram.edf",
    "SC4171E0-PSG.edf",
    "SC4171EC-Hypnogram.edf",
    "SC4172E0-PSG.edf",
    "SC4172EC-Hypnogram.edf",
    "SC4181E0-PSG.edf",
    "SC4181EC-Hypnogram.edf",
    "SC4182E0-PSG.edf",
    "SC4182EC-Hypnogram.edf",
    "SC4191E0-PSG.edf",
    "SC4191EC-Hypnogram.edf",
    "SC4192E0-PSG.edf",
    "SC4192EC-Hypnogram.edf",
    # Sleep Telemetry (ST) - subjects with sleep disorders
    "ST7011E0-PSG.edf",
    "ST7011EC-Hypnogram.edf",
    "ST7012E0-PSG.edf",
    "ST7012EC-Hypnogram.edf",
    "ST7021E0-PSG.edf",
    "ST7021EC-Hypnogram.edf",
    "ST7022E0-PSG.edf",
    "ST7022EC-Hypnogram.edf",
    "ST7031E0-PSG.edf",
    "ST7031EC-Hypnogram.edf",
    "ST7032E0-PSG.edf",
    "ST7032EC-Hypnogram.edf",
    "ST7041E0-PSG.edf",
    "ST7041EC-Hypnogram.edf",
    "ST7042E0-PSG.edf",
    "ST7042EC-Hypnogram.edf",
    "ST7051E0-PSG.edf",
    "ST7051EC-Hypnogram.edf",
    "ST7052E0-PSG.edf",
    "ST7052EC-Hypnogram.edf",
    "ST7061E0-PSG.edf",
    "ST7061EC-Hypnogram.edf",
    "ST7062E0-PSG.edf",
    "ST7062EC-Hypnogram.edf",
    "ST7071E0-PSG.edf",
    "ST7071EC-Hypnogram.edf",
    "ST7072E0-PSG.edf",
    "ST7072EC-Hypnogram.edf",
    "ST7081E0-PSG.edf",
    "ST7081EC-Hypnogram.edf",
    "ST7082E0-PSG.edf",
    "ST7082EC-Hypnogram.edf",
    "ST7091E0-PSG.edf",
    "ST7091EC-Hypnogram.edf",
    "ST7092E0-PSG.edf",
    "ST7092EC-Hypnogram.edf",
    "ST7101E0-PSG.edf",
    "ST7101EC-Hypnogram.edf",
    "ST7102E0-PSG.edf",
    "ST7102EC-Hypnogram.edf",
    "ST7111E0-PSG.edf",
    "ST7111EC-Hypnogram.edf",
    "ST7112E0-PSG.edf",
    "ST7112EC-Hypnogram.edf",
    "ST7121E0-PSG.edf",
    "ST7121EC-Hypnogram.edf",
    "ST7122E0-PSG.edf",
    "ST7122EC-Hypnogram.edf",
    "ST7131E0-PSG.edf",
    "ST7131EC-Hypnogram.edf",
    "ST7132E0-PSG.edf",
    "ST7132EC-Hypnogram.edf",
    "ST7141E0-PSG.edf",
    "ST7141EC-Hypnogram.edf",
    "ST7142E0-PSG.edf",
    "ST7142EC-Hypnogram.edf",
    "ST7151E0-PSG.edf",
    "ST7151EC-Hypnogram.edf",
    "ST7152E0-PSG.edf",
    "ST7152EC-Hypnogram.edf",
    "ST7161E0-PSG.edf",
    "ST7161EC-Hypnogram.edf",
    "ST7162E0-PSG.edf",
    "ST7162EC-Hypnogram.edf",
    "ST7171E0-PSG.edf",
    "ST7171EC-Hypnogram.edf",
    "ST7172E0-PSG.edf",
    "ST7172EC-Hypnogram.edf",
    "ST7181E0-PSG.edf",
    "ST7181EC-Hypnogram.edf",
    "ST7182E0-PSG.edf",
    "ST7182EC-Hypnogram.edf",
    "ST7191E0-PSG.edf",
    "ST7191EC-Hypnogram.edf",
    "ST7192E0-PSG.edf",
    "ST7192EC-Hypnogram.edf",
    "ST7201E0-PSG.edf",
    "ST7201EC-Hypnogram.edf",
    "ST7202E0-PSG.edf",
    "ST7202EC-Hypnogram.edf",
    "ST7211E0-PSG.edf",
    "ST7211EC-Hypnogram.edf",
    "ST7212E0-PSG.edf",
    "ST7212EC-Hypnogram.edf",
    "ST7221E0-PSG.edf",
    "ST7221EC-Hypnogram.edf",
    "ST7222E0-PSG.edf",
    "ST7222EC-Hypnogram.edf",
    "ST7231E0-PSG.edf",
    "ST7231EC-Hypnogram.edf",
    "ST7232E0-PSG.edf",
    "ST7232EC-Hypnogram.edf",
]

# Sleep stage mapping
STAGE_MAP = {
    0: "W",  # Wake
    1: "N1",  # Stage 1
    2: "N2",  # Stage 2
    3: "N3",  # Stage 3 (slow wave)
    4: "N3",  # Stage 4 (slow wave, merged with N3 in AASM)
    5: "REM",  # REM
    6: "MOVE",  # Movement time
    9: "UNK",  # Unscored
}

STAGE_TO_INT = {v: k for k, v in STAGE_MAP.items()}


def download_sleep_edf(data_dir: Path) -> Path:
    """Download Sleep-EDF Expanded dataset from PhysioNet."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Check if already downloaded (look for any .edf file)
    if any(data_dir.glob("*.edf")):
        return data_dir

    logger.info("Downloading Sleep-EDF Expanded dataset...")

    # We'll use wfdb for downloading like MIT-BIH
    import wfdb

    for record in tqdm(SLEEP_EDF_RECORDS, desc="Downloading Sleep-EDF"):
        record_name = record.replace(".edf", "")
        try:
            wfdb.dl_database("sleep-edfx", str(data_dir), records=[record_name])
        except Exception as e:
            logger.warning(f"Failed to download {record}: {e}")

    return data_dir


def load_sleep_edf_record(psg_path: Path, hypnogram_path: Path) -> dict[str, Any]:
    """Load a Sleep-EDF record pair (PSG + Hypnogram).

    Returns:
        Dictionary with:
        - 'eeg': dict of EEG channels (name -> signal array)
        - 'eog': dict of EOG channels
        - 'emg': dict of EMG channels
        - 'other': dict of other channels (airflow, etc.)
        - 'hypnogram': array of sleep stage labels per 30s epoch
        - 'hypnogram_times': timestamps for each epoch (seconds from start)
        - 'fs_dict': sampling rates per channel
        - 'duration_s': total recording duration
        - 'metadata': recording info
    """
    import edfio

    # Load PSG (signals)
    psg = edfio.read_edf(psg_path)

    # Load Hypnogram (annotations)
    hyp = edfio.read_edf(hypnogram_path)

    # Organize channels by type
    eeg_channels = {}
    eog_channels = {}
    emg_channels = {}
    other_channels = {}
    fs_dict = {}

    for i, ch in enumerate(psg.signals):
        label = ch.label.upper()
        signal_data = np.array(ch.samples, dtype=np.float32)
        fs = ch.sample_rate
        fs_dict[label] = fs

        if "EEG" in label or "FPZ" in label or "PZ" in label or "CZ" in label or "OZ" in label:
            eeg_channels[label] = signal_data
        elif "EOG" in label:
            eog_channels[label] = signal_data
        elif "EMG" in label:
            emg_channels[label] = signal_data
        else:
            other_channels[label] = signal_data

    # Extract hypnogram annotations
    hypnogram: list[int] = []
    hypnogram_times: list[float] = []

    for ann in hyp.annotations:
        # Sleep-EDF hypnogram uses 30-second epochs with stage codes
        # Duration is typically 30 seconds per annotation
        onset = ann.onset
        ann_duration = ann.duration
        duration = ann_duration if ann_duration is not None and ann_duration > 0 else 30.0
        description = ann.description.strip()

        # Parse stage from description (e.g., "Sleep stage W", "Sleep stage 1", etc.)
        stage = _parse_hypnogram_stage(description)
        if stage is not None:
            hypnogram.append(stage)
            hypnogram_times.append(onset)

    hypnogram_arr: np.ndarray = np.array(hypnogram, dtype=int)
    hypnogram_times_arr: np.ndarray = np.array(hypnogram_times, dtype=np.float64)

    # Total duration from PSG
    max_duration = 0.0
    for ch in psg.signals:
        dur = ch.n_samples / ch.sample_rate
        max_duration = max(max_duration, dur)

    metadata = {
        "subject_id": psg_path.stem.replace("-PSG", "").replace("E0", "").replace("EC", ""),
        "header": {
            "patient": psg.header.patient,
            "recording": psg.header.recording,
            "startdate": psg.header.startdate,
            "duration": psg.header.duration,
        },
    }

    return {
        "eeg": eeg_channels,
        "eog": eog_channels,
        "emg": emg_channels,
        "other": other_channels,
        "hypnogram": hypnogram_arr,
        "hypnogram_times": hypnogram_times_arr,
        "fs_dict": fs_dict,
        "duration_s": max_duration,
        "metadata": metadata,
    }


def _parse_hypnogram_stage(description: str) -> int | None:
    """Parse sleep stage from hypnogram annotation description."""
    desc = description.upper()

    # Common formats: "Sleep stage W", "Sleep stage 1", "Sleep stage R", etc.
    patterns = [
        (("UNSCORED",), 9),  # Must come before UNK
        (("W", "WAKE", "STAGE W"), 0),
        (("1", "STAGE 1", "N1"), 1),
        (("2", "STAGE 2", "N2"), 2),
        (("3", "STAGE 3", "N3"), 3),
        (("4", "STAGE 4", "N3"), 4),
        (("R", "REM", "STAGE R"), 5),
        (("MOVE", "MOVEMENT"), 6),
        (("UNK",), 9),
    ]

    for keywords, stage in patterns:
        if any(kw in desc for kw in keywords):
            return stage

    # Try numeric
    for char in desc:
        if char.isdigit():
            val = int(char)
            if val in STAGE_MAP:
                return val

    return None


def extract_epochs(
    eeg_signal: np.ndarray,
    fs: int,
    hypnogram: np.ndarray,
    hypnogram_times: np.ndarray,
    epoch_duration: float = 30.0,
) -> dict[str, list[np.ndarray]]:
    """Extract EEG epochs aligned with hypnogram stages.

    Args:
        eeg_signal: EEG signal array (n_samples,)
        fs: Sampling rate in Hz
        hypnogram: Sleep stage per epoch
        hypnogram_times: Start time of each epoch (seconds)
        epoch_duration: Duration of each epoch in seconds (default 30s)

    Returns:
        Dict mapping stage name -> list of EEG epochs (each epoch is n_samples array)
    """
    epochs_by_stage: dict[str, list[np.ndarray]] = {stage: [] for stage in STAGE_MAP.values()}
    epoch_samples = int(epoch_duration * fs)

    for i, (stage_code, epoch_start) in enumerate(zip(hypnogram, hypnogram_times)):
        stage_name = STAGE_MAP.get(stage_code, "UNK")

        start_sample = int(epoch_start * fs)
        end_sample = start_sample + epoch_samples

        if end_sample <= len(eeg_signal):
            epoch = eeg_signal[start_sample:end_sample]
            if len(epoch) == epoch_samples:
                epochs_by_stage[stage_name].append(epoch)

    return epochs_by_stage


def compute_sleep_edf_quality(
    eeg_channels: dict[str, np.ndarray],
    fs_dict: dict[str, int],
    hypnogram: np.ndarray,
    hypnogram_times: np.ndarray,
    thresholds: QualityThresholds,
) -> dict[str, Any]:
    """Compute EEG quality metrics per channel and per sleep stage."""
    results: dict[str, Any] = {
        "per_channel": {},
        "per_stage": {},
        "overall": {},
    }

    all_flatness = []
    all_alpha = []

    # Per-channel quality (using first EEG channel as representative)
    for ch_name, signal in eeg_channels.items():
        fs = fs_dict.get(ch_name, 100)
        if len(signal) < fs * 10:  # Need at least 10 seconds
            continue

        # Overall quality (eyes-closed sleep state)
        quality = compute_eeg_quality(signal, fs, state="resting_eyes_closed")

        results["per_channel"][ch_name] = {
            "sampling_rate": fs,
            "duration_s": len(signal) / fs,
            "spectral_flatness": quality["spectral_flatness"],
            "alpha_band_ratio": quality["alpha_band_ratio"],
            "band_powers": quality["band_powers"],
            "quality_pass": quality["quality_pass"],
        }

        all_flatness.append(quality["spectral_flatness"])
        all_alpha.append(quality["alpha_band_ratio"])

    # Per-stage quality (using Fpz-Cz if available, else first EEG)
    reference_channel = None
    for preferred in ["EEG FPZ-CZ", "EEG Fpz-Cz", "FPZ-CZ", "Fpz-Cz"]:
        if preferred in eeg_channels:
            reference_channel = preferred
            break
    if reference_channel is None and eeg_channels:
        reference_channel = list(eeg_channels.keys())[0]

    if reference_channel:
        ref_signal = eeg_channels[reference_channel]
        ref_fs = fs_dict.get(reference_channel, 100)

        epochs_by_stage = extract_epochs(ref_signal, ref_fs, hypnogram, hypnogram_times)

        for stage_name, epochs in epochs_by_stage.items():
            if len(epochs) < 3:  # Need at least 3 epochs for meaningful stats
                continue

            stage_flatness = []
            stage_alpha = []

            for epoch in epochs:
                q = compute_eeg_quality(epoch, ref_fs, state="resting_eyes_closed")
                stage_flatness.append(q["spectral_flatness"])
                stage_alpha.append(q["alpha_band_ratio"])

            results["per_stage"][stage_name] = {
                "n_epochs": len(epochs),
                "mean_spectral_flatness": float(np.mean(stage_flatness)),
                "std_spectral_flatness": float(np.std(stage_flatness)),
                "mean_alpha_ratio": float(np.mean(stage_alpha)),
                "std_alpha_ratio": float(np.std(stage_alpha)),
            }

    # Overall
    if all_flatness:
        results["overall"] = {
            "mean_spectral_flatness": float(np.mean(all_flatness)),
            "std_spectral_flatness": float(np.std(all_flatness)),
            "mean_alpha_ratio": float(np.mean(all_alpha)),
            "std_alpha_ratio": float(np.std(all_alpha)),
            "n_channels": len(all_flatness),
        }

    return results


def _build_signal_streams(
    channels: dict[str, np.ndarray],
    fs_dict: dict[str, int],
    subject_id: str,
    stream_type: str,
    tier: Tier,
    unit: str = "µV",
) -> list[dict[str, Any]]:
    """Build XDF streams for a group of channels."""
    streams = []
    for ch_name, signal in channels.items():
        fs = fs_dict.get(ch_name, 100)
        n_samples = len(signal)

        info = create_stream_info(
            StreamConfig(
                name=f"SYNAPSE_{stream_type}_{subject_id}_{ch_name.replace(' ', '_')}",
                stream_type=f"{stream_type}_T1",
                channel_count=1,
                sampling_rate=fs,
                channel_names=[ch_name],
                channel_units=[unit],
                tier=tier.value,
                metadata={"dataset": "Sleep-EDF", "subject": subject_id, "channel": ch_name},
            )
        )
        timestamps = generate_synthetic_timestamps(n_samples, fs)
        streams.append(
            {
                "info": info,
                "data": signal.reshape(-1, 1).astype(np.float32),
                "timestamps": timestamps.astype(np.float64),
            }
        )
    return streams


def process_sleep_edf_subject(
    psg_file: str,
    data_dir: Path,
    output_dir: Path,
    tier: Tier = Tier.T1,
) -> dict[str, Any]:
    """Process a single Sleep-EDF subject (PSG + Hypnogram pair)."""
    psg_path = data_dir / psg_file
    hypnogram_file = psg_file.replace("PSG", "Hypnogram")
    hypnogram_path = data_dir / hypnogram_file

    if not psg_path.exists() or not hypnogram_path.exists():
        raise FileNotFoundError(f"Missing PSG or hypnogram: {psg_path}, {hypnogram_path}")

    # Load data
    data = load_sleep_edf_record(psg_path, hypnogram_path)

    eeg_channels = data["eeg"]
    eog_channels = data["eog"]
    emg_channels = data["emg"]
    other_channels = data["other"]
    hypnogram = data["hypnogram"]
    hypnogram_times = data["hypnogram_times"]
    fs_dict = data["fs_dict"]
    duration_s = data["duration_s"]
    metadata = data["metadata"]

    # Tier-aware thresholds
    thresholds = QualityThresholds.for_tier(tier)

    # Compute quality metrics
    quality_results = compute_sleep_edf_quality(
        eeg_channels, fs_dict, hypnogram, hypnogram_times, thresholds
    )

    # Build XDF streams
    streams = []

    # EEG, EOG, EMG, Other streams
    streams.extend(
        _build_signal_streams(eeg_channels, fs_dict, metadata["subject_id"], "EEG", tier)
    )
    streams.extend(
        _build_signal_streams(eog_channels, fs_dict, metadata["subject_id"], "EOG", tier)
    )
    streams.extend(
        _build_signal_streams(emg_channels, fs_dict, metadata["subject_id"], "EMG", tier)
    )
    streams.extend(
        _build_signal_streams(
            other_channels, fs_dict, metadata["subject_id"], "Other", tier, "a.u."
        )
    )

    # Hypnogram marker stream (irregular, one marker per 30s epoch)
    hypnogram_markers = []
    for stage_code, epoch_time in zip(hypnogram, hypnogram_times):
        stage_name = STAGE_MAP.get(stage_code, "UNK")
        hypnogram_markers.append((float(epoch_time), f"Stage_{stage_name}"))

    streams.append(
        create_marker_stream(hypnogram_markers, f"SYNAPSE_Hypnogram_{metadata['subject_id']}")
    )

    # Quality metadata stream
    overall_quality = SignalQualityMetrics(
        spectral_flatness=quality_results["overall"].get("mean_spectral_flatness"),
        alpha_band_ratio=quality_results["overall"].get("mean_alpha_ratio"),
        sampling_rate_hz=100,  # Typical EEG fs
        duration_s=duration_s,
        modality="eeg",
        tier=tier,
        thresholds=thresholds,
    )
    overall_quality_dict = overall_quality.to_dict()
    overall_quality_dict["subject_id"] = metadata["subject_id"]
    overall_quality_dict["dataset"] = "Sleep-EDF"
    overall_quality_dict["per_channel"] = quality_results["per_channel"]
    overall_quality_dict["per_stage"] = quality_results["per_stage"]
    overall_quality_dict["hypnogram_distribution"] = {
        STAGE_MAP.get(k, "UNK"): int(v) for k, v in zip(*np.unique(hypnogram, return_counts=True))
    }

    streams.append(
        create_quality_metadata_stream(
            overall_quality_dict, f"SYNAPSE_Metadata_{metadata['subject_id']}"
        )
    )

    # Write XDF
    xdf_path = output_dir / f"{metadata['subject_id']}_sleep_edf.xdf"
    write_xdf(xdf_path, streams)

    # Save per-subject quality JSON
    with open(output_dir / f"{metadata['subject_id']}_quality.json", "w") as f:
        json.dump(
            {
                "subject_id": metadata["subject_id"],
                "xdf_path": str(xdf_path),
                "fs_dict": fs_dict,
                "duration_s": duration_s,
                "quality": quality_results,
                "overall_quality": overall_quality_dict,
                "metadata": metadata,
            },
            f,
            indent=2,
            default=str,
        )

    return {
        "subject_id": metadata["subject_id"],
        "xdf_path": str(xdf_path),
        "fs_dict": fs_dict,
        "duration_s": duration_s,
        "quality": quality_results,
        "overall_quality": overall_quality_dict,
        "hypnogram": hypnogram.tolist(),
        "hypnogram_times": hypnogram_times.tolist(),
        "metadata": metadata,
    }


def ingest_sleep_edf(
    data_dir: Path = Path("data/sleep_edf"),
    output_dir: Path = Path("data/processed"),
    records: list[str] | None = None,
    tier: Tier = Tier.T1,
) -> list[dict]:
    """Full Sleep-EDF Expanded ingestion pipeline with XDF export.

    Args:
        data_dir: Directory for raw Sleep-EDF data
        output_dir: Directory for processed outputs
        records: List of PSG record filenames to process (default: all available)
        tier: Acquisition tier for threshold selection (default: T1 for sleep)

    Returns:
        List of per-subject results with XDF paths and quality metrics
    """
    data_dir = Path(data_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    download_sleep_edf(data_dir)

    # Find available PSG files
    available_psg = sorted([f.name for f in data_dir.glob("*PSG.edf")])

    if records is None:
        records = available_psg
    else:
        # Validate requested records exist
        records = [r for r in records if r in available_psg]

    all_results = []
    for psg_file in tqdm(records, desc="Processing Sleep-EDF subjects"):
        try:
            result = process_sleep_edf_subject(psg_file, data_dir, output_dir, tier)
            all_results.append(result)
        except Exception as e:
            logger.exception(f"Failed to process {psg_file}")

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
