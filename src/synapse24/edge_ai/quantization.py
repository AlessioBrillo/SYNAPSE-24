"""TFLM quantization pipeline for SYNAPSE-24 edge AI models."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

try:
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.lite.python import converter as tf_lite_converter
except ImportError:  # pragma: no cover
    tf = None
    keras = None
    tf_lite_converter = None

from synapse24.edge_ai.model import EdgeModel, ModelConfig, TargetPlatform


@dataclass
class QuantizationConfig:
    """Configuration for post-training quantization."""

    quantization_type: str = "int8"  # int8, int16, float16, float32
    representative_dataset_size: int = 100
    representative_dataset_fn: Optional[Callable[[], np.ndarray]] = None
    inference_input_type: str = "int8"  # int8, float32
    inference_output_type: str = "int8"  # int8, float32
    supported_ops: list[str] = None  # ["TFLITE_BUILTINS_INT8", "SELECT_TF_OPS"]
    target_platform: TargetPlatform = TargetPlatform.ESP32_S3

    def __post_init__(self) -> None:
        if self.supported_ops is None:
            if self.quantization_type == "int8":
                self.supported_ops = ["TFLITE_BUILTINS_INT8"]
            elif self.quantization_type == "float16":
                self.supported_ops = ["TFLITE_BUILTINS", "TFLITE_BUILTINS_INT8"]
            else:
                self.supported_ops = ["TFLITE_BUILTINS"]


@dataclass
class QuantizationResult:
    """Result of quantization process."""

    tflite_model: bytes
    model_size_kb: float
    estimated_ram_kb: float
    ops_used: list[str]
    input_details: list[dict]
    output_details: list[dict]
    accuracy_drop_percent: float = 0.0
    calibration_stats: dict = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_size_kb": self.model_size_kb,
            "estimated_ram_kb": self.estimated_ram_kb,
            "ops_used": self.ops_used,
            "input_details": self.input_details,
            "output_details": self.output_details,
            "accuracy_drop_percent": self.accuracy_drop_percent,
            "calibration_stats": self.calibration_stats or {},
        }


class RepresentativeDatasetGenerator:
    """Generates representative dataset for post-training quantization calibration."""

    def __init__(
        self,
        model_config: ModelConfig,
        feature_scaler: Any = None,
        wesad_results: list[dict] | None = None,
    ) -> None:
        self.model_config = model_config
        self.feature_scaler = feature_scaler
        self.wesad_results = wesad_results
        self._cached_data: np.ndarray | None = None

    def generate(self, num_samples: int = 100) -> np.ndarray:
        """Generate representative dataset for calibration."""
        if self._cached_data is not None and len(self._cached_data) >= num_samples:
            return self._cached_data[:num_samples]

        if self.wesad_results:
            data = self._from_wesad_results(num_samples)
        else:
            data = self._synthetic(num_samples)

        self._cached_data = data
        return data

    def _from_wesad_results(self, num_samples: int) -> np.ndarray:
        """Extract real features from WESAD ingestion results."""
        from synapse24.edge_ai.training import create_wesad_training_data

        X, _ = create_wesad_training_data(self.wesad_results, self.model_config)
        if self.feature_scaler:
            original_shape = X.shape
            X = X.reshape(-1, X.shape[-1])
            X = self.feature_scaler.transform(X)
            X = X.reshape(original_shape)

        # Ensure we have enough samples
        if len(X) < num_samples:
            # Repeat with noise
            repeats = (num_samples // len(X)) + 1
            X = np.tile(X, (repeats, 1, 1))
            noise = np.random.normal(0, 0.01, X.shape).astype(np.float32)
            X = X + noise

        return X[:num_samples].astype(np.float32)

    def _synthetic(self, num_samples: int) -> np.ndarray:
        """Generate synthetic representative data matching input shape."""
        input_shape = self.model_config.input_shape
        if len(input_shape) == 2 or len(input_shape) == 1:
            data = np.random.randn(num_samples, *input_shape).astype(np.float32)
        else:
            raise ValueError(f"Unsupported input shape: {input_shape}")
        return data


def quantize_model(
    edge_model: EdgeModel,
    quant_config: QuantizationConfig,
    representative_data: np.ndarray | None = None,
    validation_data: tuple[np.ndarray, np.ndarray] | None = None,
) -> QuantizationResult:
    """Quantize a trained Keras model to TFLite with optional int8 quantization.

    Args:
        edge_model: Trained EdgeModel with Keras model
        quant_config: Quantization configuration
        representative_data: Calibration data (n_samples, *input_shape). If None, generates synthetic.
        validation_data: Optional (X_val, y_val) to measure accuracy drop

    Returns:
        QuantizationResult with TFLite model bytes and metadata
    """
    if tf is None or keras is None:
        raise RuntimeError("TensorFlow not installed")

    if edge_model.model is None:
        raise ValueError("EdgeModel has no Keras model to quantize")

    # Get representative dataset
    if representative_data is None:
        generator = RepresentativeDatasetGenerator(edge_model.config)
        representative_data = generator.generate(quant_config.representative_dataset_size)

    def representative_dataset_gen():
        for i in range(len(representative_data)):
            yield [representative_data[i : i + 1]]

    # Convert to TFLite
    converter = tf.lite.TFLiteConverter.from_keras_model(edge_model.model)

    if quant_config.quantization_type == "int8":
        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        converter.representative_dataset = representative_dataset_gen
        converter.target_spec.supported_ops = quant_config.supported_ops
        converter.inference_input_type = getattr(tf.lite, quant_config.inference_input_type)
        converter.inference_output_type = getattr(tf.lite, quant_config.inference_output_type)

        # Ensure full integer quantization
        converter.target_spec.supported_types = [tf.int8]

    elif quant_config.quantization_type == "float16":
        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        converter.target_spec.supported_types = [tf.float16]

    elif quant_config.quantization_type == "int16":
        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        converter.target_spec.supported_types = [tf.int16]

    # float32: no quantization, just convert

    tflite_model = converter.convert()

    # Analyze model
    interpreter = tf.lite.Interpreter(model_content=tflite_model)
    interpreter.allocate_tensors()

    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    ops_used = _get_ops_used(tflite_model)
    model_size_kb = len(tflite_model) / 1024
    estimated_ram_kb = model_size_kb * 3  # Heuristic: model + arena + buffers

    # Compute accuracy drop if validation data provided
    accuracy_drop = 0.0
    if validation_data is not None:
        accuracy_drop = _compute_accuracy_drop(edge_model.model, tflite_model, validation_data)

    # Calibration stats for int8
    calibration_stats = {}
    if quant_config.quantization_type == "int8":
        calibration_stats = _extract_calibration_stats(interpreter, input_details, output_details)

    return QuantizationResult(
        tflite_model=tflite_model,
        model_size_kb=model_size_kb,
        estimated_ram_kb=estimated_ram_kb,
        ops_used=ops_used,
        input_details=[_detail_to_dict(d) for d in input_details],
        output_details=[_detail_to_dict(d) for d in output_details],
        accuracy_drop_percent=accuracy_drop,
        calibration_stats=calibration_stats,
    )


def _get_ops_used(tflite_model: bytes) -> list[str]:
    """Extract unique ops used in TFLite model."""
    try:
        import flatbuffers
        from tensorflow.lite.schema import Model as TFLiteModel

        buf = bytearray(tflite_model)
        model = TFLiteModel.GetRootAsModel(buf, 0)
        ops = set()
        for i in range(model.SubgraphsLength()):
            subgraph = model.Subgraphs(i)
            for j in range(subgraph.OperatorsLength()):
                op = subgraph.Operators(j)
                opcode_index = op.OpcodeIndex()
                opcode = model.OperatorCodes(opcode_index)
                builtin_code = opcode.BuiltinCode()
                # Convert enum to string
                ops.add(str(builtin_code))
        return sorted(ops)
    except Exception:
        return ["unknown"]


def _detail_to_dict(detail: dict) -> dict:
    """Convert TFLite tensor detail to serializable dict."""
    return {
        "name": detail["name"],
        "index": detail["index"],
        "shape": detail["shape"].tolist(),
        "dtype": str(detail["dtype"]),
        "quantization": detail["quantization_parameters"].__dict__
        if "quantization_parameters" in detail
        else {},
    }


def _extract_calibration_stats(
    interpreter: tf.lite.Interpreter,
    input_details: list[dict],
    output_details: list[dict],
) -> dict:
    """Extract quantization parameters (scale, zero_point) for int8 models."""
    stats = {"inputs": [], "outputs": []}
    for d in input_details:
        qp = d.get("quantization_parameters", {})
        stats["inputs"].append(
            {
                "name": d["name"],
                "scale": qp.get("scales", [1.0])[0] if qp.get("scales") else 1.0,
                "zero_point": qp.get("zero_points", [0])[0] if qp.get("zero_points") else 0,
                "dtype": str(d["dtype"]),
            }
        )
    for d in output_details:
        qp = d.get("quantization_parameters", {})
        stats["outputs"].append(
            {
                "name": d["name"],
                "scale": qp.get("scales", [1.0])[0] if qp.get("scales") else 1.0,
                "zero_point": qp.get("zero_points", [0])[0] if qp.get("zero_points") else 0,
                "dtype": str(d["dtype"]),
            }
        )
    return stats


def _compute_accuracy_drop(
    keras_model: keras.Model,
    tflite_model: bytes,
    validation_data: tuple[np.ndarray, np.ndarray],
) -> float:
    """Compute accuracy drop between FP32 Keras and INT8 TFLite."""
    X_val, y_val = validation_data

    # Keras predictions
    y_pred_keras = keras_model.predict(X_val, verbose=0)
    if y_pred_keras.shape[-1] == 1:
        y_pred_keras = (y_pred_keras > 0.5).astype(int).flatten()
    else:
        y_pred_keras = np.argmax(y_pred_keras, axis=1)

    # TFLite predictions
    interpreter = tf.lite.Interpreter(model_content=tflite_model)
    interpreter.allocate_tensors()
    input_details = interpreter.get_input_details()
    output_details = interpreter.get_output_details()

    y_pred_tflite = []
    for i in range(len(X_val)):
        input_data = X_val[i : i + 1].astype(np.float32)
        # Apply input quantization if needed
        if input_details[0]["dtype"] == np.int8:
            scale, zero_point = input_details[0]["quantization"]
            if scale > 0:
                input_data = (input_data / scale + zero_point).astype(np.int8)
        interpreter.set_tensor(input_details[0]["index"], input_data)
        interpreter.invoke()
        output = interpreter.get_tensor(output_details[0]["index"])
        y_pred_tflite.append(output[0])

    y_pred_tflite = np.array(y_pred_tflite)
    if y_pred_tflite.shape[-1] == 1:
        y_pred_tflite = (y_pred_tflite > 0.5).astype(int).flatten()
    else:
        y_pred_tflite = np.argmax(y_pred_tflite, axis=1)

    # Accuracy drop
    acc_keras = np.mean(y_pred_keras == y_val)
    acc_tflite = np.mean(y_pred_tflite == y_val)
    return float((acc_keras - acc_tflite) * 100)


def save_quantization_artifacts(
    result: QuantizationResult,
    edge_model: EdgeModel,
    output_dir: Path,
    model_name: str | None = None,
) -> dict[str, Path]:
    """Save TFLite model, metadata, and C header for ESP32 deployment."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    name = model_name or edge_model.config.name

    # Save .tflite model
    tflite_path = output_dir / f"{name}.tflite"
    tflite_path.write_bytes(result.tflite_model)

    # Save metadata JSON
    metadata = {
        "model_name": name,
        "model_type": edge_model.config.model_type.value,
        "version": edge_model.config.version,
        "input_shape": list(edge_model.config.input_shape),
        "num_classes": edge_model.config.num_classes,
        "labels": edge_model.config.labels,
        "feature_names": edge_model.config.feature_names,
        "sampling_rate": edge_model.config.sampling_rate,
        "window_duration_s": edge_model.config.window_duration_s,
        "quantization": result.to_dict(),
        "target_platform": edge_model.config.target_platform.value,
    }
    metadata_path = output_dir / f"{name}_metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2))

    # Generate C header for ESP32 (flatbuffer)
    header_path = output_dir / f"{name}.h"
    _generate_c_header(result.tflite_model, name, header_path)

    return {"tflite": tflite_path, "metadata": metadata_path, "header": header_path}


