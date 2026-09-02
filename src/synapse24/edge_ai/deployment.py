"""Edge AI model deployment utilities for SYNAPSE-24."""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from synapse24.edge_ai.model import EdgeModel, ModelConfig, TargetPlatform
from synapse24.edge_ai.quantization import QuantizationResult, save_quantization_artifacts


@dataclass
class DeploymentConfig:
    """Configuration for model deployment."""

    target_platform: TargetPlatform = TargetPlatform.ESP32_S3
    optimization_level: str = "O2"  # O0, O1, O2, O3, Os
    include_ops: list[str] = None  # Specific TFLM ops to include
    arena_size_kb: int = 0  # 0 = auto-estimate
    enable_cmsis_nn: bool = True
    enable_xnnpack: bool = False
    generate_example: bool = True

    def __post_init__(self) -> None:
        if self.include_ops is None:
            self.include_ops = [
                "FULLY_CONNECTED",
                "CONV_2D",
                "DEPTHWISE_CONV_2D",
                "LSTM",
                "UNIDIRECTIONAL_SEQUENCE_LSTM",
                "BIDIRECTIONAL_SEQUENCE_LSTM",
                "ADD",
                "MUL",
                "CONCATENATION",
                "RESHAPE",
                "TRANSPOSE",
                "SOFTMAX",
                "LOGISTIC",
                "TANH",
                "RELU",
                "RELU6",
                "PRELU",
                "MEAN",
                "MAX_POOL_2D",
                "AVERAGE_POOL_2D",
                "QUANTIZE",
                "DEQUANTIZE",
            ]


@dataclass
class DeploymentResult:
    """Result of deployment artifact generation."""

    artifacts_dir: Path
    tflite_path: Path
    header_path: Path
    metadata_path: Path
    cmake_path: Path | None
    model_size_kb: float
    estimated_ram_kb: float
    estimated_flash_kb: float
    estimated_latency_ms: float
    ops_included: list[str]
    validation_passed: bool = True
    validation_notes: list[str] = None

    def __post_init__(self) -> None:
        if self.validation_notes is None:
            self.validation_notes = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifacts_dir": str(self.artifacts_dir),
            "tflite_path": str(self.tflite_path),
            "header_path": str(self.header_path),
            "metadata_path": str(self.metadata_path),
            "cmake_path": str(self.cmake_path) if self.cmake_path else None,
            "model_size_kb": self.model_size_kb,
            "estimated_ram_kb": self.estimated_ram_kb,
            "estimated_flash_kb": self.estimated_flash_kb,
            "estimated_latency_ms": self.estimated_latency_ms,
            "ops_included": self.ops_included,
            "validation_passed": self.validation_passed,
            "validation_notes": self.validation_notes,
        }


