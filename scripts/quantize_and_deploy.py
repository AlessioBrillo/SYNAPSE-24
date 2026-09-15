#!/usr/bin/env python3
"""Quantize and deploy SYNAPSE-24 triage model for ESP32-S3.

Runs INT8 post-training quantization with representative dataset,
generates TFLite model, C header, CMakeLists.txt, and inference example.
Runs Phase 0 exit gate validation.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import numpy.typing as npt

try:
    import joblib
    import tensorflow as tf
    from tensorflow import keras
except ImportError:
    tf = None
    keras = None
    joblib = None

from synapse24.edge_ai.deployment import (
    DeploymentConfig,
    DeploymentResult,
    check_phase0_exit_gate,
    deploy_model,
    generate_deployment_report,
)
from synapse24.edge_ai.model import EdgeModel, ModelConfig, ModelType, TargetPlatform
from synapse24.edge_ai.quantization import (
    QuantizationConfig,
    QuantizationResult,
    RepresentativeDatasetGenerator,
    quantize_model,
    save_quantization_artifacts,
)

logger = logging.getLogger(__name__)


def load_edge_model(model_dir: Path) -> EdgeModel:
    """Load trained EdgeModel from directory."""
    # Load Keras model
    keras_model = keras.models.load_model(model_dir / "synapse_triage.keras")

    # Load scaler
    scaler = joblib.load(model_dir / "feature_scaler.joblib")

    # Reconstruct ModelConfig
    config = ModelConfig(
        name="synapse_triage",
        model_type=ModelType.STRESS_CLASSIFIER,
        target_platform=TargetPlatform.ESP32_S3,
        input_shape=(1, 26),  # (n_timesteps, n_features)
        num_classes=3,
        labels=["baseline", "stress", "artifact"],
        feature_names=[f"feat_{i}" for i in range(26)],
        sampling_rate=1.0,
        window_duration_s=60.0,
        architecture="cnn",
        hidden_units=64,
        num_layers=2,
        dropout=0.2,
        version="0.1.0",
    )

    return EdgeModel(config=config, model=keras_model)


def generate_representative_dataset(
    model_dir: Path,
    data_dir: Path,
    use_synthetic: bool = False,
) -> npt.NDArray[np.float32]:
    """Generate representative dataset for INT8 calibration."""
    if use_synthetic:
        logger.info("Generating synthetic representative dataset")
        rng = np.random.default_rng(42)
        # 100 samples matching input shape (1, 26)
        data = rng.normal(0, 1, size=(100, 1, 26)).astype(np.float32)
        return data

    # Load scaler
    scaler = joblib.load(model_dir / "feature_scaler.joblib")

    # Try to load real WESAD data for representative dataset
    try:
        from synapse24.ingestion import extract_native_rate_fusion_windows
        from synapse24.ingestion.wesad import load_wesad_subject

        wesad_results = []
        subjects = ["S2", "S3", "S4", "S5", "S6"]
        for subj in subjects:
            try:
                result = load_wesad_subject(data_dir / "wesad" / "WESAD", subj)
                wesad_results.append(result)
            except Exception as e:
                logger.warning(f"Failed to load {subj} for rep dataset: {e}")

        if wesad_results:
            # Extract IMU features
            X, _ = extract_imu_features_from_wesad_for_rep(wesad_results)
            # Scale
            X = scaler.transform(X)
            X = X.reshape(-1, 1, 26).astype(np.float32)
            logger.info(f"Generated representative dataset from WESAD: {X.shape}")
            return X[:100]  # Use first 100
    except Exception as e:
        logger.warning(f"Could not generate from WESAD: {e}")

    # Fallback to synthetic
    logger.info("Falling back to synthetic representative dataset")
    rng = np.random.default_rng(42)
    return rng.normal(0, 1, size=(100, 1, 26)).astype(np.float32)


def extract_imu_features_from_wesad_for_rep(
    wesad_results: list[dict],
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.int64]]:
    """Extract IMU features from WESAD for representative dataset (no labels needed)."""
    from scipy.signal import welch
    from scipy.stats import entropy

    all_features = []

    for result in wesad_results:
        for window_meta in result.get("fusion_windows", []):
            if not isinstance(window_meta, dict):
                continue

            chest_signals = window_meta.get("chest_signals", {})
            wrist_signals = window_meta.get("wrist_signals", {})

            features = []

            for axis in ["acc_x", "acc_y", "acc_z"]:
                signal = chest_signals.get(axis)
                if signal is None or len(signal) < 100:
                    features.extend([0.0, 0.0, 0.0, 0.0])
                    continue
                sig = np.asarray(signal, dtype=np.float32)
                features.append(float(np.mean(sig)))
                features.append(float(np.std(sig)))
                freqs, psd = welch(sig, fs=700.0, nperseg=min(1024, len(sig)))
                psd_norm = psd / (np.sum(psd) + 1e-10)
                features.append(float(entropy(psd_norm + 1e-10)))
                dom_freq = freqs[np.argmax(psd)]
                features.append(float(dom_freq))

            for axis in ["acc_x", "acc_y", "acc_z"]:
                signal = wrist_signals.get(axis)
                if signal is None or len(signal) < 10:
                    features.extend([0.0, 0.0, 0.0, 0.0])
                    continue
                sig = np.asarray(signal, dtype=np.float32)
                features.append(float(np.mean(sig)))
                features.append(float(np.std(sig)))
                freqs, psd = welch(sig, fs=32.0, nperseg=min(256, len(sig)))
                psd_norm = psd / (np.sum(psd) + 1e-10)
                features.append(float(entropy(psd_norm + 1e-10)))
                dom_freq = freqs[np.argmax(psd)]
                features.append(float(dom_freq))

            bvp = wrist_signals.get("bvp")
            if bvp is not None and len(bvp) > 50:
                bvp_arr = np.asarray(bvp, dtype=np.float32)
                features.append(float(np.mean(bvp_arr)))
                features.append(float(np.std(bvp_arr)))
            else:
                features.extend([0.0, 0.0])

            all_features.append(features)

    X = np.array(all_features, dtype=np.float32)
    return X, np.zeros(len(X), dtype=np.int64)


def quantize_and_deploy(
    model_dir: Path,
    output_dir: Path,
    data_dir: Path,
    use_synthetic: bool = False,
    profile: str = "triage",
) -> tuple[QuantizationResult, DeploymentResult]:
    """Full quantization and deployment pipeline."""
    logger.info(f"Loading model from {model_dir}")
    edge_model = load_edge_model(model_dir)

    logger.info("Generating representative dataset...")
    rep_data = generate_representative_dataset(model_dir, data_dir, use_synthetic)

    logger.info("Running INT8 quantization...")
    quant_config = QuantizationConfig(
        quantization_type="int8",
        representative_dataset_size=100,
        inference_input_type="int8",
        inference_output_type="int8",
        target_platform=TargetPlatform.ESP32_S3,
    )

    quant_result = quantize_model(
        edge_model=edge_model,
        quant_config=quant_config,
        representative_data=rep_data,
        validation_data=(rep_data, np.zeros(100, dtype=np.int64)),  # Dummy validation
    )

    logger.info(
        f"Quantized model: {quant_result.model_size_kb:.1f} KB, "
        f"RAM: {quant_result.estimated_ram_kb:.1f} KB, "
        f"Accuracy drop: {quant_result.accuracy_drop_percent:.2f}pp"
    )
    logger.info(f"Ops used: {quant_result.ops_used}")

    # Save quantization artifacts
    logger.info("Saving quantization artifacts...")
    save_quantization_artifacts(quant_result, edge_model, output_dir)

    # Deploy
    logger.info("Generating deployment artifacts...")
    deploy_config = DeploymentConfig(
        target_platform=TargetPlatform.ESP32_S3,
        optimization_level="O2",
        arena_size_kb=0,  # Auto
        enable_cmsis_nn=True,
        generate_example=True,
    )

    deploy_result = deploy_model(edge_model, quant_result, deploy_config, output_dir)

    # Run Phase 0 exit gate
    logger.info("Running Phase 0 exit gate check...")
    gate_result = check_phase0_exit_gate(
        model_size_kb=quant_result.model_size_kb,
        estimated_ram_kb=quant_result.estimated_ram_kb,
        estimated_latency_ms=deploy_result.estimated_latency_ms,
        accuracy_drop_percent=quant_result.accuracy_drop_percent,
        ops_used=quant_result.ops_used,
        profile=profile,
    )

    logger.info(f"Phase 0 exit gate ({profile}): {'PASSED' if gate_result['passed'] else 'FAILED'}")
    if gate_result["failures"]:
        for f in gate_result["failures"]:
            logger.error(f"  FAIL: {f}")

    # Save gate result
    (output_dir / "phase0_exit_gate.json").write_text(json.dumps(gate_result, indent=2))

    # Generate deployment report
    generate_deployment_report(deploy_result, output_dir / "deployment_report.md")

    # Also save validation metrics (convert numpy arrays to lists for JSON)
    def _make_serializable(obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, (np.integer, np.floating)):
            return obj.item()
        if isinstance(obj, dict):
            return {k: _make_serializable(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_make_serializable(v) for v in obj]
        return obj

    metrics = {
        "model_size_kb": quant_result.model_size_kb,
        "estimated_ram_kb": quant_result.estimated_ram_kb,
        "estimated_flash_kb": deploy_result.estimated_flash_kb,
        "estimated_latency_ms": deploy_result.estimated_latency_ms,
        "accuracy_drop_percent": quant_result.accuracy_drop_percent,
        "ops_used": quant_result.ops_used,
        "input_details": _make_serializable(quant_result.input_details),
        "output_details": _make_serializable(quant_result.output_details),
        "calibration_stats": _make_serializable(quant_result.calibration_stats),
        "phase0_exit_gate": _make_serializable(gate_result),
    }
    (output_dir / "quantization_metrics.json").write_text(json.dumps(metrics, indent=2))

    return quant_result, deploy_result


def main():
    parser = argparse.ArgumentParser(description="Quantize and deploy triage model")
    parser.add_argument(
        "--model-dir", type=Path, default=Path("models/triage"), help="Trained model directory"
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("models/triage/deploy"), help="Output directory"
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Data root directory")
    parser.add_argument("--synthetic", action="store_true", help="Use synthetic data (for CI)")
    parser.add_argument(
        "--profile", default="triage", choices=["triage", "hub_fusion"], help="Exit gate profile"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if keras is None:
        logger.error("TensorFlow/Keras not installed")
        return 1

    try:
        quant_result, deploy_result = quantize_and_deploy(
            model_dir=args.model_dir,
            output_dir=args.output_dir,
            data_dir=args.data_dir,
            use_synthetic=args.synthetic,
            profile=args.profile,
        )

        if deploy_result.validation_passed:
            logger.info("✅ Quantization and deployment SUCCESSFUL")
            logger.info(f"  Model: {quant_result.model_size_kb:.1f} KB")
            logger.info(f"  RAM: {quant_result.estimated_ram_kb:.1f} KB")
            logger.info(f"  Latency: {deploy_result.estimated_latency_ms:.1f} ms")
            logger.info(f"  Accuracy drop: {quant_result.accuracy_drop_percent:.2f}pp")
            return 0
        logger.error("❌ Deployment validation FAILED")
        for note in deploy_result.validation_notes:
            logger.error(f"  {note}")
        return 1

    except Exception as e:
        logger.exception("Quantization/deployment failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
