"""Edge Impulse training integration for SYNAPSE-24."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import numpy.typing as npt

try:
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras import layers
except ImportError:
    tf = None
    keras = None
    layers = None

from synapse24.edge_ai.model import EdgeModel, ModelConfig, ModelType, TargetPlatform


@dataclass
class TrainingConfig:
    """Configuration for Edge Impulse / local training."""

    # Data
    train_data: npt.NDArray[np.float64] | None = None  # (n_samples, n_timesteps, n_features)
    train_labels: npt.NDArray[np.int64] | None = None
    val_data: npt.NDArray[np.float64] | None = None
    val_labels: npt.NDArray[np.int64] | None = None

    # Edge Impulse
    ei_api_key: str = ""
    ei_project_id: str = ""
    ei_dataset_id: str = ""

    # Local training fallback
    use_local_training: bool = True
    local_epochs: int = 50
    local_batch_size: int = 32
    local_learning_rate: float = 0.001
    early_stopping_patience: int = 10
    reduce_lr_patience: int = 5

    # Augmentation
    augment: bool = True
    noise_std: float = 0.01
    time_warp_sigma: float = 0.2

    # Callbacks
    callbacks: list[Any] = field(default_factory=list)


class EdgeImpulseTrainer:
    """Trains models using Edge Impulse Studio or locally with TensorFlow/Keras.

    For Edge Impulse: requires API key and project setup at studio.edgeimpulse.com
    For local: uses TensorFlow/Keras with architectures suitable for TFLM.
    """

    def __init__(self, config: TrainingConfig, model_config: ModelConfig) -> None:
        self.config = config
        self.model_config = model_config
        self._model: EdgeModel | None = None

    def train(self) -> EdgeModel:
        """Train model - tries Edge Impulse first, falls back to local."""
        if not self.config.use_local_training and self.config.ei_api_key:
            return self._train_edge_impulse()
        return self._train_local()

    def _train_edge_impulse(self) -> EdgeModel:
        """Train using Edge Impulse Python SDK."""
        try:
            from edgeimpulse import API, Project
        except ImportError:
            print("Edge Impulse SDK not installed, falling back to local training")
            return self._train_local()

        # This is a simplified placeholder - full implementation would:
        # 1. Create project or use existing
        # 2. Upload data via API
        # 3. Configure impulse (preprocessing + learning block)
        # 4. Train model
        # 5. Download trained model

        print("Edge Impulse training not fully implemented, using local fallback")
        return self._train_local()

    def _train_local(self) -> EdgeModel:
        """Train locally with Keras/TensorFlow."""
        if keras is None:
            raise RuntimeError("TensorFlow/Keras not installed")

        # Prepare data
        x_train, y_train = self._prepare_data()
        x_val, y_val = self._get_validation_data()

        # Build model
        model = self._build_model()

        # Compile
        optimizer = keras.optimizers.Adam(learning_rate=self.config.local_learning_rate)
        if self.model_config.num_classes == 2:
            loss = "binary_crossentropy"
            metrics = ["accuracy", keras.metrics.AUC(name="auc")]
        else:
            loss = "sparse_categorical_crossentropy"
            metrics = ["accuracy"]

        model.compile(optimizer=optimizer, loss=loss, metrics=metrics)

        # Callbacks
        callbacks = [
            keras.callbacks.EarlyStopping(
                monitor="val_loss",
                patience=self.config.early_stopping_patience,
                restore_best_weights=True,
            ),
            keras.callbacks.ReduceLROnPlateau(
                monitor="val_loss",
                factor=0.5,
                patience=self.config.reduce_lr_patience,
                min_lr=1e-6,
            ),
        ] + self.config.callbacks

        # Train
        history = model.fit(
            x_train,
            y_train,
            validation_data=(x_val, y_val) if x_val is not None else None,
            epochs=self.config.local_epochs,
            batch_size=self.config.local_batch_size,
            callbacks=callbacks,
            verbose=1,
            class_weight=self.model_config.class_weights,
        )

        # Evaluate
        eval_metrics: dict[str, float] = {}
        if x_val is not None:
            eval_results = model.evaluate(x_val, y_val, verbose=0, return_dict=True)
            eval_metrics = eval_results

        # Create EdgeModel
        edge_model = EdgeModel(
            config=self.model_config,
            model=model,
            history=history.history,
            metrics=eval_metrics,
        )

        self._model = edge_model
        return edge_model

    def _prepare_data(self) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int64]]:
        """Prepare training data with augmentation."""
        if self.config.train_data is None or self.config.train_labels is None:
            raise ValueError("Training data not provided")

        x = self.config.train_data.copy()
        y = self.config.train_labels.copy()

        if self.config.augment:
            x = self._augment_data(x)

        return x, y

    def _get_validation_data(
        self,
    ) -> tuple[npt.NDArray[np.float64] | None, npt.NDArray[np.int64] | None]:
        """Get validation data."""
        if self.config.val_data is not None and self.config.val_labels is not None:
            return self.config.val_data, self.config.val_labels
        return None, None

    def _augment_data(self, x: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Apply data augmentation for time series."""
        augmented = [x]

        # Gaussian noise
        noise = np.random.normal(0, self.config.noise_std, x.shape)
        augmented.append(x + noise)

        # Time warping (simplified)
        if x.ndim == 3 and x.shape[1] > 10:
            warped = self._time_warp(x)
            augmented.append(warped)

        return np.concatenate(augmented, axis=0)

    def _time_warp(self, x: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Simple time warping augmentation."""
        n_samples, n_timesteps, n_features = x.shape
        warped = np.zeros_like(x)

        for i in range(n_samples):
            # Random time warp
            warp_factor = 1 + np.random.normal(0, self.config.time_warp_sigma)
            warp_factor = np.clip(warp_factor, 0.8, 1.2)

            new_timesteps = int(n_timesteps * warp_factor)
            new_timesteps = max(new_timesteps, 10)

            for f in range(n_features):
                warped[i, :, f] = np.interp(
                    np.linspace(0, n_timesteps - 1, n_timesteps),
                    np.linspace(0, n_timesteps - 1, new_timesteps),
                    np.interp(
                        np.linspace(0, n_timesteps - 1, new_timesteps),
                        np.arange(n_timesteps),
                        x[i, :, f],
                    ),
                )

        return warped

    def _build_model(self) -> keras.Model:
        """Build Keras model based on architecture."""
        input_shape = self.model_config.input_shape

        if self.model_config.architecture == "lstm":
            return self._build_lstm(input_shape)
        if self.model_config.architecture == "cnn":
            return self._build_cnn(input_shape)
        if self.model_config.architecture == "cnn_lstm":
            return self._build_cnn_lstm(input_shape)
        if self.model_config.architecture == "tcn":
            return self._build_tcn(input_shape)
        if self.model_config.architecture == "mlp":
            return self._build_mlp(input_shape)
        raise ValueError(f"Unknown architecture: {self.model_config.architecture}")

    def _build_lstm(self, input_shape: tuple[int, ...]) -> Any:
        """Build LSTM model."""
        inputs = keras.Input(shape=input_shape)
        x = inputs

        for i in range(self.model_config.num_layers):
            return_sequences = i < self.model_config.num_layers - 1
            x = layers.LSTM(
                self.model_config.hidden_units,
                return_sequences=return_sequences,
                dropout=self.model_config.dropout,
                recurrent_dropout=self.model_config.dropout,
            )(x)

        x = layers.Dense(self.model_config.hidden_units // 2, activation="relu")(x)
        x = layers.Dropout(self.model_config.dropout)(x)

        if self.model_config.num_classes == 2:
            outputs = layers.Dense(1, activation="sigmoid")(x)
        else:
            outputs = layers.Dense(self.model_config.num_classes, activation="softmax")(x)

        return keras.Model(inputs, outputs, name=self.model_config.name)

    def _build_cnn(self, input_shape: tuple[int, ...]) -> Any:
        """Build 1D CNN model."""
        inputs = keras.Input(shape=input_shape)
        x = inputs

        # Conv blocks
        for filters in [32, 64, 128]:
            x = layers.Conv1D(filters, 3, padding="same", activation="relu")(x)
            x = layers.BatchNormalization()(x)
            x = layers.MaxPooling1D(2)(x)
            x = layers.Dropout(self.model_config.dropout)(x)

        x = layers.GlobalAveragePooling1D()(x)
        x = layers.Dense(self.model_config.hidden_units, activation="relu")(x)
        x = layers.Dropout(self.model_config.dropout)(x)

        if self.model_config.num_classes == 2:
            outputs = layers.Dense(1, activation="sigmoid")(x)
        else:
            outputs = layers.Dense(self.model_config.num_classes, activation="softmax")(x)

        return keras.Model(inputs, outputs, name=self.model_config.name)

    def _build_cnn_lstm(self, input_shape: tuple[int, ...]) -> Any:
        """Build CNN-LSTM hybrid model."""
        inputs = keras.Input(shape=input_shape)
        x = inputs

        # CNN feature extraction
        x = layers.Conv1D(64, 5, padding="same", activation="relu")(x)
        x = layers.BatchNormalization()(x)
        x = layers.MaxPooling1D(2)(x)
        x = layers.Conv1D(128, 3, padding="same", activation="relu")(x)
        x = layers.BatchNormalization()(x)
        x = layers.Dropout(self.model_config.dropout)(x)

        # LSTM sequence modeling
        x = layers.LSTM(
            self.model_config.hidden_units,
            return_sequences=False,
            dropout=self.model_config.dropout,
        )(x)

        x = layers.Dense(self.model_config.hidden_units // 2, activation="relu")(x)
        x = layers.Dropout(self.model_config.dropout)(x)

        if self.model_config.num_classes == 2:
            outputs = layers.Dense(1, activation="sigmoid")(x)
        else:
            outputs = layers.Dense(self.model_config.num_classes, activation="softmax")(x)

        return keras.Model(inputs, outputs, name=self.model_config.name)

    def _build_tcn(self, input_shape: tuple[int, ...]) -> Any:
        """Build Temporal Convolutional Network (dilated CNN)."""
        inputs = keras.Input(shape=input_shape)
        x = inputs

        # TCN blocks with increasing dilation
        for i, dilation in enumerate([1, 2, 4, 8, 16]):
            x = layers.Conv1D(
                self.model_config.hidden_units,
                3,
                padding="causal",
                dilation_rate=dilation,
                activation="relu",
            )(x)
            x = layers.BatchNormalization()(x)
            x = layers.Dropout(self.model_config.dropout)(x)
            x = layers.Conv1D(
                self.model_config.hidden_units,
                3,
                padding="causal",
                dilation_rate=dilation,
                activation="relu",
            )(x)
            x = layers.BatchNormalization()(x)
            x = layers.Dropout(self.model_config.dropout)(x)

        x = layers.GlobalAveragePooling1D()(x)
        x = layers.Dense(self.model_config.hidden_units // 2, activation="relu")(x)
        x = layers.Dropout(self.model_config.dropout)(x)

        if self.model_config.num_classes == 2:
            outputs = layers.Dense(1, activation="sigmoid")(x)
        else:
            outputs = layers.Dense(self.model_config.num_classes, activation="softmax")(x)

        return keras.Model(inputs, outputs, name=self.model_config.name)

    def _build_mlp(self, input_shape: tuple[int, ...]) -> Any:
        """Build MLP model (for pre-extracted features)."""
        # Flatten if time series
        if len(input_shape) > 1:
            flat_size = np.prod(input_shape)
            inputs = keras.Input(shape=input_shape)
            x = layers.Flatten()(inputs)
        else:
            inputs = keras.Input(shape=input_shape)
            x = inputs

        for units in [256, 128, 64]:
            x = layers.Dense(units, activation="relu")(x)
            x = layers.BatchNormalization()(x)
            x = layers.Dropout(self.model_config.dropout)(x)

        if self.model_config.num_classes == 2:
            outputs = layers.Dense(1, activation="sigmoid")(x)
        else:
            outputs = layers.Dense(self.model_config.num_classes, activation="softmax")(x)

        return keras.Model(inputs, outputs, name=self.model_config.name)


def create_wesad_training_data(
    wesad_results: list[dict[str, Any]],
    config: ModelConfig,
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int64]]:
    """Create training data from WESAD ingestion results.

    Extracts features from each segment for stress classification.
    """
    from sklearn.preprocessing import StandardScaler

    all_features = []
    all_labels = []

    label_map = {"baseline": 0, "stress": 1, "amusement": 2}

    for result in wesad_results:
        segments = result.get("segments", {})
        for seg_name, seg_data in segments.items():
            if seg_name not in label_map:
                continue

            ecg_q = seg_data.get("ecg_quality", {})
            ppg_q = seg_data.get("ppg_quality", {})

            hrv = ecg_q.get("metrics", {}).get("ecg", {}).get("hrv_metrics", {})

            features = [
                hrv.get("mean_rr_ms", 0),
                hrv.get("sdnn_ms", 0),
                hrv.get("rmssd_ms", 0),
                hrv.get("pnn50", 0),
                hrv.get("lf_power", 0),
                hrv.get("hf_power", 0),
                hrv.get("lf_hf_ratio", 0),
                ppg_q.get("ppg_sqi", 0),
                ppg_q.get("perfusion_index", 0),
                ppg_q.get("motion_artifact_prob", 0),
            ]

            all_features.append(features)
            all_labels.append(label_map[seg_name])

    X = np.array(all_features)
    y = np.array(all_labels)

    # Scale features
    scaler = StandardScaler()
    X = scaler.fit_transform(X)

    # Reshape for time series models (add time dimension)
    if len(config.input_shape) == 2:
        # For LSTM/TCN: (n_samples, n_timesteps, n_features)
        # We treat each segment as one timestep with multiple features
        # Or we could create sliding windows
        X = X.reshape(-1, 1, X.shape[1])

    return X, y