def deploy_model(
    edge_model: EdgeModel,
    quantization_result: QuantizationResult,
    deploy_config: DeploymentConfig,
    output_dir: Path,
) -> DeploymentResult:
    """Generate complete deployment artifacts for target platform.

    Args:
        edge_model: Trained EdgeModel
        quantization_result: Result from quantize_model()
        deploy_config: Deployment configuration
        output_dir: Output directory for artifacts

    Returns:
        DeploymentResult with paths and metrics
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    name = edge_model.config.name

    # Save quantization artifacts (tflite, metadata, header)
    artifacts = save_quantization_artifacts(quantization_result, edge_model, output_dir, name)

    # Estimate flash usage (model + overhead)
    estimated_flash_kb = quantization_result.model_size_kb * 1.2  # 20% overhead

    # Estimate latency
    from synapse24.edge_ai.quantization import estimate_inference_latency

    estimated_latency_ms = estimate_inference_latency(
        quantization_result.model_size_kb, deploy_config.target_platform
    )

    # Generate CMakeLists.txt for ESP-IDF integration
    cmake_path = None
    if deploy_config.target_platform in (
        TargetPlatform.ESP32,
        TargetPlatform.ESP32_S3,
    ):
        cmake_path = _generate_cmake(edge_model, quantization_result, deploy_config, output_dir)

    # Generate example inference code
    example_path = None
    if deploy_config.generate_example:
        example_path = _generate_inference_example(
            edge_model, quantization_result, deploy_config, output_dir
        )

    # Validate against constraints
    validation_passed, validation_notes = _validate_deployment(quantization_result, deploy_config)

    return DeploymentResult(
        artifacts_dir=output_dir,
        tflite_path=artifacts["tflite"],
        header_path=artifacts["header"],
        metadata_path=artifacts["metadata"],
        cmake_path=cmake_path,
        model_size_kb=quantization_result.model_size_kb,
        estimated_ram_kb=quantization_result.estimated_ram_kb,
        estimated_flash_kb=estimated_flash_kb,
        estimated_latency_ms=estimated_latency_ms,
        ops_included=quantization_result.ops_used,
        validation_passed=validation_passed,
        validation_notes=validation_notes,
    )


def _generate_cmake(
    edge_model: EdgeModel,
    quantization_result: QuantizationResult,
    deploy_config: DeploymentConfig,
    output_dir: Path,
) -> Path:
    """Generate CMakeLists.txt for ESP-IDF component."""
    name = edge_model.config.name
    cmake_path = output_dir / "CMakeLists.txt"

    # Determine required TFLM libraries
    tflm_libs = [
        "tflite-micro",
    ]
    if deploy_config.enable_cmsis_nn:
        tflm_libs.append("tflite-micro-cmsis-nn")

    cmake_content = f"""# ESP-IDF component for {name} TFLM model
# Auto-generated by synapse24.edge_ai.deployment

idf_component_register(
    SRCS
        "inference.cpp"
    INCLUDE_DIRS
        "."
    REQUIRES
        {" ".join(tflm_libs)}
    PRIV_REQUIRES
        esp_timer
        log
)

# Model data
set({name.upper()}_MODEL_DATA ${{CMAKE_CURRENT_SOURCE_DIR}}/{name}.tflite)
set({name.upper()}_MODEL_SIZE {len(quantization_result.tflite_model)})

# Embed model binary into firmware
target_sources(${{COMPONENT_LIB}} PRIVATE
    ${{{name.upper()}_MODEL_DATA}}
)
"""
    cmake_path.write_text(cmake_content)
    return cmake_path


def _generate_inference_example(
    edge_model: EdgeModel,
    quantization_result: QuantizationResult,
    deploy_config: DeploymentConfig,
    output_dir: Path,
) -> Path:
    """Generate example inference code for target platform."""
    name = edge_model.config.name
    input_details = quantization_result.input_details[0]
    output_details = quantization_result.output_details[0]

    # Determine input/output types
    input_dtype = input_details["dtype"]
    output_dtype = output_details["dtype"]

    # Check if quantized
    is_quantized = "int8" in str(input_dtype).lower()

    example_path = output_dir / "inference.cpp"

    # Get normalization params for int8
    input_scale = 1.0
    input_zero_point = 0
    if is_quantized and quantization_result.calibration_stats:
        input_scale = quantization_result.calibration_stats["inputs"][0].get("scale", 1.0)
        input_zero_point = quantization_result.calibration_stats["inputs"][0].get("zero_point", 0)

    input_shape = input_details["shape"]
    n_features = input_shape[-1] if len(input_shape) > 1 else input_shape[0]
    n_timesteps = input_shape[1] if len(input_shape) > 2 else 1

    labels = edge_model.config.labels
    labels_str = ", ".join(f'"{l}"' for l in labels)

    example_content = f"""// Auto-generated inference example for {name}
// Target: {deploy_config.target_platform.value}
// Model: {quantization_result.model_size_kb:.1f} KB, ~{quantization_result.estimated_ram_kb:.1f} KB RAM

#include <cstring>
#include <cmath>
#include "esp_log.h"
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/micro/system_setup.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "{name}.h"

