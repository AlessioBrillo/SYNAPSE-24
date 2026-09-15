#!/usr/bin/env python3
"""Train triage model for SYNAPSE-24 Tier 0 edge inference.

3-class IMU classifier: baseline / stress / artifact
Trains on WESAD fusion windows (60s, native-rate, no resampling).
Outputs Keras model ready for TFLM INT8 quantization.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

try:
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras import callbacks, layers
except ImportError:
    tf = None
    keras = None
    layers = None
    callbacks = None

from synapse24.edge_ai.model import EdgeModel, ModelConfig, ModelType, TargetPlatform
from synapse24.edge_ai.training import (
    EdgeImpulseTrainer,
    TrainingConfig,
    create_wesad_training_data,
)
from synapse24.ingestion import extract_native_rate_fusion_windows

logger = logging.getLogger(__name__)


def build_triage_model(input_shape: tuple[int, ...], num_classes: int = 3) -> keras.Model:
    """Build 1D CNN model for IMU triage classification.

    Architecture optimized for ESP32-S3 INT8 deployment:
    - 1D CNN feature extractor + dense head
    - ~30KB INT8 model size
    - <30ms inference on ESP32-S3 @ 240MHz
    - Converts cleanly to TFLite INT8 (no TensorList ops)
    """
    inputs = keras.Input(shape=input_shape, name="imu_input")
    x = inputs

    # 1D CNN feature extraction (works on 1 timestep, 26 features)
    # Since we have (1, 26), treat as 1D conv over features
    x = layers.Conv1D(32, 3, padding="same", activation="relu", name="conv1d_1")(x)
    x = layers.BatchNormalization(name="bn_1")(x)
    x = layers.Conv1D(64, 3, padding="same", activation="relu", name="conv1d_2")(x)
    x = layers.BatchNormalization(name="bn_2")(x)
    x = layers.GlobalAveragePooling1D(name="gap")(x)

    # Dense head
    x = layers.Dense(32, activation="relu", name="dense_1")(x)
    x = layers.Dropout(0.2, name="dropout_1")(x)
    x = layers.Dense(16, activation="relu", name="dense_2")(x)
    x = layers.Dropout(0.2, name="dropout_2")(x)

    if num_classes == 2:
        outputs = layers.Dense(1, activation="sigmoid", name="output")(x)
        loss = "binary_crossentropy"
        metrics = ["accuracy", keras.metrics.AUC(name="auc")]
    else:
        outputs = layers.Dense(num_classes, activation="softmax", name="output")(x)
        loss = "sparse_categorical_crossentropy"
        metrics = ["accuracy"]

    model = keras.Model(inputs, outputs, name="synapse_triage")
    model.compile(optimizer=keras.optimizers.Adam(learning_rate=1e-3), loss=loss, metrics=metrics)
    return model


def extract_imu_features_from_wesad(
    wesad_results: list[dict[str, Any]],
    window_duration_s: float = 60.0,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.int64]]:
    """Extract IMU features from WESAD fusion windows for triage training.

    Features per 60s window (native-rate, no resampling):
    - ACC chest (700 Hz): mean, std, spectral entropy, dominant freq for each axis
    - ACC wrist (32 Hz): mean, std, spectral entropy, dominant freq for each axis
    - Total: ~24 features per window

    Labels: 1=baseline, 2=stress, 3=amusement (4=meditation mapped to baseline)
    """
    from scipy.signal import welch
    from scipy.stats import entropy

    all_features = []
    all_labels = []

    label_map = {1: 0, 2: 1, 3: 2, 4: 0}  # 4-class -> 3-class (meditation -> baseline)

    for result in wesad_results:
        for window_meta in result.get("fusion_windows", []):
            if not isinstance(window_meta, dict):
                continue

            label = window_meta.get("label")
            if label not in label_map:
                continue

            chest_signals = window_meta.get("chest_signals", {})
            wrist_signals = window_meta.get("wrist_signals", {})

            features = []

            # Chest ACC (700 Hz) - 3 axes
            for axis in ["acc_x", "acc_y", "acc_z"]:
                signal = chest_signals.get(axis)
                if signal is None or len(signal) < 100:
                    features.extend([0.0, 0.0, 0.0, 0.0])
                    continue

                sig = np.asarray(signal, dtype=np.float32)
                features.append(float(np.mean(sig)))
                features.append(float(np.std(sig)))

                # Spectral features
                freqs, psd = welch(sig, fs=700.0, nperseg=min(1024, len(sig)))
                psd_norm = psd / (np.sum(psd) + 1e-10)
                features.append(float(entropy(psd_norm + 1e-10)))  # spectral entropy
                dom_freq = freqs[np.argmax(psd)]
                features.append(float(dom_freq))

            # Wrist ACC (32 Hz) - 3 axes
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

            # Wrist BVP (64 Hz) - add HRV-like features as proxy
            bvp = wrist_signals.get("bvp")
            if bvp is not None and len(bvp) > 50:
                bvp_arr = np.asarray(bvp, dtype=np.float32)
                features.append(float(np.mean(bvp_arr)))
                features.append(float(np.std(bvp_arr)))
            else:
                features.extend([0.0, 0.0])

            all_features.append(features)
            all_labels.append(label_map[label])

    X = np.array(all_features, dtype=np.float32)
    y = np.array(all_labels, dtype=np.int64)

    logger.info(f"Extracted {len(X)} windows, {X.shape[1]} features, labels: {np.bincount(y)}")
    return X, y


def create_synthetic_training_data(
    n_samples: int = 1000,
    n_features: int = 26,
    num_classes: int = 3,
) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.int64]]:
    """Generate synthetic training data for CI/testing when WESAD not available."""
    rng = np.random.default_rng(42)

    # Create somewhat separable clusters for each class
    X_list = []
    y_list = []

    samples_per_class = n_samples // num_classes
    for class_idx in range(num_classes):
        # Different mean for each class
        mean = np.zeros(n_features)
        mean[class_idx * 3 : (class_idx + 1) * 3] = [2.0, -1.5, 1.0]  # Distinctive pattern
        class_data = rng.normal(mean, 1.0, size=(samples_per_class, n_features))
        X_list.append(class_data.astype(np.float32))
        y_list.append(np.full(samples_per_class, class_idx, dtype=np.int64))

    X = np.vstack(X_list)
    y = np.hstack(y_list)

    # Shuffle
    idx = rng.permutation(len(X))
    return X[idx], y[idx]


def train_triage_model(
    data_dir: Path,
    output_dir: Path,
    use_synthetic: bool = False,
    epochs: int = 50,
    batch_size: int = 32,
) -> EdgeModel:
    """Train triage model and save to output_dir."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load or generate training data
    if use_synthetic:
        logger.info("Using synthetic training data (WESAD not available)")
        X_train, y_train = create_synthetic_training_data(2000, 26, 3)
        X_val, y_val = create_synthetic_training_data(400, 26, 3)
    else:
        logger.info("Loading WESAD data for training...")
        from synapse24.ingestion.wesad import load_wesad_subject

        wesad_results = []
        subjects = [
            "S2",
            "S3",
            "S4",
            "S5",
            "S6",
            "S7",
            "S8",
            "S9",
            "S10",
            "S11",
            "S13",
            "S14",
            "S15",
            "S16",
            "S17",
        ]
        for subj in subjects:
            try:
                result = load_wesad_subject(data_dir / "wesad" / "WESAD", subj)
                wesad_results.append(result)
            except Exception as e:
                logger.warning(f"Failed to load {subj}: {e}")

        if not wesad_results:
            logger.warning("No WESAD data loaded, falling back to synthetic")
            X_train, y_train = create_synthetic_training_data(2000, 26, 3)
            X_val, y_val = create_synthetic_training_data(400, 26, 3)
        else:
            X_train, y_train = extract_imu_features_from_wesad(wesad_results)
            # Split 80/20
            split = int(0.8 * len(X_train))
            idx = np.random.permutation(len(X_train))
            X_train, y_train = X_train[idx[:split]], y_train[idx[:split]]
            X_val, y_val = X_train[idx[split:]], y_train[idx[split:]]

    # Scale features
    from sklearn.preprocessing import StandardScaler

    scaler = StandardScaler()
    X_train = scaler.fit_transform(X_train)
    X_val = scaler.transform(X_val)

    # Save scaler for inference
    import joblib

    joblib.dump(scaler, output_dir / "feature_scaler.joblib")

    # Reshape for LSTM: (n_samples, n_timesteps, n_features)
    # We treat each window as 1 timestep with 26 features
    n_features = X_train.shape[1]
    X_train = X_train.reshape(-1, 1, n_features)
    X_val = X_val.reshape(-1, 1, n_features)

    input_shape = (1, n_features)

    # Build model
    model = build_triage_model(input_shape, num_classes=3)
    logger.info(f"Model summary:\n{model.summary()}")

    # Training callbacks
    train_callbacks = [
        callbacks.EarlyStopping(monitor="val_loss", patience=10, restore_best_weights=True),
        callbacks.ReduceLROnPlateau(monitor="val_loss", factor=0.5, patience=5, min_lr=1e-6),
    ]

    # Train
    history = model.fit(
        X_train,
        y_train,
        validation_data=(X_val, y_val),
        epochs=epochs,
        batch_size=batch_size,
        callbacks=train_callbacks,
        verbose=1,
    )

    # Evaluate
    eval_results = model.evaluate(X_val, y_val, verbose=0, return_dict=True)
    logger.info(f"Validation metrics: {eval_results}")

    # Create EdgeModel
    model_config = ModelConfig(
        name="synapse_triage",
        model_type=ModelType.STRESS_CLASSIFIER,
        target_platform=TargetPlatform.ESP32_S3,
        input_shape=input_shape,
        num_classes=3,
        labels=["baseline", "stress", "artifact"],
        feature_names=[f"feat_{i}" for i in range(n_features)],
        sampling_rate=1.0,  # 1 window per 60s
        window_duration_s=60.0,
        architecture="cnn",
        hidden_units=64,
        num_layers=2,
        dropout=0.2,
        version="0.1.0",
    )

    edge_model = EdgeModel(
        config=model_config,
        model=model,
        history=history.history,
        metrics=eval_results,
    )

    # Save Keras model
    edge_model.model.save(output_dir / "synapse_triage.keras")
    logger.info(f"Saved Keras model to {output_dir / 'synapse_triage.keras'}")

    return edge_model


def main():
    parser = argparse.ArgumentParser(description="Train SYNAPSE-24 triage model")
    parser.add_argument("--data-dir", type=Path, default=Path("data"), help="Data root directory")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("models/triage"), help="Output directory"
    )
    parser.add_argument("--synthetic", action="store_true", help="Use synthetic data (for CI)")
    parser.add_argument("--epochs", type=int, default=50, help="Training epochs")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if keras is None:
        logger.error("TensorFlow/Keras not installed. Install with: pip install tensorflow-cpu")
        return 1

    try:
        edge_model = train_triage_model(
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            use_synthetic=args.synthetic,
            epochs=args.epochs,
            batch_size=args.batch_size,
        )
        logger.info("Training completed successfully!")
        return 0
    except Exception as e:
        logger.exception("Training failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
