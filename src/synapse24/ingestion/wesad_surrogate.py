"""Deterministic WESAD-like surrogate for pipeline closure.

Architecture.md §51-53 + Roadmap.md §4: UCI WESAD mirrors are dead, so CI
can never download real WESAD. This module provides a seeded,
explicitly-flagged surrogate that emits the EXACT quality_metadata dict
shape consumed by fusion_window_quality_to_features() — proving the
canonical 60s window → 11-feat → GroupKFold ≥80% pipeline end-to-end.

CRITICAL: every artifact produced here carries ``surrogate: True``.
A surrogate pass must NEVER be misreported as real physiology. The
real-data path (process_wesad_subject on canonical data/wesad/WESAD)
is untouched and remains the only source of a non-surrogate gate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from synapse24.ingestion.wesad import (
    FUSION_WINDOW_LABEL_TO_ID,
    WESAD_SUBJECTS,
    FusionWindow,
)

SURROGATE_SEED = 42

# Class-conditional centroids ordered physiologically (Schmidt et al. ICMI
# 2018 direction: stress = faster HR, higher sympathetic drive / LF-HF,
# lower HRV; amusement intermediate; baseline resting). Gaps are large
# relative to seeded noise + per-subject offsets so GroupKFold-by-subject
# generalizes (subject offset ±2% << inter-class gaps of 12-25%).
_SURROGATE_CENTROIDS: dict[str, dict[str, float]] = {
    "baseline": {
        "mean_rr_ms": 800.0,
        "sdnn_ms": 50.0,
        "rmssd_ms": 30.0,
        "pnn50": 10.0,
        "hr_mean_bpm": 75.0,
        "lf_power": 100.0,
        "hf_power": 80.0,
        "lf_hf_ratio": 1.25,
        "ppg_sqi": 0.85,
        "perfusion_index": 3.0,
        "motion_artifact_prob": 0.10,
    },
    "stress": {
        "mean_rr_ms": 600.0,
        "sdnn_ms": 40.0,
        "rmssd_ms": 20.0,
        "pnn50": 5.0,
        "hr_mean_bpm": 100.0,
        "lf_power": 150.0,
        "hf_power": 50.0,
        "lf_hf_ratio": 3.0,
        "ppg_sqi": 0.80,
        "perfusion_index": 2.5,
        "motion_artifact_prob": 0.15,
    },
    "amusement": {
        "mean_rr_ms": 700.0,
        "sdnn_ms": 45.0,
        "rmssd_ms": 25.0,
        "pnn50": 8.0,
        "hr_mean_bpm": 85.0,
        "lf_power": 120.0,
        "hf_power": 70.0,
        "lf_hf_ratio": 1.7,
        "ppg_sqi": 0.82,
        "perfusion_index": 2.8,
        "motion_artifact_prob": 0.12,
    },
}

_SURROGATE_LABEL_IDS = {"baseline": 1, "stress": 2, "amusement": 3}

# Relative per-feature noise (fraction of centroid value); small enough to
# keep classes linearly separable across held-out subjects.
_REL_NOISE = 0.02


def _window_meta(
    label_name: str,
    rng: np.random.Generator,
    subject_offset: float,
    window_idx: int,
) -> dict[str, Any]:
    """Build one canonical fusion-window quality_metadata dict."""
    centroid = _SURROGATE_CENTROIDS[label_name]
    jittered = {
        k: float(v * (1.0 + subject_offset + rng.normal(0.0, _REL_NOISE)))
        for k, v in centroid.items()
    }
    # Clamp probabilities/indices to physiological ranges.
    jittered["ppg_sqi"] = float(np.clip(jittered["ppg_sqi"], 0.0, 1.0))
    jittered["motion_artifact_prob"] = float(np.clip(jittered["motion_artifact_prob"], 0.0, 1.0))
    jittered["pnn50"] = float(np.clip(jittered["pnn50"], 0.0, 100.0))
    jittered["hr_mean_bpm"] = float(np.clip(jittered["hr_mean_bpm"], 30.0, 200.0))
    return {
        "window_idx": window_idx,
        "label": _SURROGATE_LABEL_IDS[label_name],
        "label_name": label_name,
        "duration_s": 60.0,
        "ecg_quality": {
            "metrics": {
                "ecg": {
                    "hrv_metrics": {
                        "mean_rr_ms": jittered["mean_rr_ms"],
                        "sdnn_ms": jittered["sdnn_ms"],
                        "rmssd_ms": jittered["rmssd_ms"],
                        "pnn50": jittered["pnn50"],
                        "hr_mean_bpm": jittered["hr_mean_bpm"],
                        "lf_power": jittered["lf_power"],
                        "hf_power": jittered["hf_power"],
                        "lf_hf_ratio": jittered["lf_hf_ratio"],
                    }
                }
            }
        },
        "ppg_quality": {
            "ppg_sqi": jittered["ppg_sqi"],
            "perfusion_index": jittered["perfusion_index"],
            "motion_artifact_prob": jittered["motion_artifact_prob"],
        },
    }


def generate_surrogate_subject_results(
    n_subjects: int = 6,
    windows_per_class: int = 6,
    seed: int = SURROGATE_SEED,
) -> list[dict[str, Any]]:
    """Generate surrogate per-subject results in process_wesad_subject shape.

    Each result carries ``surrogate: True`` plus ``fusion_windows`` (one
    quality_metadata dict per 60s window) consumable by
    extract_wesad_window_features() / fusion_window_quality_to_features().
    Deterministic in (seed, subject index, window index).
    """
    subjects = WESAD_SUBJECTS[:n_subjects]
    class_names = list(FUSION_WINDOW_LABEL_TO_ID)
    results: list[dict[str, Any]] = []
    for s_idx, subject_id in enumerate(subjects):
        # Per-subject physiological offset (±2%), fixed by seed + index.
        subject_offset = float(np.sin(seed + s_idx * 7.0) * 0.02)
        windows: list[dict[str, Any]] = []
        window_idx = 0
        for label_name in class_names:
            for _ in range(windows_per_class):
                rng = np.random.default_rng(seed * 1000 + s_idx * 100 + window_idx)
                windows.append(_window_meta(label_name, rng, subject_offset, window_idx))
                window_idx += 1
        results.append(
            {
                "subject_id": subject_id,
                "surrogate": True,
                "fusion_windows": windows,
            }
        )
    return results


def surrogate_fusion_windows(
    subject_id: str,
    windows_per_class: int = 6,
    seed: int = SURROGATE_SEED,
) -> list[FusionWindow]:
    """Build FusionWindow objects (with native-rate placeholder signals).

    Quality path only: signals are short deterministic placeholders —
    scoring consumes quality_metadata, never raw surrogate waveforms.
    """
    s_idx = WESAD_SUBJECTS.index(subject_id) if subject_id in WESAD_SUBJECTS else 0
    subject_offset = float(np.sin(seed + s_idx * 7.0) * 0.02)
    class_names = list(FUSION_WINDOW_LABEL_TO_ID)
    windows: list[FusionWindow] = []
    window_idx = 0
    for label_name in class_names:
        for _ in range(windows_per_class):
            rng = np.random.default_rng(seed * 1000 + s_idx * 100 + window_idx)
            meta = _window_meta(label_name, rng, subject_offset, window_idx)
            n_chest = 700 * 10  # 10 s placeholder @700 Hz (XDF proof size)
            t = np.arange(n_chest, dtype=np.float64) / 700.0
            ecg_ph = np.sin(2.0 * np.pi * 1.2 * t).astype(np.float64)
            chest = {
                "ecg": ecg_ph,
                "eda": np.full(n_chest, 2.0),
                "emg": np.zeros(n_chest),
                "resp": np.sin(2.0 * np.pi * 0.25 * t),
                "temp": np.full(n_chest, 36.5),
                "acc_x": np.zeros(n_chest),
                "acc_y": np.zeros(n_chest),
                "acc_z": np.ones(n_chest),
                "labels": np.full(n_chest, meta["label"], dtype=np.int64),
            }
            n_bvp = 64 * 10
            wrist: dict[str, npt.NDArray[np.float64]] = {
                "bvp": np.sin(2.0 * np.pi * 1.2 * np.arange(n_bvp) / 64.0),
                "eda": np.full(320, 1.5),
                "temp": np.full(320, 36.0),
                "acc_x": np.zeros(320),
                "acc_y": np.zeros(320),
                "acc_z": np.ones(320),
            }
            w = FusionWindow(
                subject_id=subject_id,
                window_idx=window_idx,
                start_time_s=float(window_idx * 60),
                end_time_s=float(window_idx * 60 + 60),
                label=int(meta["label"]),
                label_name=label_name,
                chest_signals=chest,
                wrist_signals=wrist,
            )
            w.quality_metadata = meta
            windows.append(w)
            window_idx += 1
    return windows


def write_surrogate_xdf(
    path: Path,
    subject_id: str = "S2",
    seed: int = SURROGATE_SEED,
) -> Path:
    """Write a small multi-stream surrogate XDF (zero-drop sync proof)."""
    from synapse24.utils import (
        create_marker_stream,
        create_stream_info_from_dict,
        generate_synthetic_timestamps,
        write_xdf,
    )

    path = Path(path)
    rng = np.random.default_rng(seed)
    fs_ecg, fs_ppg, fs_acc = 100.0, 64.0, 32.0
    n_ecg, n_ppg, n_acc = 1000, 640, 320
    ecg = (
        rng.standard_normal(n_ecg) * 0.05 + np.sin(2 * np.pi * 1.2 * np.arange(n_ecg) / fs_ecg)
    ).astype(np.float32)
    ppg = np.sin(2 * np.pi * 1.2 * np.arange(n_ppg) / fs_ppg).astype(np.float32)
    acc = np.column_stack([rng.standard_normal(n_acc) * 0.01] * 3).astype(np.float32)

    def _stream(
        name: str,
        stream_type: str,
        data: npt.NDArray[np.float32],
        fs: float,
        channels: list[str],
        units: list[str],
    ) -> dict[str, Any]:
        return {
            "info": create_stream_info_from_dict(
                {
                    "name": name,
                    "type": stream_type,
                    "channel_count": len(channels),
                    "sampling_rate": fs,
                    "channel_names": channels,
                    "channel_units": units,
                    "tier": 1,
                    "metadata": {"dataset": "WESAD-surrogate", "subject": subject_id},
                }
            ),
            "data": data.reshape(-1, len(channels)),
            "timestamps": generate_synthetic_timestamps(len(data), fs).astype(np.float64),
        }

    streams: list[dict[str, Any]] = [
        _stream(f"SYNAPSE_ECG_SURROGATE_{subject_id}", "ECG_T1", ecg, fs_ecg, ["ECG"], ["µV"]),
        _stream(f"SYNAPSE_PPG_SURROGATE_{subject_id}", "PPG_T0", ppg, fs_ppg, ["BVP"], ["a.u."]),
        _stream(
            f"SYNAPSE_ACC_SURROGATE_{subject_id}",
            "ACC_T0",
            acc,
            fs_acc,
            ["ACC_X", "ACC_Y", "ACC_Z"],
            ["g", "g", "g"],
        ),
        create_marker_stream(
            [(0.0, "baseline"), (60.0, "stress")], f"SYNAPSE_Markers_{subject_id}"
        ),
    ]
    write_xdf(path, streams)
    return path
