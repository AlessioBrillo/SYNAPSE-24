#!/usr/bin/env python3
"""Baseline validation against published results.

Reproduces:
1. WESAD 3-class stress classification (ECG+EDA+ACC) - target ≥80% accuracy
2. MIT-BIH R-peak detection - target Se ≥99.6%, PPV ≥99.6%
3. Sleep-EDF sleep staging - target Cohen's κ ≥0.75 vs. gold standard
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import cohen_kappa_score
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from synapse24.ingestion import Tier, ingest_mitbih, ingest_sleep_edf, ingest_wesad
from synapse24.signal_quality import validate_sleep_staging_against_gold
from synapse24.utils import validate_xdf


def extract_wesad_features(
    subject_result: dict,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Extract features and labels for stress classification from WESAD subject.

    Uses segments: baseline (1) vs stress (2) vs amusement (3) for 3-class.
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

    return np.array(features_list), np.array(labels_list)


def validate_wesad_stress_classification(
    results: list[dict],
    n_splits: int = 5,
) -> dict[str, float]:
    """Validate WESAD 3-class stress classification.

    Target: ≥80% accuracy (published benchmark: 80% for 3-class, 93% for binary)
    """
    all_features = []
    all_labels = []

    for result in results:
        extracted = extract_wesad_features(result)
        if extracted is not None:
            feats, labels = extracted
            all_features.append(feats)
            all_labels.append(labels)

    if not all_features:
        return {"accuracy": 0.0, "error": "No valid segments"}

    X = np.vstack(all_features)
    y = np.hstack(all_labels)

    # Remove any NaN/inf
    mask = np.all(np.isfinite(X), axis=1)
    X = X[mask]
    y = y[mask]

    if len(np.unique(y)) < 3:
        return {"accuracy": 0.0, "error": "Less than 3 classes present"}

    # Random Forest with CV
    pipeline = Pipeline(
        [
            ("scaler", StandardScaler()),
            ("clf", RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1)),
        ]
    )

    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
    scores = cross_val_score(pipeline, X, y, cv=cv, scoring="accuracy", n_jobs=-1)

    return {
        "accuracy": float(np.mean(scores)),
        "std": float(np.std(scores)),
        "min": float(np.min(scores)),
        "max": float(np.max(scores)),
        "n_samples": len(X),
        "n_classes": len(np.unique(y)),
        "target_met": np.mean(scores) >= 0.80,
    }


def validate_mitbih_rpeak_detection(
    results: list[dict],
) -> dict[str, float]:
    """Validate MIT-BIH R-peak detection against published baselines.

    Target: Sensitivity ≥99.6%, PPV ≥99.6% (standard benchmark)
    """
    sensitivities = []
    ppvs = []
    maes = []

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

    if not sensitivities:
        return {"error": "No valid results"}

    return {
        "mean_sensitivity": float(np.mean(sensitivities)),
        "std_sensitivity": float(np.std(sensitivities)),
        "min_sensitivity": float(np.min(sensitivities)),
        "mean_ppv": float(np.mean(ppvs)),
        "std_ppv": float(np.std(ppvs)),
        "min_ppv": float(np.min(ppvs)),
        "mean_rmssd_mae_ms": float(np.mean(maes)),
        "std_rmssd_mae_ms": float(np.std(maes)),
        "target_sensitivity_met": np.mean(sensitivities) >= 0.996,
        "target_ppv_met": np.mean(ppvs) >= 0.996,
        "n_records": len(sensitivities),
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

        print(
            f"  {subject_id}: κ={validation_result['kappa']:.3f}, "
            f"acc={validation_result['accuracy']:.3f}, "
            f"epochs={validation_result['n_epochs']}, "
            f"target={'✅' if validation_result['target_met'] else '❌'}"
        )

        return {
            "subject_id": subject_id,
            "kappa": validation_result["kappa"],
            "accuracy": validation_result["accuracy"],
            "n_epochs": validation_result["n_epochs"],
            "target_met": validation_result["target_met"],
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
        "target_met": mean_kappa >= 0.75,
    }


def main():
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
        choices=["wesad", "mitbih", "sleep_edf", "all"],
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
    args = parser.parse_args()

    tier = Tier(args.tier)
    all_results = {}

    if args.dataset in ("wesad", "all"):
        if args.use_cache:
            wesad_results = load_cached_results(args.output_dir, "wesad")
        else:
            wesad_results = ingest_wesad(
                data_dir=args.data_dir / "wesad",
                output_dir=args.output_dir,
                tier=tier,
            )
        wesad_metrics = validate_wesad_stress_classification(wesad_results)
        all_results["wesad"] = wesad_metrics

    if args.dataset in ("mitbih", "all"):
        if args.use_cache:
            mitbih_results = load_cached_results(args.output_dir, "mitbih")
        else:
            mitbih_results = ingest_mitbih(
                data_dir=args.data_dir / "mitbih",
                output_dir=args.output_dir,
                tier=tier,
            )
        mitbih_metrics = validate_mitbih_rpeak_detection(mitbih_results)
        all_results["mitbih"] = mitbih_metrics

    if args.dataset in ("sleep_edf", "all"):
        if args.use_cache:
            sleep_edf_results = load_cached_results(args.output_dir, "sleep_edf")
        else:
            sleep_edf_results = ingest_sleep_edf(
                data_dir=args.data_dir / "sleep_edf",
                output_dir=args.output_dir,
                tier=tier,
            )
        sleep_edf_metrics = validate_sleep_edf_sleep_staging(sleep_edf_results, args.data_dir)
        all_results["sleep_edf"] = sleep_edf_metrics

    # Save validation results
    output_path = args.output_dir / "baseline_validation.json"
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)

    # Overall pass/fail
    overall_pass = True
    if "wesad" in all_results:
        overall_pass &= all_results["wesad"].get("target_met", False)
    if "mitbih" in all_results:
        overall_pass &= all_results["mitbih"].get("target_sensitivity_met", False)
        overall_pass &= all_results["mitbih"].get("target_ppv_met", False)
    if "sleep_edf" in all_results:
        overall_pass &= all_results["sleep_edf"].get("target_met", False)

    return 0 if overall_pass else 1


def load_cached_results(output_dir: Path, dataset: str) -> list[dict]:
    """Load validation results from cached JSON files."""
    results = []

    if dataset == "mitbih":
        # MIT-BIH files are named like 100_quality.json
        pattern = "*_quality.json"
        for json_file in output_dir.glob(pattern):
            # Skip non-MITBIH files (WESAD uses S2, S3, etc. and Sleep-EDF uses different naming)
            name = json_file.stem.replace("_quality", "")
            if name.isdigit() or name.startswith("2"):  # MIT-BIH records are 100-234
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
        # WESAD files are named like S2_quality.json, S3_quality.json
        for json_file in output_dir.glob("S*_quality.json"):
            try:
                with open(json_file) as f:
                    data = json.load(f)
                    results.append(data)
            except Exception as e:
                print(f"Warning: Failed to load {json_file}: {e}")
    elif dataset == "sleep_edf":
        # Sleep-EDF files would have subject IDs
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