static const char* TAG = "{name}_inference";

// Model input/output dimensions
constexpr int kInputSize = {n_features * n_timesteps};
constexpr int kOutputSize = {edge_model.config.num_classes};
constexpr int kNumClasses = {edge_model.config.num_classes};

// Arena size (adjust based on model)
constexpr int kTensorArenaSize = {max(deploy_config.arena_size_kb * 1024, int(quantization_result.estimated_ram_kb * 1024))};

// Tensor arena
alignas(16) static uint8_t tensor_arena[kTensorArenaSize];

// Model and interpreter
const tflite::Model* model = nullptr;
tflite::MicroInterpreter* interpreter = nullptr;
TfLiteTensor* input_tensor = nullptr;
TfLiteTensor* output_tensor = nullptr;

// Op resolver with required ops
static tflite::MicroMutableOpResolver<20> resolver;

void setup_{name}() {{
    // Initialize tensor arena
    tflite::InitializeTarget();

    // Register ops
    {_generate_op_registrations(deploy_config.include_ops)}

    // Load model
    model = tflite::GetModel({name}_tflite);
    if (model->version() != TFLITE_SCHEMA_VERSION) {{
        ESP_LOGE(TAG, "Model schema version mismatch: %d vs %d", model->version(), TFLITE_SCHEMA_VERSION);
        return;
    }}

    // Create interpreter
    static tflite::MicroInterpreter static_interpreter(
        model, resolver, tensor_arena, kTensorArenaSize, nullptr
    );
    interpreter = &static_interpreter;

    // Allocate tensors
    TfLiteStatus allocate_status = interpreter->AllocateTensors();
    if (allocate_status != kTfLiteOk) {{
        ESP_LOGE(TAG, "AllocateTensors failed");
        return;
    }}

    // Get input/output tensors
    input_tensor = interpreter->input(0);
    output_tensor = interpreter->output(0);

    ESP_LOGI(TAG, "{name} initialized successfully");
    ESP_LOGI(TAG, "Input: {{dtype=%d, shape=[%s]}}", input_tensor->type, "{_shape_str(input_shape)}");
    ESP_LOGI(TAG, "Output: {{dtype=%d, shape=[%s]}}", output_tensor->type, "{_shape_str(output_details["shape"])}");
    ESP_LOGI(TAG, "Arena used: %d / %d bytes", interpreter->arena_used_bytes(), kTensorArenaSize);
}}

int run_{name}_inference(const float* input_features, float* output_probs) {{
    if (!interpreter || !input_tensor || !output_tensor) {{
        ESP_LOGE(TAG, "Interpreter not initialized");
        return -1;
    }}

    // Copy and quantize input
    {_generate_input_copy_code(input_dtype, n_features, n_timesteps, input_scale, input_zero_point)}

    // Run inference
    TfLiteStatus invoke_status = interpreter->Invoke();
    if (invoke_status != kTfLiteOk) {{
        ESP_LOGE(TAG, "Invoke failed");
        return -1;
    }}

    // Dequantize and copy output
    {_generate_output_copy_code(output_dtype, edge_model.config.num_classes)}

    return 0;
}}

