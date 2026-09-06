"""WESAD 11-feat int8 quantization closure (TF-free).

Architecture.md §49-53 (edge triage + multimodal fusion), Roadmap.md §4
(Edge Impulse → TFLM → ESP32 loop): ties the proven FP32 fusion accuracy
(≥80% GroupKFold-by-subject, Schmidt et al. ICMI 2018) to the proven int8
exit-gate envelope on the SAME canonical 11-feat distribution emitted by
fusion_window_quality_to_features().

TF-free by design: full TFLite conversion is covered by test_edge_ai.py
(which skips without TensorFlow). This module measures what conversion
would cost — deterministic per-feature int8 round-trip (min-max scale →
round → dequantize) applied to the closure matrix, then re-scores the
identical GroupKFold pipeline. The resulting accuracy_drop_pp feeds
check_phase0_exit_gate() directly.

Surrogate policy: runs with surrogate=True end-to-end are valid CI gates
(UCI WESAD mirrors are dead). Only cached canonical data/wesad/WESAD may
produce surrogate=False.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
from sklearn.model_selection import GroupKFold, cross_val_score
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from synapse24.edge_ai.deployment import PHASE0_EXIT_GATE, check_phase0_exit_gate
from synapse24.edge_ai.model import TargetPlatform
from synapse24.edge_ai.quantization import estimate_inference_latency
from synapse24.ingestion.wesad import (
    FUSION_WINDOW_FEATURE_NAMES,
    fusion_window_quality_to_features,
)

FEATURE_SOURCE = "fusion_windows_60s_int8sim"

_TRIAGE_HIDDEN = (32, 16)
_MLP_MAX_ITER = 800
_MLP_SEED = 42


@dataclass
class ClosureMatrix:
    """Canonical (n_windows, 11) int8-closure matrix with subject groups."""

    X: npt.NDArray[np.float64]
    y: npt.NDArray[np.int64]
    groups: list[str]
    all_surrogate: bool
    feature_names: list[str]
    n_subjects: int


def build_closure_matrix(wesad_results: list[dict[str, Any]]) -> ClosureMatrix:
    """Map per-subject fusion windows to the canonical closure matrix.

    Consumes the exact quality_metadata dict shape produced by
    process_wesad_subject() and the deterministic surrogate. Windows with
    non-3-class labels (e.g. meditation) are skipped via the canonical
    fusion_window_quality_to_features() None contract.

    Raises:
        ValueError: No valid 3-class fusion windows found.
    """
    features: list[list[float]] = []
    labels: list[int] = []
    groups: list[str] = []
    contributing_surrogate: list[bool] = []

    for result in wesad_results:
        subject_id = str(result.get("subject_id", "unknown"))
        windows = result.get("fusion_windows", [])
        if not isinstance(windows, list):
            continue
        contributed = False
        for window_meta in windows:
            if not isinstance(window_meta, dict):
                continue
            parsed = fusion_window_quality_to_features(window_meta)
            if parsed is None:
                continue
            feats, label_id = parsed
            features.append([float(v) for v in feats])
            labels.append(int(label_id))
            groups.append(subject_id)
            contributed = True
        if contributed:
            contributing_surrogate.append(bool(result.get("surrogate", False)))

    if not features:
        raise ValueError("No valid 3-class fusion windows found for int8 closure")

    all_surrogate = bool(contributing_surrogate) and all(contributing_surrogate)
    return ClosureMatrix(
        X=np.asarray(features, dtype=np.float64),
        y=np.asarray(labels, dtype=np.int64),
        groups=groups,
        all_surrogate=all_surrogate,
        feature_names=list(FUSION_WINDOW_FEATURE_NAMES),
        n_subjects=len(set(groups)),
    )


def _mlp_pipeline() -> Pipeline:
    """Tiny triage MLP pipeline (11 feats → 32 → 16 → 3 classes)."""
    return Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "mlp",
                MLPClassifier(
                    hidden_layer_sizes=_TRIAGE_HIDDEN,
                    activation="relu",
                    max_iter=_MLP_MAX_ITER,
                    random_state=_MLP_SEED,
                ),
            ),
        ]
    )


def _effective_splits(groups: list[str], requested: int = 3) -> int:
    """GroupKFold splits bounded by subject count (minimum 2)."""
    n_groups = len(set(groups))
    if n_groups < 2:
        raise ValueError("int8 closure requires at least 2 subjects for GroupKFold")
    return min(requested, n_groups)


def groupkfold_scores(
    features: npt.NDArray[np.float64],
    labels: npt.NDArray[np.int64],
    groups: list[str],
    n_splits: int = 3,
) -> list[float]:
    """FP32-equivalent GroupKFold accuracy (pipeline with in-fold scaling)."""
    splits = _effective_splits(groups, n_splits)
    cv = GroupKFold(n_splits=splits)
    scores = cross_val_score(
        _mlp_pipeline(), features, labels, groups=groups, cv=cv, scoring="accuracy"
    )
    return [float(s) for s in scores]


def simulate_int8_roundtrip(features: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
    """Deterministic per-feature int8 calibration-noise simulation.

    Each feature column is min-max scaled to [-128, 127], rounded (the int8
    quantization step), then dequantized back. Constant columns (zero range)
    map to zeros. This is the TF-free stand-in for post-training int8
    calibration error on the 11-feat distribution.
    """
    X = np.asarray(features, dtype=np.float64)
    out = np.empty_like(X)
    for j in range(X.shape[1]):
        col = X[:, j]
        lo, hi = float(np.min(col)), float(np.max(col))
        if hi <= lo:
            out[:, j] = 0.0
            continue
        scaled = (col - lo) / (hi - lo) * 255.0 - 128.0
        quantized = np.round(scaled)
        out[:, j] = (quantized + 128.0) / 255.0 * (hi - lo) + lo
    return out


def estimate_triage_footprint(
    n_features: int = 11,
    hidden: tuple[int, int] = _TRIAGE_HIDDEN,
    num_classes: int = 3,
) -> dict[str, float]:
    """Deterministic int8 footprint for the triage MLP (1 byte/param).

    Returns model_size_kb, estimated_ram_kb (weights ×2 + activation
    buffer) and estimated_latency_ms on ESP32-S3.
    """
    h1, h2 = hidden
    params = n_features * h1 + h1 + h1 * h2 + h2 + h2 * num_classes + num_classes
    model_size_kb = params / 1024.0
    activation_kb = (h1 + h2 + num_classes) * 4.0 / 1024.0
    ram_kb = model_size_kb * 2.0 + activation_kb
    latency_ms = estimate_inference_latency(model_size_kb, TargetPlatform.ESP32_S3)
    return {
        "model_size_kb": float(model_size_kb),
        "estimated_ram_kb": float(ram_kb),
        "estimated_latency_ms": float(latency_ms),
    }


def run_wesad_int8_closure(
    wesad_results: list[dict[str, Any]],
    profile: str = "triage",
    n_splits: int = 3,
) -> dict[str, Any]:
    """Run FP32 GroupKFold → int8 simulation → exit-gate check.

    Args:
        wesad_results: Per-subject results with fusion_windows.
        profile: triage (ESP32-S3) or hub_fusion (Pi hub).
        n_splits: Requested GroupKFold splits (bounded by subject count).

    Returns:
        Dict with accuracy, per_fold_scores, accuracy_int8,
        accuracy_drop_pp, footprint, gate, surrogate, feature_source.

    Raises:
        ValueError: Unknown profile or no valid fusion windows.
    """
    if profile not in PHASE0_EXIT_GATE:
        valid = sorted(PHASE0_EXIT_GATE)
        raise ValueError(f"Unknown exit-gate profile '{profile}'. Valid: {valid}")

    matrix = build_closure_matrix(wesad_results)
    splits = _effective_splits(matrix.groups, n_splits)

    fp32_scores = groupkfold_scores(matrix.X, matrix.y, matrix.groups, splits)
    X_int8 = simulate_int8_roundtrip(matrix.X)
    int8_scores = groupkfold_scores(X_int8, matrix.y, matrix.groups, splits)

    acc_fp32 = float(np.mean(fp32_scores))
    acc_int8 = float(np.mean(int8_scores))
    drop_pp = (acc_fp32 - acc_int8) * 100.0

    footprint = estimate_triage_footprint(
        n_features=len(matrix.feature_names),
        hidden=_TRIAGE_HIDDEN,
        num_classes=len({int(v) for v in matrix.y}),
    )
    gate = check_phase0_exit_gate(
        model_size_kb=footprint["model_size_kb"],
        estimated_ram_kb=footprint["estimated_ram_kb"],
        estimated_latency_ms=footprint["estimated_latency_ms"],
        accuracy_drop_percent=drop_pp,
        ops_used=["FULLY_CONNECTED", "RELU", "SOFTMAX", "QUANTIZE"],
        profile=profile,
    )

    return {
        "accuracy": acc_fp32,
        "accuracy_int8": acc_int8,
        "accuracy_drop_pp": float(drop_pp),
        "per_fold_scores": fp32_scores,
        "per_fold_scores_int8": int8_scores,
        "n_splits": splits,
        "n_subjects": matrix.n_subjects,
        "model_size_kb": footprint["model_size_kb"],
        "estimated_ram_kb": footprint["estimated_ram_kb"],
        "estimated_latency_ms": footprint["estimated_latency_ms"],
        "gate": gate,
        "surrogate": matrix.all_surrogate,
        "feature_source": FEATURE_SOURCE,
    }
