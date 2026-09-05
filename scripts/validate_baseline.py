#!/usr/bin/env python3
"""Baseline validation against published results.

Reproduces:
1. WESAD 3-class stress classification (ECG+EDA+ACC) - target ≥80% accuracy
2. MIT-BIH R-peak detection - target Se ≥99.6%, PPV ≥99.6%
3. Sleep-EDF sleep staging - target Cohen's κ ≥0.75 vs. gold standard

Architecture Decision (Principal Architect):
- Deterministic seeding: np.random.seed(42), RandomForest(random_state=42)
- Subject-grouped CV for WESAD (no subject leakage across folds)
- Per-fold scores reported in baseline_report.json
- RMSSD MAE target: <5 ms (full set), <2 ms on clean records
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import cohen_kappa_score
from sklearn.model_selection import GroupKFold, StratifiedKFold, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from synapse24.ingestion import Tier, ingest_mitbih, ingest_sleep_edf, ingest_wesad
from synapse24.ingestion.wesad import FUSION_WINDOW_CONFIG, fusion_window_quality_to_features
from synapse24.signal_quality import validate_sleep_staging_against_gold
from synapse24.utils import validate_xdf

# Global deterministic seed for Phase 0 exit gate reproducibility
SEED = 42
np.random.seed(SEED)

BASELINE_REPORT_SCHEMA_VERSION = "1.0"

BASELINE_DATASET_ALIASES: dict[str, list[str]] = {
    "wesad": ["wesad"],
    "mitbih": ["mitbih"],
    "sleep_edf": ["sleep_edf"],
    # README + CI contract: "both" = peripheral pair (excludes Sleep-EDF).
    "both": ["wesad", "mitbih"],
    "all": ["wesad", "mitbih", "sleep_edf"],
}
"""Dataset selector aliases (Roadmap.md §4: WESAD + MIT-BIH are the Phase 0 pair)."""


def resolve_baseline_datasets(name: str) -> list[str]:
    """Resolve a --dataset selector to concrete dataset keys.

    Raises:
        ValueError: Unknown selector (fail fast — an empty run must never
            silently pass the exit gate).
    """
    try:
        return list(BASELINE_DATASET_ALIASES[name])
    except KeyError:
        valid = sorted(BASELINE_DATASET_ALIASES)
        raise ValueError(f"Unknown dataset '{name}'. Valid: {valid}") from None


def validate_baseline_report_schema(report: dict[str, Any]) -> bool:
    """Enforce baseline_report.json schema v1.0 (fail fast on contract drift).

    Required top-level keys: seed, timestamp, datasets, xdf_validation,
    schema_version == "1.0". The WESAD block must carry per_fold_scores
    (fold variance must never be averaged away silently). The Sleep-EDF
    block, when present (--dataset all), must carry mean_cohen_kappa,
    mean_accuracy, n_subjects and target_met (Fpz-Cz YASA vs PSG gate).

    Raises:
        ValueError: On any schema violation.
    """
    required = ("seed", "timestamp", "datasets", "xdf_validation", "schema_version")
    for key in required:
        if key not in report:
            raise ValueError(f"baseline_report.json missing required key: '{key}'")

    if report["schema_version"] != BASELINE_REPORT_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported schema_version '{report['schema_version']}' "
            f"(expected '{BASELINE_REPORT_SCHEMA_VERSION}')"
        )

    datasets = report["datasets"]
    if not isinstance(datasets, dict) or not datasets:
        raise ValueError("baseline_report.json 'datasets' must be a non-empty mapping")

    if "wesad" in datasets:
        wesad = datasets["wesad"]
        if "error" in wesad:
            # Explicit failure blocks are schema-valid (failure stays visible,
            # exit code is driven by target_met flags, not by a crash here).
            return True
        for key in ("accuracy", "per_fold_scores", "n_splits", "n_subjects"):
            if key not in wesad:
                raise ValueError(f"WESAD block missing required key: '{key}'")
        scores = wesad["per_fold_scores"]
        if not isinstance(scores, list) or not scores:
            raise ValueError("WESAD 'per_fold_scores' must be a non-empty list")
        if int(wesad["n_splits"]) != len(scores):
            raise ValueError("WESAD 'n_splits' must match len('per_fold_scores')")

    if "sleep_edf" in datasets:
        sleep_edf = datasets["sleep_edf"]
        if "error" in sleep_edf:
            # Explicit failure blocks are schema-valid (failure stays visible,
            # exit code is driven by target_met flags, not by a crash here).
            return True
        for key in ("mean_cohen_kappa", "mean_accuracy", "n_subjects", "target_met"):
            if key not in sleep_edf:
                raise ValueError(f"Sleep-EDF block missing required key: '{key}'")

    xdf = report["xdf_validation"]
    for key in ("files_validated", "files_failed"):
        if key not in xdf:
            raise ValueError(f"xdf_validation missing required key: '{key}'")

    return True


def extract_wesad_features(
    subject_result: dict,
) -> tuple[np.ndarray, np.ndarray, str] | None:
    """Legacy segment-level feature extraction (deprecated fallback).

    Prefer canonical 60s fusion windows via extract_wesad_window_features().
    Kept only for cached JSON without fusion_windows. Uses segments:
    baseline vs stress vs amusement for 3-class.
    Returns (features, labels, subject_id) for subject-grouped CV.
    """
    segments = subject_result.get("segments", {})

    # We need baseline, stress, and amusement
    required = ["baseline", "stress", "amusement"]
    if not all(s in segments for s in required):
        return None

    features_list = []
    labels_list = []

    label_map = {"baseline": 0, "stress": 1, "amusement": 2}

    for label_name, label_id in label_map.items():
        seg = segments[label_name]
        ecg_q = seg["ecg_quality"]
        ppg_q = seg["ppg_quality"]

        # Extract HRV features from ECG quality
        hrv = ecg_q.get("metrics", {}).get("ecg", {}).get("hrv_metrics", {})

        feat = [
            hrv.get("mean_rr_ms", 0),
            hrv.get("sdnn_ms", 0),
            hrv.get("rmssd_ms", 0),
            hrv.get("pnn50", 0),
            hrv.get("hr_mean_bpm", 0),
            hrv.get("lf_power", 0),
            hrv.get("hf_power", 0),
            hrv.get("lf_hf_ratio", 0),
            ppg_q.get("ppg_sqi", 0),
            ppg_q.get("perfusion_index", 0),
            ppg_q.get("motion_artifact_prob", 0),
        ]
        features_list.append(feat)
        labels_list.append(label_id)

    subject_id = subject_result.get("subject_id", "unknown")
    return np.array(features_list), np.array(labels_list), subject_id


def extract_wesad_window_features(
    subject_result: dict,
) -> tuple[np.ndarray, np.ndarray, str] | None:
    """Canonical 60s fusion-window feature extraction (single source of truth).

    Consumes result["fusion_windows"] (quality_metadata per 60s window,
    overlap_s=0, purity>=0.9 per FUSION_WINDOW_CONFIG). One sample per window.
    Returns (features, labels, subject_id) or None if no 3-class windows.
    """
    windows = subject_result.get("fusion_windows", [])
    if not windows:
        return None
    subject_id = subject_result.get("subject_id", "unknown")
    features_list: list[list[float]] = []
    labels_list: list[int] = []
    for window_meta in windows:
        if not isinstance(window_meta, dict):
            continue
        parsed = fusion_window_quality_to_features(window_meta)
        if parsed is None:
            continue
        feats, label_id = parsed
        features_list.append(feats)
        labels_list.append(label_id)
    if not features_list:
        return None
    return np.array(features_list), np.array(labels_list), subject_id


def validate_wesad_stress_classification(
    results: list[dict],
    n_splits: int = 5,
) -> dict[str, float]:
    """Validate WESAD 3-class stress classification.

    Canonical path (Architecture.md §51-53, Roadmap.md §4): 60s native-rate
    fusion windows (FUSION_WINDOW_CONFIG), GroupKFold by subject, overlap_s=0.
    Falls back to legacy segment scoring only when no fusion_windows present.

    Target: ≥80% accuracy (published benchmark: 80% for 3-class, 93% for binary)
    Uses subject-grouped GroupKFold to prevent data leakage.
    """
    all_features = []
    all_labels = []
    all_groups = []

    use_windows = any(bool(r.get("fusion_windows")) for r in results)
    feature_source = "fusion_windows_60s" if use_windows else "segments_legacy"
    extractor = extract_wesad_window_features if use_windows else extract_wesad_features

    for result in results:
        extracted = extractor(result)
        if extracted is not None:
            feats, labels, subject_id = extracted
            all_features.append(feats)
            all_labels.append(labels)
            all_groups.extend([subject_id] * len(labels))

    if not all_features:
        return {
            "accuracy": 0.0,
            "error": "No valid segments",
            "per_fold_scores": [],
            "feature_source": feature_source,
        }

    X = np.vstack(all_features)
    y = np.hstack(all_labels)
    groups = np.array(all_groups)

    # Remove any NaN/inf
    mask = np.all(np.isfinite(X), axis=1)
    X = X[mask]
    y = y[mask]
    groups = groups[mask]

    if len(np.unique(y)) < 3:
        return {
            "accuracy": 0.0,
            "error": "Less than 3 classes present",
            "per_fold_scores": [],
            "feature_source": feature_source,
        }

    # Random Forest with subject-grouped CV (GroupKFold - no subject leakage)
    pipeline = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(n_estimators=200, random_state=SEED, n_jobs=-1)),
        ]
    )

    # GroupKFold: each fold holds out entire subjects
    n_subjects = len(np.unique(groups))
    effective_splits = min(n_splits, n_subjects)
    cv = GroupKFold(n_splits=effective_splits)
    scores = cross_val_score(pipeline, X, y, groups=groups, cv=cv, scoring="accuracy", n_jobs=-1)

    return {
        "accuracy": float(np.mean(scores)),
        "std": float(np.std(scores)),
        "min": float(np.min(scores)),
        "max": float(np.max(scores)),
        "n_samples": len(X),
        "n_subjects": n_subjects,
        "n_classes": len(np.unique(y)),
        "n_splits": effective_splits,
        "target_met": bool(np.mean(scores) >= 0.80),
        "per_fold_scores": [float(s) for s in scores],
        "feature_source": feature_source,
        "window_config": dict(FUSION_WINDOW_CONFIG) if use_windows else {},
    }


def validate_mitbih_rpeak_detection(
    results: list[dict],
) -> dict[str, float]:
    """Validate MIT-BIH R-peak detection against published baselines.

    Target: Sensitivity ≥99.6%, PPV ≥99.6% (standard benchmark)
    RMSSD MAE: <5 ms (full set), <2 ms on clean records
    """
    sensitivities = []
    ppvs = []
    maes = []
    clean_maes = []

    for result in results:
        sens = result.get("r_peak_sensitivity", 0)
        ppv = result.get("r_peak_ppv", 0)
        mae = result.get("rmssd_mae_ms", 0)

        if sens > 0:
            sensitivities.append(sens)
        if ppv > 0:
            ppvs.append(ppv)
        if mae > 0:
            maes.append(mae)
            # Clean records: MAE < 10ms typically indicates good quality
            if mae < 10:
                clean_maes.append(mae)

    if not sensitivities:
        return {"error": "No valid results", "per_fold_scores": []}

    return {
        "mean_sensitivity": float(np.mean(sensitivities)),
        "std_sensitivity": float(np.std(sensitivities)),
        "min_sensitivity": float(np.min(sensitivities)),
        "mean_ppv": float(np.mean(ppvs)),
        "std_ppv": float(np.std(ppvs)),
        "min_ppv": float(np.min(ppvs)),
        "mean_rmssd_mae_ms": float(np.mean(maes)),
        "std_rmssd_mae_ms": float(np.std(maes)),
        "mean_rmssd_mae_clean_ms": float(np.mean(clean_maes))
        if clean_maes
        else float(np.mean(maes)),
        "target_sensitivity_met": bool(np.mean(sensitivities) >= 0.996),
        "target_ppv_met": bool(np.mean(ppvs) >= 0.996),
        "target_rmssd_mae_met": bool(np.mean(maes) < 5.0),
        "target_rmssd_mae_clean_met": bool(np.mean(clean_maes) < 2.0) if clean_maes else False,
        "n_records": len(sensitivities),
        "n_clean_records": len(clean_maes),
    }


def _extract_sleep_edf_data(xdf_path: Path) -> tuple | None:
    """Extract EEG, EOG, EMG, and hypnogram from Sleep-EDF XDF file."""
    import pyxdf

    streams, _ = pyxdf.load_xdf(str(xdf_path))

    eeg_signal = None
    eeg_fs = None
    gold_hypnogram = None
    gold_times = None
    eog_signal = None
    emg_signal = None

    for stream in streams:
        info = stream["info"]
        stream_name = info.get("name", [""])[0]
        stream_type = info.get("type", [""])[0]
        time_series = stream["time_series"]
        time_stamps = stream["time_stamps"]

        if "EEG" in stream_type and "Fpz" in stream_name:
            eeg_signal = time_series.flatten()
            eeg_fs = info.get("nominal_srate", [100])[0]
        elif "EOG" in stream_type:
            eog_signal = time_series.flatten()
        elif "EMG" in stream_type:
            emg_signal = time_series.flatten()
        elif stream_type == "Markers" and "Hypnogram" in stream_name:
            markers = time_series.flatten()
            gold_hypnogram = []
            gold_times = []
            for ts, marker in zip(time_stamps, markers):
                if isinstance(marker, str) and marker.startswith("Stage_"):
                    stage = marker.replace("Stage_", "")
                    stage_map = {
                        "W": 0,
                        "N1": 1,
                        "N2": 2,
                        "N3": 3,
                        "REM": 4,
                        "MOVE": 5,
                        "UNK": 9,
                    }
                    gold_hypnogram.append(stage_map.get(stage, 9))
                    gold_times.append(float(ts))
            gold_hypnogram = np.array(gold_hypnogram, dtype=np.int64)
            gold_times = np.array(gold_times, dtype=np.float64)

    if eeg_signal is None or eeg_fs is None or gold_hypnogram is None or gold_times is None:
        return None

    return eeg_signal, int(eeg_fs), gold_hypnogram, gold_times, eog_signal, emg_signal


def _validate_single_sleep_subject(subject_id: str, xdf_path: Path) -> dict | None:
    """Validate a single Sleep-EDF subject using YASA."""
    extracted = _extract_sleep_edf_data(xdf_path)
    if extracted is None:
        print(f"Warning: Missing EEG or hypnogram data for {subject_id}")
        return None

    eeg_signal, eeg_fs, gold_hypnogram, gold_times, eog_signal, emg_signal = extracted

    try:
        validation_result = validate_sleep_staging_against_gold(
            eeg_signal=eeg_signal,
            sampling_rate=eeg_fs,
            gold_hypnogram=gold_hypnogram,
            gold_times=gold_times,
            eog_signal=eog_signal,
            emg_signal=emg_signal,
        )

        target_ok = bool(validation_result["target_met"])
        print(
            f"  {subject_id}: kappa={validation_result['kappa']:.3f}, "
            f"acc={validation_result['accuracy']:.3f}, "
            f"epochs={validation_result['n_epochs']}, "
            f"target={'PASS' if target_ok else 'FAIL'}"
        )

        return {
            "subject_id": subject_id,
            "kappa": validation_result["kappa"],
            "accuracy": validation_result["accuracy"],
            "n_epochs": validation_result["n_epochs"],
            "target_met": target_ok,
            "per_stage": validation_result.get("per_stage", {}),
        }

    except Exception as e:
        print(f"Error validating {subject_id}: {e}")
        return None


def validate_sleep_edf_sleep_staging(
    results: list[dict],
    data_dir: Path,
) -> dict[str, float]:
    """Validate Sleep-EDF sleep staging against gold standard hypnogram.

    Loads EEG from XDF files, runs YASA sleep staging, and compares
    against the gold standard hypnogram from annotations.
    Target: Cohen's κ ≥0.75 (substantial agreement per Landis & Koch)
    """
    all_kappas = []
    all_accuracies = []
    subject_details = []

    for result in results:
        subject_id = result.get("subject_id", "")
        xdf_path = result.get("xdf_path", "")

        if not xdf_path or not Path(xdf_path).exists():
            continue

        xdf_summary = validate_xdf(Path(xdf_path))
        if not xdf_summary["validation"]["all_streams_valid"]:
            print(f"Warning: XDF validation failed for {xdf_path}: {xdf_summary['validation']}")
            continue

        subject_result = _validate_single_sleep_subject(subject_id, Path(xdf_path))
        if subject_result is None:
            continue

        all_kappas.append(subject_result["kappa"])
        all_accuracies.append(subject_result["accuracy"])
        subject_details.append(subject_result)

    if not all_kappas:
        return {"error": "No valid sleep recordings processed", "kappa": 0.0, "target_met": False}

    mean_kappa = float(np.mean(all_kappas))
    std_kappa = float(np.std(all_kappas))
    mean_acc = float(np.mean(all_accuracies))

    return {
        "mean_cohen_kappa": mean_kappa,
        "std_cohen_kappa": std_kappa,
        "mean_accuracy": mean_acc,
        "n_subjects": len(all_kappas),
        "subject_details": subject_details,
        "target_met": bool(mean_kappa >= 0.75),
    }


def run_xdf_validation_summary(output_dir: Path) -> dict:
    """Run XDF validation on all generated XDF files and return summary."""
    xdf_files = list(output_dir.glob("*.xdf"))
    summary = {
        "files_validated": 0,
        "files_failed": 0,
        "details": [],
    }

    for xdf_path in xdf_files:
        try:
            xdf_summary = validate_xdf(xdf_path)
            summary["files_validated"] += 1
            summary["details"].append(
                {
                    "file": xdf_path.name,
                    "valid": xdf_summary["validation"]["all_streams_valid"],
                    "streams": len(xdf_summary["streams"]),
                    "duration_s": xdf_summary.get("duration_s", 0),
                }
            )
        except Exception as e:
            summary["files_failed"] += 1
            summary["details"].append(
                {
                    "file": xdf_path.name,
                    "valid": False,
                    "error": str(e),
                }
            )

    return summary


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    import argparse

    parser = argparse.ArgumentParser(description="Validate against published baselines")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Data directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed"),
        help="Processed data directory",
    )
    parser.add_argument(
        "--dataset",
        choices=["wesad", "mitbih", "sleep_edf", "both", "all"],
        default="all",
    )
    parser.add_argument(
        "--tier",
        type=int,
        choices=[0, 1, 2],
        default=1,
        help="Acquisition tier (0=continuous, 1=high-density, 2=calibration)",
    )
    parser.add_argument(
        "--use-cache",
        action="store_true",
        help="Read validation data from cached XDF/JSON files instead of re-ingesting",
    )
    return parser.parse_args()


def _run_dataset_validation(
    args: argparse.Namespace,
    tier: Tier,
) -> dict:
    """Run validation for specified datasets."""
    all_results = {}
    selected = resolve_baseline_datasets(args.dataset)

    if "wesad" in selected:
        if args.use_cache:
            wesad_results = load_cached_results(args.output_dir, "wesad")
        else:
            wesad_results = ingest_wesad(
                data_dir=args.data_dir / "wesad",
                output_dir=args.output_dir,
                tier=tier,
            )
        all_results["wesad"] = validate_wesad_stress_classification(wesad_results)

    if "mitbih" in selected:
        if args.use_cache:
            mitbih_results = load_cached_results(args.output_dir, "mitbih")
        else:
            mitbih_results = ingest_mitbih(
                data_dir=args.data_dir / "mitbih",
                output_dir=args.output_dir,
                tier=tier,
            )
        all_results["mitbih"] = validate_mitbih_rpeak_detection(mitbih_results)

    if "sleep_edf" in selected:
        if args.use_cache:
            sleep_edf_results = load_cached_results(args.output_dir, "sleep_edf")
        else:
            sleep_edf_results = ingest_sleep_edf(
                data_dir=args.data_dir / "sleep_edf",
                output_dir=args.output_dir,
                tier=tier,
            )
        all_results["sleep_edf"] = validate_sleep_edf_sleep_staging(
            sleep_edf_results, args.data_dir
        )

    return all_results


def _build_baseline_report(all_results: dict, xdf_summary: dict) -> dict:
    """Build the comprehensive baseline report (schema-validated before return)."""
    report = {
        "seed": SEED,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
        "datasets": all_results,
        "xdf_validation": xdf_summary,
        "schema_version": BASELINE_REPORT_SCHEMA_VERSION,
    }
    validate_baseline_report_schema(report)
    return report


def _save_reports(
    all_results: dict,
    baseline_report: dict,
    output_dir: Path,
) -> Path:
    """Save validation results and baseline report to disk."""
    output_path = output_dir / "baseline_validation.json"
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    report_path = output_dir / "baseline_report.json"
    with open(report_path, "w") as f:
        json.dump(baseline_report, f, indent=2, default=str)

    return report_path


def _print_report(
    all_results: dict,
    xdf_summary: dict,
    report_path: Path,
) -> bool:
    """Print validation report and return overall pass/fail."""
    print(f"\n{'=' * 60}")
    print("BASELINE VALIDATION REPORT")
    print(f"{'=' * 60}")
    print(f"Seed: {SEED}")
    print(f"Report saved to: {report_path}")
    print(
        f"XDF files validated: {xdf_summary['files_validated']}, failed: {xdf_summary['files_failed']}"
    )

    # NOTE: ASCII-only output — Windows consoles (cp1252) cannot encode
    # unicode symbols (>=, kappa, emoji). Keep prints cp1252-safe.
    def _mark(ok: object) -> str:
        return "PASS" if ok else "FAIL"

    overall_pass = True
    if "wesad" in all_results:
        w = all_results["wesad"]
        print(
            f"WESAD 3-class: {w.get('accuracy', 0):.3f} (target >=0.80) [{_mark(w.get('target_met'))}]"
        )
        overall_pass = overall_pass and bool(w.get("target_met", False))

    if "mitbih" in all_results:
        m = all_results["mitbih"]
        print(
            f"MIT-BIH Se: {m.get('mean_sensitivity', 0):.4f} "
            f"(target >=0.996) [{_mark(m.get('target_sensitivity_met'))}]"
        )
        print(
            f"MIT-BIH PPV: {m.get('mean_ppv', 0):.4f} "
            f"(target >=0.996) [{_mark(m.get('target_ppv_met'))}]"
        )
        print(
            f"MIT-BIH RMSSD MAE: {m.get('mean_rmssd_mae_ms', 0):.2f}ms "
            f"(target <5ms) [{_mark(m.get('target_rmssd_mae_met'))}]"
        )
        print(
            f"MIT-BIH RMSSD MAE (clean): {m.get('mean_rmssd_mae_clean_ms', 0):.2f}ms "
            f"(target <2ms) [{_mark(m.get('target_rmssd_mae_clean_met'))}]"
        )
        overall_pass = (
            overall_pass
            and bool(m.get("target_sensitivity_met", False))
            and bool(m.get("target_ppv_met", False))
            and bool(m.get("target_rmssd_mae_met", False))
        )

    if "sleep_edf" in all_results:
        s = all_results["sleep_edf"]
        print(
            f"Sleep-EDF kappa: {s.get('mean_cohen_kappa', 0):.3f} "
            f"(target >=0.75) [{_mark(s.get('target_met'))}]"
        )
        overall_pass = overall_pass and bool(s.get("target_met", False))

    print(f"\nOverall: {'PASS' if overall_pass else 'FAIL'}")
    return overall_pass


def main() -> int:
    """Main entry point for baseline validation."""
    args = _parse_args()
    tier = Tier(args.tier)

    all_results = _run_dataset_validation(args, tier)
    xdf_summary = run_xdf_validation_summary(args.output_dir)
    baseline_report = _build_baseline_report(all_results, xdf_summary)
    report_path = _save_reports(all_results, baseline_report, args.output_dir)
    overall_pass = _print_report(all_results, xdf_summary, report_path)

    return 0 if overall_pass else 1


def load_cached_results(output_dir: Path, dataset: str) -> list[dict]:
    """Load validation results from cached JSON files."""
    results = []

    if dataset == "mitbih":
        # MIT-BIH files are named like 100_quality.json
        pattern = "*_quality.json"
        for json_file in output_dir.glob(pattern):
            name = json_file.stem.replace("_quality", "")
            if name.isdigit() or name.startswith("2"):
                try:
                    with open(json_file) as f:
                        data = json.load(f)
                        result = {
                            "r_peak_sensitivity": data.get("r_peak_sensitivity", 0),
                            "r_peak_ppv": data.get("r_peak_ppv", 0),
                            "rmssd_mae_ms": data.get("rmssd_mae_ms", 0),
                        }
                        results.append(result)
                except Exception as e:
                    print(f"Warning: Failed to load {json_file}: {e}")
    elif dataset == "wesad":
        for json_file in output_dir.glob("S*_quality.json"):
            try:
                with open(json_file) as f:
                    data = json.load(f)
                    results.append(data)
            except Exception as e:
                print(f"Warning: Failed to load {json_file}: {e}")
    elif dataset == "sleep_edf":
        for json_file in output_dir.glob("*sleep*quality.json"):
            try:
                with open(json_file) as f:
                    data = json.load(f)
                    results.append(data)
            except Exception as e:
                print(f"Warning: Failed to load {json_file}: {e}")

    return results


if __name__ == "__main__":
    sys.exit(main())
