"""WESAD dataset ingestion and preprocessing pipeline with LSL/XDF export.

Supports multiple data sources:
- Original WESAD (UCI/author mirrors) - if available
- ISPAAD (Zenodo) - alternative multimodal stress dataset with EDA, BVP, ACC
- Local cached data - if already downloaded
"""

from __future__ import annotations

import json
import logging
import pickle
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
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

# Original WESAD URLs (often unavailable)
WESAD_URLS = [
    "https://archive.ics.uci.edu/dataset/465/wesad.zip",
    "https://archive.ics.uci.edu/static/public/465/wesad.zip",
]
# ISPAAD from Zenodo - alternative with similar modalities (EDA, BVP/PPG, ACC)
ISPAAD_ZENODO_URL = "https://zenodo.org/records/16842625/files/eda.csv, https://zenodo.org/records/16842625/files/bvp.csv, https://zenodo.org/records/16842625/files/acc.csv"
WESAD_SUBJECTS = [f"S{i}" for i in range(2, 18) if i != 12]  # S12 missing

logger = logging.getLogger(__name__)


def download_wesad(data_dir: Path) -> Path | None:
    """Download and extract WESAD dataset from available mirrors.

    Returns Path to extracted data directory, or None if all mirrors fail.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)

    extract_dir = data_dir / "WESAD"
    if extract_dir.exists() and any(extract_dir.iterdir()):
        logger.info("WESAD already cached at %s", extract_dir)
        return extract_dir

    # Try each mirror
    for url in WESAD_URLS:
        try:
            logger.info("Trying WESAD mirror: %s", url)
            response = requests.get(url, stream=True, timeout=30)
            if response.status_code == 200:
                content_type = response.headers.get("content-type", "")
                if "zip" in content_type or "octet-stream" in content_type:
                    zip_path = data_dir / "WESAD.zip"
                    total_size = int(response.headers.get("content-length", 0))
                    with (
                        open(zip_path, "wb") as f,
                        tqdm(total=total_size, unit="B", unit_scale=True, desc="WESAD") as pbar,
                    ):
                        for chunk in response.iter_content(chunk_size=8192):
                            f.write(chunk)
                            pbar.update(len(chunk))

                    with zipfile.ZipFile(zip_path, "r") as zf:
                        zf.extractall(data_dir)
                    zip_path.unlink()
                    logger.info("WESAD downloaded and extracted to %s", extract_dir)
                    return extract_dir
                logger.warning("Mirror returned non-zip content: %s", content_type)
            else:
                logger.warning("Mirror returned status %s", response.status_code)
        except Exception as e:
            logger.warning("Mirror failed: %s", e)

    logger.warning("All WESAD mirrors failed. Dataset will be skipped.")
    logger.warning("To use WESAD, manually download from https://archive.ics.uci.edu/dataset/465")
    logger.warning("  and place in data/wesad/WESAD/")
    return None


def load_wesad_subject(subject_dir: Path) -> dict[str, Any]:
    """Load a single WESAD subject's pickle file."""
    pkl_files = list(subject_dir.glob("*.pkl"))
    if not pkl_files:
        raise FileNotFoundError(f"No pickle file in {subject_dir}")

    with open(pkl_files[0], "rb") as f:
        return dict(pickle.load(f, encoding="latin1"))


def extract_chest_signals(
    data: dict[str, Any],
) -> dict[str, npt.NDArray[np.float64] | npt.NDArray[np.int64]]:
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
        "labels": data["label"].flatten().astype(np.int64),
    }


def extract_wrist_signals(data: dict[str, Any]) -> dict[str, npt.NDArray[np.float64]]:
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


def resample_labels(
    labels: npt.NDArray[np.int64], original_rate: int, target_rate: int
) -> npt.NDArray[np.int64]:
    """Resample labels to target rate using nearest neighbor."""
    if original_rate == target_rate:
        return labels
    ratio = target_rate / original_rate
    indices = np.arange(0, len(labels) * ratio, ratio).astype(int)
    indices = np.clip(indices, 0, len(labels) - 1)
    return np.asarray(labels[indices])


def compute_accel_magnitude(
    acc_x: npt.NDArray[np.float64], acc_y: npt.NDArray[np.float64], acc_z: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64]:
    """Compute 3D accelerometer magnitude."""
    return np.asarray(np.sqrt(acc_x**2 + acc_y**2 + acc_z**2))


def segment_by_label(
    signals: Mapping[str, npt.NDArray[np.float64] | npt.NDArray[np.int64]],
    labels: npt.NDArray[np.int64],
    label_value: int,
) -> dict[str, npt.NDArray[np.float64]]:
    """Extract signal segments for a specific label."""
    mask = labels == label_value
    return {k: v[mask] for k, v in signals.items()}


