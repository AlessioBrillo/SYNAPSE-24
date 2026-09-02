"""TFLM quantization for SYNAPSE-24 edge deployment."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

try:
    import tensorflow as tf
    from tensorflow import lite as tflite
except ImportError:
    tf = None
    tflite = None

from synapse24.edge_ai.model import EdgeModel, ModelConfig, TargetPlatform


@dataclass
class QuantizationConfig:
    """Configuration for TFLite quantization."""

    # Quantization mode
    quantization: str = "int8"  # int8, int16, float16, float32, dynamic

    # Representative dataset for post-training quantization
    representative_dataset: Callable[[], Generator[list[np.ndarray], None, None]] | None = None
    representative_dataset_size: int = 100

    # Optimization
    optimizations: list[str] = None  # ["DEFAULT", "EXPERIMENTAL_SPARSITY"]

    # Quantization-specific
    inference_input_type: str = "int8"  # int8, int16, float32
    inference_output_type: str = "int8"
    supported_ops: list[str] = None  # ["TFLITE_BUILTINS_INT8", "SELECT_TF_OPS"]

    # Experimental
    experimental_new_quantizer: bool = True
    experimental_new_converter: bool = True

    def __post_init__(self) -> None:
        if self.optimizations is None:
            self.optimizations = ["DEFAULT"]
        if self.supported_ops is None:
            self.supported_ops = ["TFLITE_BUILTINS", "SELECT_TF_OPS"]


class TFLMQuantizer:
    """Quantizes Keras models to TFLite/TFLM for edge deployment.

    Supports:
    - Dynamic range quantization (weights only)
    - Full integer quantization (weights + activations)
    - Float16 quantization
    - Custom quantization with representative dataset
    """

    def __init__(self, config: QuantizationConfig | None = None) -> None:
        self.config = config or QuantizationConfig()
        self._converter: Any | None = None

    def quantize(self, model: EdgeModel) -> EdgeModel:
        """Quantize an EdgeModel's Keras model to TFLite.

        Returns the same EdgeModel with tflite_model and tflm_model populated.
        """
        if tf is None or tflite is None:
            raise RuntimeError("TensorFlow not installed")

        if model.model is None:
            raise ValueError("No Keras model to quantize")

        # Save Keras model temporarily
        with tempfile.NamedTemporaryFile(suffix=".h5", delete=False) as f:
            keras_path = f.name
        model.model.save(keras_path)

        try:
            # Create converter
            converter = tflite.TFLiteConverter.from_keras_model_file(keras_path)
            self._converter = converter

            # Apply quantization config
            self._apply_quantization(converter, model.config)

            # Convert
            tflite_model = converter.convert()

            # Save TFLite model
            model.tflite_model = tflite_model

            # Generate TFLM model (with metadata)
            model.tflm_model = self._generate_tflm_model(tflite_model, model.config)

            return model

        finally:
            # Cleanup
            import os

            os.unlink(keras_path)

    def _apply_quantization(self, converter: Any, model_config: ModelConfig) -> None:
        """Apply quantization settings to converter."""
        quant_mode = self.config.quantization.lower()

        # Set optimizations
        opt_map = {
            "DEFAULT": tf.lite.Optimize.DEFAULT,
            "EXPERIMENTAL_SPARSITY": tf.lite.Optimize.EXPERIMENTAL_SPARSITY,
        }
        converter.optimizations = [
            opt_map.get(o, tf.lite.Optimize.DEFAULT) for o in self.config.optimizations
        ]

        # Set supported ops
        ops_map = {
            "TFLITE_BUILTINS": tf.lite.OpsSet.TFLITE_BUILTINS,
            "TFLITE_BUILTINS_INT8": tf.lite.OpsSet.TFLITE_BUILTINS_INT8,
            "SELECT_TF_OPS": tf.lite.OpsSet.SELECT_TF_OPS,
        }
        converter.target_spec.supported_ops = [
            ops_map.get(op, tf.lite.OpsSet.TFLITE_BUILTINS) for op in self.config.supported_ops
        ]

        if quant_mode == "float16":
            converter.target_spec.supported_types = [tf.float16]

        elif quant_mode == "int8":
            # Full integer quantization
            converter.inference_input_type = tf.int8
            converter.inference_output_type = tf.int8

            if self.config.representative_dataset:
                converter.representative_dataset = self._create_representative_dataset_gen(
                    self.config.representative_dataset, self.config.representative_dataset_size
                )
            else:
                # Generate from model's validation data if available
                pass  # Would need access to validation data

        elif quant_mode == "dynamic":
            # Dynamic range quantization (weights only)
            pass  # DEFAULT optimization handles this

        elif quant_mode == "float32":
            # No quantization
            pass

        # Experimental flags
        if hasattr(converter, "experimental_new_quantizer"):
            converter.experimental_new_quantizer = self.config.experimental_new_quantizer
        if hasattr(converter, "experimental_new_converter"):
            converter.experimental_new_converter = self.config.experimental_new_converter

    def _create_representative_dataset_gen(
        self, dataset_fn: Callable, size: int
    ) -> Generator[list[np.ndarray], None, None]:
        """Create generator for representative dataset."""
        for i, data in enumerate(dataset_fn()):
            if i >= size:
                break
            yield [data.astype(np.float32)]

    def _generate_tflm_model(self, tflite_model: bytes, model_config: ModelConfig) -> bytes:
        """Generate TFLM-compatible flatbuffer with metadata.

        For TFLM, we need to ensure the model uses only supported ops
        and includes metadata for the C++ library.
        """
        # For now, return the same flatbuffer
        # In production, would use flatc to compile with schema
        # and add metadata for input/output tensor names, quantization params
        return tflite_model

    def benchmark_tflite(
        self,
        tflite_model: bytes,
        input_data: np.ndarray,
        num_runs: int = 100,
    ) -> dict[str, float]:
        """Benchmark TFLite model inference time."""
        import time

        interpreter = tf.lite.Interpreter(model_content=tflite_model)
        interpreter.allocate_tensors()

        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()

        # Warmup
        for _ in range(10):
            interpreter.set_tensor(input_details[0]["index"], input_data.astype(np.float32))
            interpreter.invoke()

        # Benchmark
        times = []
        for _ in range(num_runs):
            start = time.perf_counter()
            interpreter.set_tensor(input_details[0]["index"], input_data.astype(np.float32))
            interpreter.invoke()
            times.append(time.perf_counter() - start)

        times = np.array(times) * 1000  # ms

        return {
            "mean_ms": float(np.mean(times)),
            "std_ms": float(np.std(times)),
            "min_ms": float(np.min(times)),
            "max_ms": float(np.max(times)),
            "p99_ms": float(np.percentile(times, 99)),
        }

    def validate_tflite_output(
        self,
        keras_model: Any,
        tflite_model: bytes,
        test_data: np.ndarray,
        tolerance: float = 1e-3,
    ) -> dict[str, Any]:
        """Validate TFLite output matches Keras output."""
        # Keras prediction
        keras_out = keras_model.predict(test_data, verbose=0)

        # TFLite prediction
        interpreter = tf.lite.Interpreter(model_content=tflite_model)
        interpreter.allocate_tensors()

        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()

        interpreter.set_tensor(input_details[0]["index"], test_data.astype(np.float32))
        interpreter.invoke()
        tflite_out = interpreter.get_tensor(output_details[0]["index"])

        # Compare
        max_diff = np.max(np.abs(keras_out - tflite_out))
        mean_diff = np.mean(np.abs(keras_out - tflite_out))

        return {
            "max_diff": float(max_diff),
            "mean_diff": float(mean_diff),
            "passes": max_diff < tolerance,
            "tolerance": tolerance,
        }

    def get_model_info(self, tflite_model: bytes) -> dict[str, Any]:
        """Get detailed TFLite model information."""
        interpreter = tf.lite.Interpreter(model_content=tflite_model)
        interpreter.allocate_tensors()

        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()

        return {
            "input_shape": input_details[0]["shape"].tolist(),
            "input_type": str(input_details[0]["dtype"]),
            "input_quantization": input_details[0]["quantization"],
            "output_shape": output_details[0]["shape"].tolist(),
            "output_type": str(output_details[0]["dtype"]),
            "output_quantization": output_details[0]["quantization"],
            "model_size_kb": len(tflite_model) / 1024,
            "num_tensors": len(interpreter.get_tensor_details()),
        }