// Example usage
extern "C" void app_main() {{
    setup_{name}();

    // Example input (replace with real sensor data)
    float input[kInputSize] = {{0}};
    float output[kOutputSize] = {{0}};

    while (true) {{
        // Replace with actual sensor reading
        // read_sensors(input);

        if (run_{name}_inference(input, output) == 0) {{
            // Find max probability
            int max_idx = 0;
            float max_prob = output[0];
            for (int i = 1; i < kNumClasses; i++) {{
                if (output[i] > max_prob) {{
                    max_prob = output[i];
                    max_idx = i;
                }}
            }}

            static const char* labels[] = {{{labels_str}}};
            ESP_LOGI(TAG, "Prediction: %s (%.2f%%)", labels[max_idx], max_prob * 100);
        }}

        vTaskDelay(pdMS_TO_TICKS(1000));  // 1 Hz inference
    }}
}}
"""
    example_path.write_text(example_content)
    return example_path


def _generate_op_registrations(ops: list[str]) -> str:
    """Generate op resolver registration code."""
    registrations = []
    for op in ops:
        op_lower = op.lower()
        if op_lower == "fully_connected":
            registrations.append("    resolver.AddFullyConnected();")
        elif op_lower == "conv_2d":
            registrations.append("    resolver.AddConv2D();")
        elif op_lower == "depthwise_conv_2d":
            registrations.append("    resolver.AddDepthwiseConv2D();")
        elif op_lower == "lstm":
            registrations.append("    resolver.AddLSTM();")
        elif op_lower == "unidirectional_sequence_lstm":
            registrations.append("    resolver.AddUnidirectionalSequenceLSTM();")
        elif op_lower == "bidirectional_sequence_lstm":
            registrations.append("    resolver.AddBidirectionalSequenceLSTM();")
        elif op_lower == "add":
            registrations.append("    resolver.AddAdd();")
        elif op_lower == "mul":
            registrations.append("    resolver.AddMul();")
        elif op_lower == "concatenation":
            registrations.append("    resolver.AddConcatenation();")
        elif op_lower == "reshape":
            registrations.append("    resolver.AddReshape();")
        elif op_lower == "transpose":
            registrations.append("    resolver.AddTranspose();")
        elif op_lower == "softmax":
            registrations.append("    resolver.AddSoftmax();")
        elif op_lower == "logistic":
            registrations.append("    resolver.AddLogistic();")
        elif op_lower == "tanh":
            registrations.append("    resolver.AddTanh();")
        elif op_lower == "relu":
            registrations.append("    resolver.AddRelu();")
        elif op_lower == "relu6":
            registrations.append("    resolver.AddRelu6();")
        elif op_lower == "prelu":
            registrations.append("    resolver.AddPRelu();")
        elif op_lower == "mean":
            registrations.append("    resolver.AddMean();")
        elif op_lower == "max_pool_2d":
            registrations.append("    resolver.AddMaxPool2D();")
        elif op_lower == "average_pool_2d":
            registrations.append("    resolver.AddAveragePool2D();")
        elif op_lower == "quantize":
            registrations.append("    resolver.AddQuantize();")
        elif op_lower == "dequantize":
            registrations.append("    resolver.AddDequantize();")

    return "\n".join(registrations)


def _generate_input_copy_code(
    input_dtype: str,
    n_features: int,
    n_timesteps: int,
    input_scale: float,
    input_zero_point: int,
) -> str:
    """Generate input copy/quantization code."""
    is_quantized = "int8" in str(input_dtype).lower()

    if is_quantized:
        return f"""    // Quantize float input to int8
    int8_t* input_data = input_tensor->data.int8;
    for (int i = 0; i < kInputSize; i++) {{
        float val = input_features[i];
        int32_t quantized = static_cast<int32_t>(roundf(val / {input_scale}f + {input_zero_point}));
        input_data[i] = static_cast<int8_t>(std::max(-128, std::min(127, quantized)));
    }}"""
    return """    // Copy float input
    float* input_data = input_tensor->data.f;
    memcpy(input_data, input_features, kInputSize * sizeof(float));"""


def _generate_output_copy_code(output_dtype: str, num_classes: int) -> str:
    """Generate output copy/dequantization code."""
    is_quantized = "int8" in str(output_dtype).lower()

    if is_quantized:
        return """    // Dequantize int8 output to float probabilities
    const int8_t* output_data = output_tensor->data.int8;
    for (int i = 0; i < kNumClasses; i++) {
        output_probs[i] = static_cast<float>(output_data[i]) / 128.0f;  // Approximate
    }"""
    return """    // Copy float output
    float* output_data = output_tensor->data.f;
    memcpy(output_probs, output_data, kNumClasses * sizeof(float));"""


def _shape_str(shape: list) -> str:
    """Format shape list as string."""
    return ", ".join(str(s) for s in shape)


def _validate_deployment(
    result: QuantizationResult,
    config: DeploymentConfig,
) -> tuple[bool, list[str]]:
    """Validate deployment against platform constraints."""
    notes = []
    passed = True

    # Platform-specific limits
    limits = {
        TargetPlatform.ESP32: {"ram_kb": 200, "flash_kb": 1024, "model_kb": 100},
        TargetPlatform.ESP32_S3: {"ram_kb": 400, "flash_kb": 4096, "model_kb": 250},
        TargetPlatform.RASPBERRY_PI_PICO: {"ram_kb": 180, "flash_kb": 2048, "model_kb": 150},
        TargetPlatform.GENERIC_CORTEX_M: {"ram_kb": 200, "flash_kb": 1024, "model_kb": 150},
    }

    limit = limits.get(config.target_platform, limits[TargetPlatform.ESP32])

    if result.model_size_kb > limit["model_kb"]:
        passed = False
        notes.append(
            f"Model size {result.model_size_kb:.1f} KB exceeds {limit['model_kb']} KB limit for {config.target_platform.value}"
        )

    if result.estimated_ram_kb > limit["ram_kb"]:
        passed = False
        notes.append(
            f"Estimated RAM {result.estimated_ram_kb:.1f} KB exceeds {limit['ram_kb']} KB limit for {config.target_platform.value}"
        )

    # Check for unsupported ops
    unsupported = []
    for op in result.ops_used:
        if op not in config.include_ops and "UNKNOWN" not in op:
            unsupported.append(op)
    if unsupported:
        notes.append(f"Potentially unsupported ops: {unsupported}")

    # Latency warning
    if result.model_size_kb > 100:
        notes.append("Model >100KB may exceed real-time constraints on MCU; consider pruning")

    return passed, notes


def generate_deployment_report(
    deployment_result: DeploymentResult,
    output_path: Path,
) -> Path:
    """Generate human-readable deployment report."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    report = f"""# SYNAPSE-24 Edge AI Deployment Report