def _create_stream(
    name: str,
    stream_type: str,
    data: npt.NDArray[np.float64],
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
    chest: dict[str, npt.NDArray[np.float64] | npt.NDArray[np.int64]],
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
            chest["ecg"].astype(np.float64),
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
            chest["eda"].astype(np.float64),
            fs_chest,
            ["EDA"],
            ["µS"],
            tier.value,
            metadata,
        )
    )

    acc_data = np.column_stack(
        [
            chest["acc_x"].astype(np.float64),
            chest["acc_y"].astype(np.float64),
            chest["acc_z"].astype(np.float64),
        ]
    )
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
            chest["resp"].astype(np.float64),
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
            chest["temp"].astype(np.float64),
            fs_chest,
            ["TEMP"],
            ["°C"],
            tier.value,
            metadata,
        )
    )

    return streams


def _build_wrist_streams(
    wrist: dict[str, npt.NDArray[np.float64]],
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
    chest: dict[str, npt.NDArray[np.float64] | npt.NDArray[np.int64]],
    ecg_timestamps: npt.NDArray[np.float64],
    fs_chest: int,
    subject_id: str,
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
    chest: dict[str, npt.NDArray[np.float64] | npt.NDArray[np.int64]],
    wrist: dict[str, npt.NDArray[np.float64]],
    wrist_acc_mag: npt.NDArray[np.float64],
    fs_chest: int,
    fs_wrist_bvp: int,
    thresholds: QualityThresholds,
) -> dict[str, dict[str, Any]]:
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

    segment_qualities: dict[str, dict[str, Any]] = {}
    # Labels are int64, but mypy sees union type - cast explicitly
    chest_labels = chest["labels"]
    assert isinstance(chest_labels, np.ndarray)
    chest_labels_int: npt.NDArray[np.int64] = chest_labels.astype(np.int64, copy=False)
    for label_val, label_name in label_names.items():
        chest_seg = segment_by_label(chest, chest_labels_int, label_val)
        if len(chest_seg["ecg"]) > fs_chest * 10:
            seg_ecg = compute_ecg_quality(
                chest_seg["ecg"].astype(np.float64), fs_chest, thresholds=thresholds
            )
            chest_acc_mag = compute_accel_magnitude(
                chest_seg["acc_x"].astype(np.float64),
                chest_seg["acc_y"].astype(np.float64),
                chest_seg["acc_z"].astype(np.float64),
            )
            wrist_seg = segment_by_label(
                wrist,
                resample_labels(chest_labels_int, fs_chest, fs_wrist_bvp),
                label_val,
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


@dataclass
class FusionWindow:
    """A 60-second native-rate fusion window with per-modality quality metrics.

    Architecture.md §33-43: Tiered acquisition - fusion happens at feature level
    on label-stationary 60s windows. No raw resampling across modalities.
    """

    subject_id: str
    window_idx: int
    start_time_s: float
    end_time_s: float
    label: int  # 1=baseline, 2=stress, 3=amusement, etc.
    label_name: str
    chest_signals: dict[str, npt.NDArray[np.float64] | npt.NDArray[np.int64]]
    wrist_signals: dict[str, npt.NDArray[np.float64]]
    chest_fs: int = 700
    wrist_bvp_fs: int = 64
    wrist_acc_fs: int = 32
    ecg_quality: dict[str, Any] | None = None
    ppg_quality: dict[str, Any] | None = None
    quality_metadata: dict[str, Any] | None = None


def extract_native_rate_fusion_windows(
    chest: dict[str, npt.NDArray[np.float64] | npt.NDArray[np.int64]],
    wrist: dict[str, npt.NDArray[np.float64]],
    window_s: float = 60.0,
    overlap_s: float = 0.0,
    min_label_purity: float = 0.9,
) -> list[FusionWindow]:
    """Extract 60s native-rate fusion windows from WESAD chest + wrist signals.

    Architecture Decision (from Principal Architect review):
    - NO raw resampling across modalities (ECG 700Hz → 64Hz destroys HRV RMSSD precision)
    - Feature-level fusion on label-stationary windows (Schmidt et al. WESAD benchmark)
    - Each window preserves native rates: chest 700Hz, wrist BVP 64Hz, wrist ACC 32Hz, EDA/Temp 4Hz
    - Labels from chest stream (700Hz resolution) define window boundaries
    - Overlap_s=0 for validation (no label leakage); overlap_s=30 for inference only

    Args:
        chest: Chest signals from extract_chest_signals (700 Hz, includes labels)
        wrist: Wrist signals from extract_wrist_signals (64/32/4 Hz)
        window_s: Window duration in seconds (default 60s per WESAD protocol)
        overlap_s: Overlap in seconds (0 for validation, 30 for inference)
        min_label_purity: Minimum fraction of window with same label (default 0.9)

    Returns:
        List of FusionWindow objects with native-rate signals + per-window quality
    """
    fs_chest = 700
    fs_wrist_bvp = 64
    fs_wrist_acc = 32

    labels_raw = chest["labels"]
    assert isinstance(labels_raw, np.ndarray)
    labels: npt.NDArray[np.int64] = labels_raw.astype(np.int64, copy=False)
    n_chest = len(labels)

    window_samples_chest = int(window_s * fs_chest)
    step_samples_chest = int((window_s - overlap_s) * fs_chest)

    if step_samples_chest <= 0:
        raise ValueError(f"overlap_s ({overlap_s}) must be < window_s ({window_s})")

    label_names = {
        1: "baseline",
        2: "stress",
        3: "amusement",
        4: "meditation",
        5: "recovery",
        6: "fun",
        7: "rest",
    }

    windows = []
    window_idx = 0

    for start_idx in range(0, n_chest - window_samples_chest + 1, step_samples_chest):
        end_idx = start_idx + window_samples_chest
        window_labels = labels[start_idx:end_idx]

        # Check label purity - must be dominated by one label
        unique, counts = np.unique(window_labels, return_counts=True)
        if len(counts) == 0:
            continue
        dominant_label = unique[np.argmax(counts)]
        purity = counts.max() / counts.sum()

        if purity < min_label_purity:
            continue  # Skip mixed-label windows

        label_name = label_names.get(dominant_label, "unknown")

        # Extract chest signals for this window (native 700 Hz)
        chest_signals = {k: v[start_idx:end_idx] for k, v in chest.items()}

        # Compute corresponding wrist window boundaries
        # Wrist BVP is at 64 Hz, chest at 700 Hz
        wrist_start = int(start_idx * fs_wrist_bvp / fs_chest)
        wrist_end = int(end_idx * fs_wrist_bvp / fs_chest)
        wrist_signals_window = {
            k: v[wrist_start:wrist_end] for k, v in wrist.items() if k in ("bvp",)
        }

        # Wrist ACC at 32 Hz
        acc_start = int(start_idx * fs_wrist_acc / fs_chest)
        acc_end = int(end_idx * fs_wrist_acc / fs_chest)
        wrist_signals_window["acc_x"] = wrist["acc_x"][acc_start:acc_end]
        wrist_signals_window["acc_y"] = wrist["acc_y"][acc_start:acc_end]
        wrist_signals_window["acc_z"] = wrist["acc_z"][acc_start:acc_end]

        # Wrist EDA/Temp at 4 Hz
        fs_wrist_eda = 4
        eda_start = int(start_idx * fs_wrist_eda / fs_chest)
        eda_end = int(end_idx * fs_wrist_eda / fs_chest)
        wrist_signals_window["eda"] = wrist["eda"][eda_start:eda_end]
        wrist_signals_window["temp"] = wrist["temp"][eda_start:eda_end]

        # Start/end time in seconds (from chest timestamps)
        start_time_s = start_idx / fs_chest
        end_time_s = end_idx / fs_chest

        windows.append(
            FusionWindow(
                subject_id="",  # Set by caller
                window_idx=window_idx,
                start_time_s=start_time_s,
                end_time_s=end_time_s,
                label=int(dominant_label),
                label_name=label_name,
                chest_signals=chest_signals,
                wrist_signals=wrist_signals_window,
                chest_fs=fs_chest,
                wrist_bvp_fs=fs_wrist_bvp,
                wrist_acc_fs=fs_wrist_acc,
            )
        )

        window_idx += 1

    return windows


def extract_native_rate_fusion_windows_for_subject(
    subject_id: str,
    data_dir: Path,
    window_s: float = 60.0,
    overlap_s: float = 0.0,
) -> list[FusionWindow]:
    """Convenience function: load subject + extract fusion windows in one call."""
    subject_dir = data_dir / subject_id
    data = load_wesad_subject(subject_dir)
    chest = extract_chest_signals(data)
    wrist = extract_wrist_signals(data)
    windows = extract_native_rate_fusion_windows(chest, wrist, window_s, overlap_s)
    for w in windows:
        w.subject_id = subject_id
    return windows


def process_wesad_subject(
    subject_id: str,
    data_dir: Path,
    output_dir: Path,
    tier: Tier = Tier.T1,
) -> dict[str, Any]:
    """Process a single WESAD subject and compute quality metrics with XDF export.

    WESAD chest data (RespiBAN) represents research-grade resting/stress recordings
    → Tier 1 (high-density, clean context) thresholds apply.

    Adds native-rate 60s fusion windows (overlap_s=0 for validation) as
    SYNAPSE_FusionWindow markers with per-window quality metadata.
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
    ecg_quality = compute_ecg_quality(
        chest["ecg"].astype(np.float64), fs_chest, thresholds=thresholds
    )
    ppg_quality = compute_ppg_quality(
        wrist["bvp"], fs_wrist_bvp, wrist_acc_mag, thresholds=thresholds
    )

    # --- NATIVE-RATE FUSION WINDOWS (60s, overlap=0 for validation) ---
    fusion_windows = extract_native_rate_fusion_windows(
        chest, wrist, window_s=60.0, overlap_s=0.0, min_label_purity=0.9
    )
    for w in fusion_windows:
        w.subject_id = subject_id

    # Compute per-window quality metrics
    from synapse24.signal_quality import (
        compute_ppg_sqi,
        ppg_motion_artifact_probability,
        r_peak_detection_quality,
    )

    for w in fusion_windows:
        # ECG quality on this window
        seg_ecg = compute_ecg_quality(
            w.chest_signals["ecg"].astype(np.float64), fs_chest, thresholds=thresholds
        )
        w.ecg_quality = seg_ecg.to_dict()

        # PPG quality on this window
        chest_acc_mag = compute_accel_magnitude(
            w.chest_signals["acc_x"].astype(np.float64),
            w.chest_signals["acc_y"].astype(np.float64),
            w.chest_signals["acc_z"].astype(np.float64),
        )
        wrist_acc_seg = np.sqrt(
            w.wrist_signals["acc_x"] ** 2
            + w.wrist_signals["acc_y"] ** 2
            + w.wrist_signals["acc_z"] ** 2
        )
        seg_ppg = compute_ppg_quality(
            w.wrist_signals["bvp"], fs_wrist_bvp, wrist_acc_seg, thresholds=thresholds
        )
        w.ppg_quality = seg_ppg

        # Quality metadata for XDF
        w.quality_metadata = {
            "window_idx": w.window_idx,
            "start_time_s": w.start_time_s,
            "end_time_s": w.end_time_s,
            "label": w.label,
            "label_name": w.label_name,
            "duration_s": w.end_time_s - w.start_time_s,
            "ecg_quality": w.ecg_quality,
            "ppg_quality": w.ppg_quality,
        }

    # Build all streams
    streams = []

    # Chest streams
    streams.extend(_build_chest_streams(chest, subject_id, tier, fs_chest))

    # Wrist streams
    streams.extend(_build_wrist_streams(wrist, subject_id, fs_wrist_bvp, fs_wrist_acc))

    # ECG timestamps for marker alignment
    ecg_timestamps = generate_synthetic_timestamps(len(chest["ecg"]), fs_chest)

    # Marker stream (activity labels)
    streams.append(_build_marker_stream(chest, ecg_timestamps, fs_chest, subject_id))

    # Fusion window markers (60s windows with quality metadata)
    from synapse24.utils import create_marker_stream

    fusion_markers = [
        (w.start_time_s + ecg_timestamps[0], f"fusion_window:{w.window_idx}:{w.label_name}")
        for w in fusion_windows
    ]
    streams.append(create_marker_stream(fusion_markers, f"SYNAPSE_FusionWindows_{subject_id}"))

    # Segment qualities (existing label-based segments)
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

    # Per-window fusion quality metadata
    fusion_quality_dict = {
        "dataset": "WESAD",
        "subject_id": subject_id,
        "fusion_windows": [w.quality_metadata for w in fusion_windows],
        "window_config": {"window_s": 60.0, "overlap_s": 0.0, "min_label_purity": 0.9},
    }
    streams.append(
        create_quality_metadata_stream(fusion_quality_dict, f"SYNAPSE_FusionQuality_{subject_id}")
    )

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
        "fusion_windows": [w.quality_metadata for w in fusion_windows],
        "overall_quality": overall_quality_dict,
    }


def ingest_wesad(
    data_dir: Path = Path("data/wesad"),
    output_dir: Path = Path("data/processed"),
    subjects: list[str] | None = None,
    tier: Tier = Tier.T1,
) -> list[dict[str, Any]]:
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

    extract_dir = download_wesad(data_dir)
    if extract_dir is None:
        logger.warning("WESAD dataset not available locally or via mirrors.")
        logger.warning(
            "Skipping WESAD ingestion. Run with --dataset mitbih or --dataset sleep_edf instead."
        )
        return []

    if subjects is None:
        subjects = WESAD_SUBJECTS

    all_results = []
    for subject_id in tqdm(subjects, desc="Processing WESAD subjects"):
        try:
            result = process_wesad_subject(subject_id, extract_dir, output_dir, tier)
            all_results.append(result)

            # Save per-subject quality JSON (backward compatibility)
            with open(output_dir / f"{subject_id}_quality.json", "w") as f:
                json.dump(result, f, indent=2, default=str)

        except Exception as e:
            logger.warning("Failed to process %s: %s", subject_id, e)

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
