"""Edge AI model configuration and management."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import numpy as np
import numpy.typing as npt


class ModelType(Enum):
    """Supported model architectures."""

    # Stress/affect classification
    STRESS_CLASSIFIER = "stress_classifier"
    STRESS_BINARY = "stress_binary"
    AFFECT_CLASSIFIER = "affect_classifier"

    # Sleep staging
    SLEEP_STAGING = "sleep_staging"

    # Anomaly detection
    ECG_ANOMALY = "ecg_anomaly"
    PPG_QUALITY = "ppg_quality"
    EEG_ARTIFACT = "eeg_artifact"

    # Fusion
    MULTIMODAL_FUSION = "multimodal_fusion"
    CARDIO_NEURO_FUSION = "cardio_neuro_fusion"

    # Custom
    CUSTOM = "custom"


class TargetPlatform(Enum):
    """Target deployment platforms."""

    ESP32 = "esp32"
    ESP32_S3 = "esp32s3"
    RASPBERRY_PI_PICO = "pico"
    RASPBERRY_PI = "rpi"
    GENERIC_CORTEX_M = "cortex-m"
    HOST = "host"  # For testing


@dataclass
class ModelConfig:
    """Configuration for edge AI model."""

    model_type: ModelType
    input_shape: tuple[int, ...]  # (window_size, n_channels) or (n_features,)
    num_classes: int
    sampling_rate: int
    window_duration_s: float

    # Architecture
    architecture: str = "lstm"  # lstm, cnn, tcn, transformer, mlp
    hidden_units: int = 64
    num_layers: int = 2
    dropout: float = 0.2

    # Training
    learning_rate: float = 0.001
    batch_size: int = 32
    epochs: int = 50
    validation_split: float = 0.2
    class_weights: dict[int, float] | None = None

    # Quantization
    quantization: str = "int8"  # int8, int16, float16, float32
    representative_dataset_size: int = 100

    # Target
    target_platform: TargetPlatform = TargetPlatform.ESP32
    target_accelerator: str = ""  # e.g., "esp-nn", "cmsis-nn"

    # Metadata
    name: str = ""
    version: str = "1.0.0"
    description: str = ""
    labels: list[str] = field(default_factory=list)
    feature_names: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = f"{self.model_type.value}_{self.architecture}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_type": self.model_type.value,
            "input_shape": self.input_shape,
            "num_classes": self.num_classes,
            "sampling_rate": self.sampling_rate,
            "window_duration_s": self.window_duration_s,
            "architecture": self.architecture,
            "hidden_units": self.hidden_units,
            "num_layers": self.num_layers,
            "dropout": self.dropout,
            "learning_rate": self.learning_rate,
            "batch_size": self.batch_size,
            "epochs": self.epochs,
            "validation_split": self.validation_split,
            "class_weights": self.class_weights,
            "quantization": self.quantization,
            "representative_dataset_size": self.representative_dataset_size,
            "target_platform": self.target_platform.value,
            "target_accelerator": self.target_accelerator,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "labels": self.labels,
            "feature_names": self.feature_names,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelConfig:
        return cls(
            model_type=ModelType(data["model_type"]),
            input_shape=tuple(data["input_shape"]),
            num_classes=data["num_classes"],
            sampling_rate=data["sampling_rate"],
            window_duration_s=data["window_duration_s"],
            architecture=data.get("architecture", "lstm"),
            hidden_units=data.get("hidden_units", 64),
            num_layers=data.get("num_layers", 2),
            dropout=data.get("dropout", 0.2),
            learning_rate=data.get("learning_rate", 0.001),
            batch_size=data.get("batch_size", 32),
            epochs=data.get("epochs", 50),
            validation_split=data.get("validation_split", 0.2),
            class_weights=data.get("class_weights"),
            quantization=data.get("quantization", "int8"),
            representative_dataset_size=data.get("representative_dataset_size", 100),
            target_platform=TargetPlatform(data.get("target_platform", "esp32")),
            target_accelerator=data.get("target_accelerator", ""),
            name=data.get("name", ""),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            labels=data.get("labels", []),
            feature_names=data.get("feature_names", []),
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: Path) -> ModelConfig:
        with open(path) as f:
            data = json.load(f)
        return cls.from_dict(data)

    @classmethod
    def stress_3class_wesad(cls) -> ModelConfig:
        """Predefined config for WESAD 3-class stress classification."""
        return cls(
            model_type=ModelType.STRESS_CLASSIFIER,
            input_shape=(128, 12),  # 12 features from ECG+PPG+EDA+ACC
            num_classes=3,
            sampling_rate=700,
            window_duration_s=60,
            architecture="lstm",
            hidden_units=64,
            num_layers=2,
            labels=["baseline", "stress", "amusement"],
            feature_names=[
                "hrv_mean_rr",
                "hrv_sdnn",
                "hrv_rmssd",
                "hrv_pnn50",
                "hrv_lf_power",
                "hrv_hf_power",
                "hrv_lf_hf_ratio",
                "ppg_sqi",
                "ppg_pi",
                "ppg_map",
                "eda_tonic",
                "eda_scr_rate",
            ],
        )

    @classmethod
    def stress_binary_wesad(cls) -> ModelConfig:
        """Predefined config for WESAD binary stress classification."""
        cfg = cls.stress_3class_wesad()
        cfg.model_type = ModelType.STRESS_BINARY
        cfg.num_classes = 2
        cfg.labels = ["non_stress", "stress"]
        return cfg

    @classmethod
    def sleep_staging_sleep_edf(cls) -> ModelConfig:
        """Predefined config for Sleep-EDF sleep staging."""
        return cls(
            model_type=ModelType.SLEEP_STAGING,
            input_shape=(3000, 2),  # 30s * 100Hz, 2 EEG channels
            num_classes=5,  # W, N1, N2, N3, REM
            sampling_rate=100,
            window_duration_s=30,
            architecture="cnn_lstm",
            hidden_units=128,
            num_layers=3,
            labels=["W", "N1", "N2", "N3", "REM"],
            feature_names=["EEG_Fpz-Cz", "EEG_Pz-Oz"],
        )


class EdgeModel:
    """Wrapper for trained edge model with metadata."""

    def __init__(
        self,
        config: ModelConfig,
        model: Any = None,  # Keras/TFLite model
        history: dict[str, list[float]] | None = None,
        metrics: dict[str, float] | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.history = history or {}
        self.metrics = metrics or {}
        self.tflite_model: bytes | None = None
        self.tflm_model: bytes | None = None

    def predict(self, x: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Run inference."""
        if self.model is None:
            raise ValueError("Model not loaded")
        return self.model.predict(x, verbose=0)

    def evaluate(self, x: npt.NDArray[np.float64], y: npt.NDArray[np.float64]) -> dict[str, float]:
        """Evaluate model."""
        if self.model is None:
            raise ValueError("Model not loaded")
        results = self.model.evaluate(x, y, verbose=0, return_dict=True)
        return results

    def save_keras(self, path: Path) -> None:
        """Save Keras model."""
        if self.model is None:
            raise ValueError("No Keras model to save")
        self.model.save(path)

    def save_tflite(self, path: Path) -> None:
        """Save TFLite model."""
        if self.tflite_model is None:
            raise ValueError("No TFLite model to save")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(self.tflite_model)

    def save_tflm(self, path: Path) -> None:
        """Save TFLM model (flatbuffer with metadata)."""
        if self.tflm_model is None:
            raise ValueError("No TFLM model to save")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            f.write(self.tflm_model)

    def get_model_size_kb(self) -> float:
        """Get model size in KB."""
        if self.tflite_model:
            return len(self.tflite_model) / 1024
        if self.model:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".h5", delete=False) as f:
                self.model.save(f.name)
                import os

                size = os.path.getsize(f.name)
                os.unlink(f.name)
                return size / 1024
        return 0.0

    def get_estimated_ram_kb(self) -> float:
        """Estimate RAM usage for TFLM inference."""
        if self.tflite_model:
            # Rough estimate: model size + arena (2-4x model size)
            model_kb = self.get_model_size_kb()
            return model_kb * 3
        return 0.0