## Model: {deployment_result.artifacts_dir.name}

### Artifacts Generated
- **TFLite Model**: `{deployment_result.tflite_path.name}` ({deployment_result.model_size_kb:.1f} KB)
- **C Header**: `{deployment_result.header_path.name}` (for ESP32 embedding)
- **Metadata**: `{deployment_result.metadata_path.name}` (JSON with quantization params)
{"- **CMakeLists.txt**: `" + deployment_result.cmake_path.name + "` (ESP-IDF component)" if deployment_result.cmake_path else ""}

### Resource Estimates
| Metric | Value | Limit | Status |
|--------|-------|-------|--------|
| Model Size | {deployment_result.model_size_kb:.1f} KB | Platform dependent | {"✅" if deployment_result.validation_passed else "❌"} |
| Estimated RAM | {deployment_result.estimated_ram_kb:.1f} KB | Platform dependent | {"✅" if deployment_result.validation_passed else "❌"} |
| Estimated Flash | {deployment_result.estimated_flash_kb:.1f} KB | Platform dependent | {"✅" if deployment_result.validation_passed else "❌"} |
| Estimated Latency | {deployment_result.estimated_latency_ms:.1f} ms | < 50 ms target | {"✅" if deployment_result.estimated_latency_ms < 50 else "⚠️"} |

### TFLite Operations Used
{chr(10).join(f"- {op}" for op in deployment_result.ops_included)}

### Validation Notes
{chr(10).join(f"- {note}" for note in deployment_result.validation_notes) if deployment_result.validation_notes else "All checks passed"}

### Deployment Status
**{"✅ READY FOR DEPLOYMENT" if deployment_result.validation_passed else "❌ VALIDATION FAILED"}**
"""
    output_path.write_text(report)
    return output_path
