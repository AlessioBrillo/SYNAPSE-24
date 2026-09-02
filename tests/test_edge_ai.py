"""Tests for edge AI quantization and deployment."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

try:
    import tensorflow as tf
    from tensorflow import keras
except ImportError:
    tf = None
    keras = None

from synapse24.edge_ai import (
    DeploymentConfig,
    EdgeModel,
    ModelConfig,
    ModelType,
    TargetPlatform,
    deploy_model,
    quantize_model,
)
from synapse24.edge_ai.quantization import QuantizationConfig, RepresentativeDatasetGenerator


@pytest.mark.skipif(tf is None, reason="TensorFlow not installed")
class TestQuantization:
    """Tests for TFLM quantization pipeline."""

    def create_test_model(self) -> EdgeModel:
        """Create a simple test model."""
        config = ModelConfig(
            model_type=ModelType.STRESS_BINARY,
            input_shape=(10, 12),  # 10 timesteps, 12 features
            num_classes=2,
            sampling_rate=700,
            window_duration_s=60,
            architecture="lstm",
            hidden_units=32,
            num_layers=1,
        )

        # Build simple LSTM model
        inputs = keras.Input(shape=config.input_shape)
        x = keras.layers.LSTM(32, return_sequences=False)(inputs)
        x = keras.layers.Dense(16, activation="relu")(x)
        outputs = keras.layers.Dense(1, activation="sigmoid")(x)
        model = keras.Model(inputs, outputs, name=config.name)

        model.compile(
            optimizer="adam",
            loss="binary_crossentropy",
            metrics=["accuracy"],
        )

        # Dummy training to initialize weights
        X_dummy = np.random.randn(100, *config.input_shape).astype(np.float32)
        y_dummy = np.random.randint(0, 2, 100).astype(np.float32)
        model.fit(X_dummy, y_dummy, epochs=1, verbose=0)

        return EdgeModel(config=config, model=model)

    def test_quantization_int8(self):
        """Test int8 post-training quantization."""
        edge_model = self.create_test_model()

        quant_config = QuantizationConfig(
            quantization_type="int8",
            representative_dataset_size=50,
            target_platform=TargetPlatform.ESP32_S3,
        )

        # Generate validation data
        X_val = np.random.randn(50, *edge_model.config.input_shape).astype(np.float32)
        y_val = np.random.randint(0, 2, 50).astype(np.float32)

        result = quantize_model(edge_model, quant_config, validation_data=(X_val, y_val))

        assert result.tflite_model is not None
        assert len(result.tflite_model) > 0
        assert result.model_size_kb > 0
        assert result.estimated_ram_kb > 0
        assert len(result.input_details) > 0
        assert len(result.output_details) > 0
        # Accuracy drop should be reasonable (< 5% for simple model)
        assert result.accuracy_drop_percent < 5.0

    def test_quantization_float16(self):
        """Test float16 quantization."""
        edge_model = self.create_test_model()

        quant_config = QuantizationConfig(
            quantization_type="float16",
            target_platform=TargetPlatform.ESP32_S3,
        )

        result = quantize_model(edge_model, quant_config)

        assert result.tflite_model is not None
        assert result.model_size_kb > 0
        # Float16 should be ~half the size of float32
        assert result.model_size_kb < 100  # Small model

    def test_quantization_float32(self):
        """Test float32 (no quantization)."""
        edge_model = self.create_test_model()

        quant_config = QuantizationConfig(
            quantization_type="float32",
            target_platform=TargetPlatform.ESP32_S3,
        )

        result = quantize_model(edge_model, quant_config)

        assert result.tflite_model is not None
        assert result.model_size_kb > 0

    def test_save_artifacts(self):
        """Test saving quantization artifacts."""
        edge_model = self.create_test_model()

        quant_config = QuantizationConfig(quantization_type="int8")
        result = quantize_model(edge_model, quant_config)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            artifacts = save_quantization_artifacts(result, edge_model, output_dir)

            assert artifacts["tflite"].exists()
            assert artifacts["metadata"].exists()
            assert artifacts["header"].exists()

            # Verify metadata content
            metadata = json.loads(artifacts["metadata"].read_text())
            assert metadata["model_name"] == edge_model.config.name
            assert "quantization" in metadata
            assert metadata["quantization"]["model_size_kb"] == result.model_size_kb

            # Verify C header
            header_content = artifacts["header"].read_text()
            assert edge_model.config.name in header_content
            assert "tflite" in header_content.lower()

    def test_representative_dataset_generator(self):
        """Test representative dataset generation."""
        config = ModelConfig(
            model_type=ModelType.STRESS_BINARY,
            input_shape=(10, 12),
            num_classes=2,
            sampling_rate=700,
            window_duration_s=60,
        )

        generator = RepresentativeDatasetGenerator(config)
        data = generator.generate(50)

        assert data.shape == (50, 10, 12)
        assert data.dtype == np.float32

    def test_deployment(self):
        """Test deployment artifact generation."""
        edge_model = self.create_test_model()

        quant_config = QuantizationConfig(quantization_type="int8")
        quant_result = quantize_model(edge_model, quant_config)

        deploy_config = DeploymentConfig(
            target_platform=TargetPlatform.ESP32_S3,
            arena_size_kb=0,
            generate_example=True,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = deploy_model(edge_model, quant_result, deploy_config, output_dir)

            assert result.validation_passed
            assert result.tflite_path.exists()
            assert result.header_path.exists()
            assert result.metadata_path.exists()
            assert result.cmake_path is not None
            assert result.cmake_path.exists()
            assert result.estimated_latency_ms > 0

            # Check example file exists
            example_path = output_dir / "inference.cpp"
            assert example_path.exists()

    def test_deployment_report(self):
        """Test deployment report generation."""
        edge_model = self.create_test_model()

        quant_config = QuantizationConfig(quantization_type="int8")
        quant_result = quantize_model(edge_model, quant_config)

        deploy_config = DeploymentConfig(target_platform=TargetPlatform.ESP32_S3)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            result = deploy_model(edge_model, quant_result, deploy_config, output_dir)

            report_path = output_dir / "DEPLOYMENT_REPORT.md"
            from synapse24.edge_ai.deployment import generate_deployment_report

            generate_deployment_report(result, report_path)

            assert report_path.exists()
            content = report_path.read_text()
            assert "SYNAPSE-24 Edge AI Deployment Report" in content
            assert "READY FOR DEPLOYMENT" in content


@pytest.mark.skipif(tf is None, reason="TensorFlow not installed")
class TestModelConfig:
    """Tests for ModelConfig presets."""

    def test_stress_3class_wesad(self):
        """Test WESAD 3-class stress config."""
        config = ModelConfig.stress_3class_wesad()
        assert config.model_type == ModelType.STRESS_CLASSIFIER
        assert config.num_classes == 3
        assert config.input_shape == (128, 12)
        assert len(config.labels) == 3
        assert len(config.feature_names) == 12

    def test_stress_binary_wesad(self):
        """Test WESAD binary stress config."""
        config = ModelConfig.stress_binary_wesad()
        assert config.model_type == ModelType.STRESS_BINARY
        assert config.num_classes == 2
        assert config.labels == ["non_stress", "stress"]

    def test_sleep_staging_sleep_edf(self):
        """Test Sleep-EDF sleep staging config."""
        config = ModelConfig.sleep_staging_sleep_edf()
        assert config.model_type == ModelType.SLEEP_STAGING
        assert config.num_classes == 5
        assert config.input_shape == (3000, 2)
        assert config.labels == ["W", "N1", "N2", "N3", "REM"]

    def test_save_load_config(self):
        """Test config serialization."""
        config = ModelConfig.stress_binary_wesad()

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "config.json"
            config.save(path)

            loaded = ModelConfig.load(path)
            assert loaded.model_type == config.model_type
            assert loaded.input_shape == config.input_shape
            assert loaded.num_classes == config.num_classes
            assert loaded.labels == config.labels


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