def _generate_c_header(tflite_model: bytes, name: str, output_path: Path) -> None:
    """Generate C header file with model data array for ESP32."""
    hex_bytes = ", ".join(f"0x{b:02x}" for b in tflite_model)
    # Split into lines of 12 bytes
    lines = []
    for i in range(0, len(hex_bytes), 120):  # ~12 bytes per line
        lines.append(hex_bytes[i : i + 120])

    newline = chr(10)
    header_content = (
        f"// Auto-generated TFLM model header for {name}\n"
        f"// Model size: {len(tflite_model)} bytes\n"
        f"// Generated by synapse24.edge_ai.quantization\n\n"
        f"#ifndef {name.upper()}_MODEL_H\n"
        f"#define {name.upper()}_MODEL_H\n\n"
        f"alignas(8) const unsigned char {name}_tflite[] = {{\n"
        f"{newline.join(lines)}\n"
        f"}};\n"
        f"const unsigned int {name}_tflite_len = {len(tflite_model)};\n\n"
        f"#endif // {name.upper()}_MODEL_H\n"
    )
    output_path.write_text(header_content)


def estimate_inference_latency(
    model_size_kb: float,
    target_platform: TargetPlatform,
    n_maccs: float | None = None,
) -> float:
    """Estimate inference latency in milliseconds."""
    # Rough estimates based on platform and model size
    # These are heuristics; real measurement requires on-device benchmarking
    mhz = {
        TargetPlatform.ESP32: 240,
        TargetPlatform.ESP32_S3: 240,
        TargetPlatform.RASPBERRY_PI_PICO: 133,
        TargetPlatform.RASPBERRY_PI: 1500,
        TargetPlatform.GENERIC_CORTEX_M: 200,
        TargetPlatform.HOST: 3000,
    }.get(target_platform, 240)

    # Very rough: ~1-2 MACC/cycle on Cortex-M4/M7 with CMSIS-NN
    # ESP32-S3 has vector instructions for INT8
    if target_platform in (TargetPlatform.ESP32, TargetPlatform.ESP32_S3):
        maccs_per_cycle = 2 if target_platform == TargetPlatform.ESP32_S3 else 1
    else:
        maccs_per_cycle = 1

    if n_maccs is None:
        # Heuristic: ~2-4 MACC per parameter for LSTM/CNN
        n_maccs = model_size_kb * 1024 * 3

    cycles = n_maccs / maccs_per_cycle
    latency_ms = (cycles / (mhz * 1_000_000)) * 1000
    return max(latency_ms, 1.0)  # At least 1ms
