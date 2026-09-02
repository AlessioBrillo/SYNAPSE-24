"""ESP32 deployment utilities for SYNAPSE-24 TFLM models."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from synapse24.edge_ai.model import EdgeModel, ModelConfig, TargetPlatform


@dataclass
class DeploymentConfig:
    """Configuration for ESP32 deployment."""

    # Target
    target_platform: TargetPlatform = TargetPlatform.ESP32
    target_board: str = "esp32dev"  # PlatformIO board name
    framework: str = "arduino"  # arduino, esp-idf

    # TFLM
    tflm_version: str = "2.14.0"
    tflm_lib_path: str = ""  # Path to TFLM source if not using library manager

    # Memory
    tensor_arena_size_kb: int = 60  # KB for TFLM arena
    max_model_size_kb: int = 500  # Max model size

    # Build
    platformio_ini: str = ""  # Custom platformio.ini content
    build_flags: list[str] = field(default_factory=list)
    monitor_speed: int = 115200

    # Deployment
    upload_port: str = ""  # Auto-detect if empty
    upload_protocol: str = "esptool"

    # Model metadata
    model_name: str = "model"
    model_version: str = "1.0.0"
    input_tensor_name: str = "input"
    output_tensor_name: str = "output"


class ESP32Deployer:
    """Deploys TFLM models to ESP32 via PlatformIO.

    Generates:
    - model.cpp/.h with model data array
    - inference.cpp/.h with TFLM inference code
    - platformio.ini for build configuration
    - Example Arduino sketch
    """

    def __init__(self, config: DeploymentConfig | None = None) -> None:
        self.config = config or DeploymentConfig()

    def deploy(self, model: EdgeModel, output_dir: Path) -> dict[str, Any]:
        """Deploy model to ESP32 project structure.

        Returns dict with generated files and build info.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        if model.tflite_model is None:
            raise ValueError("Model not quantized - no TFLite model available")

        # Validate model size
        model_size_kb = len(model.tflite_model) / 1024
        if model_size_kb > self.config.max_model_size_kb:
            raise ValueError(
                f"Model too large: {model_size_kb:.1f} KB > {self.config.max_model_size_kb} KB"
            )

        # Generate files
        files = {}

        # 1. Model data array (model.cpp/.h)
        model_h, model_cpp = self._generate_model_files(model)
        files["model.h"] = model_h
        files["model.cpp"] = model_cpp

        # 2. Inference engine (inference.h/.cpp)
        inference_h, inference_cpp = self._generate_inference_files(model)
        files["inference.h"] = inference_h
        files["inference.cpp"] = inference_cpp

        # 3. PlatformIO configuration
        platformio_ini = self._generate_platformio_ini()
        files["platformio.ini"] = platformio_ini

        # 4. Example sketch
        example_ino = self._generate_example_sketch(model)
        files["example.ino"] = example_ino

        # 5. Model metadata JSON
        metadata = self._generate_metadata(model)
        files["model_metadata.json"] = metadata

        # Write all files
        for filename, content in files.items():
            (output_dir / filename).write_text(content)

        # Build and optionally upload
        build_result = self._build_project(output_dir)

        return {
            "output_dir": str(output_dir),
            "files": list(files.keys()),
            "model_size_kb": model_size_kb,
            "tensor_arena_kb": self.config.tensor_arena_size_kb,
            "build": build_result,
        }

    def _generate_model_files(self, model: EdgeModel) -> tuple[str, str]:
        """Generate model.h and model.cpp with TFLite model data array."""
        model_data = model.tflite_model
        model_name = self.config.model_name

        # Convert to C array
        hex_data = ", ".join(f"0x{b:02x}" for b in model_data)
        # Format in lines of 12 bytes
        lines = []
        for i in range(0, len(hex_data), 12 * 6):  # ~12 bytes per line
            lines.append("  " + hex_data[i : i + 12 * 6])
        formatted_data = ",\n".join(lines)

        model_h = f"""// Auto-generated from SYNAPSE-24 Edge AI pipeline
// Model: {model.config.name} v{model.config.version}
// Size: {len(model_data)} bytes ({len(model_data) / 1024:.1f} KB)
// Quantization: {model.config.quantization}
// Target: {model.config.target_platform.value}

#ifndef {model_name.upper()}_H
#define {model_name.upper()}_H

#include <cstdint>

// Model metadata
constexpr const char* {model_name}_name = "{model.config.name}";
constexpr const char* {model_name}_version = "{model.config.version}";
constexpr const char* {model_name}_quantization = "{model.config.quantization}";
constexpr int {model_name}_input_shape[] = {{{", ".join(map(str, model.config.input_shape))}}};
constexpr int {model_name}_num_classes = {model.config.num_classes};
constexpr int {model_name}_sampling_rate = {model.config.sampling_rate};
constexpr float {model_name}_window_duration = {model.config.window_duration_s}f;

// Model data
extern const unsigned char {model_name}_tflite[];
extern const int {model_name}_tflite_len;

#endif // {model_name.upper()}_H
"""

        model_cpp = f"""// Auto-generated from SYNAPSE-24 Edge AI pipeline
#include "{model_name}.h"

// TFLite model data
const unsigned char {model_name}_tflite[] = {{
{formatted_data}
}};

const int {model_name}_tflite_len = {len(model_data)};
"""

        return model_h, model_cpp

    def _generate_inference_files(self, model: EdgeModel) -> tuple[str, str]:
        """Generate inference.h and inference.cpp with TFLM inference code."""
        model_name = self.config.model_name
        input_shape = model.config.input_shape
        num_classes = model.config.num_classes

        inference_h = f"""// Auto-generated from SYNAPSE-24 Edge AI pipeline
// TFLM Inference Engine for {model.config.name}

#ifndef INFERENCE_H
#define INFERENCE_H

#include <cstdint>
#include <vector>
#include "tensorflow/lite/micro/micro_interpreter.h"
#include "tensorflow/lite/micro/micro_mutable_op_resolver.h"
#include "tensorflow/lite/schema/schema_generated.h"
#include "tensorflow/lite/version.h"
#include "{model_name}.h"

class SynapseInference {{
public:
    SynapseInference();
    ~SynapseInference();

    bool init();
    bool invoke(const float* input, float* output, int input_size);
    bool invoke_int8(const int8_t* input, float* output, int input_size);

    // Get output as probabilities
    void get_probabilities(float* probs);

    // Model info
    int get_input_size() const;
    int get_output_size() const;
    int get_num_classes() const;

private:
    // TFLM interpreter
    tflite::MicroInterpreter* interpreter_;
    tflite::MicroMutableOpResolver<10>* resolver_;
    const tflite::Model* model_;

    // Tensor arena
    static constexpr int kTensorArenaSize = {self.config.tensor_arena_size_kb} * 1024;
    alignas(16) uint8_t tensor_arena_[kTensorArenaSize];

    // Input/output tensors
    TfLiteTensor* input_tensor_;
    TfLiteTensor* output_tensor_;

    // Quantization params
    float input_scale_;
    int input_zero_point_;
    float output_scale_;
    int output_zero_point_;
}};

#endif // INFERENCE_H
"""

        inference_cpp = f"""// Auto-generated from SYNAPSE-24 Edge AI pipeline
#include "inference.h"
#include "tensorflow/lite/micro/kernels/micro_ops.h"
#include "tensorflow/lite/micro/micro_allocator.h"

using namespace tflite;

SynapseInference::SynapseInference()
    : interpreter_(nullptr), resolver_(nullptr), model_(nullptr),
      input_tensor_(nullptr), output_tensor_(nullptr),
      input_scale_(1.0f), input_zero_point_(0),
      output_scale_(1.0f), output_zero_point_(0) {{}}

SynapseInference::~SynapseInference() {{
    delete interpreter_;
    delete resolver_;
}}

bool SynapseInference::init() {{
    // Load model
    model_ = GetModel({model_name}_tflite);
    if (model_->version() != TFLITE_SCHEMA_VERSION) {{
        return false;
    }}

    // Create op resolver (adjust ops as needed)
    resolver_ = new MicroMutableOpResolver<10>();
    resolver_->AddFullyConnected();
    resolver_->AddConv2D();
    resolver_->AddDepthwiseConv2D();
    resolver_->AddMaxPool2D();
    resolver_->AddAveragePool2D();
    resolver_->AddLSTM();
    resolver_->AddSoftmax();
    resolver_->AddReshape();
    resolver_->AddQuantize();
    resolver_->AddDequantize();

    // Create interpreter
    interpreter_ = new MicroInterpreter(
        model_, *resolver_, tensor_arena_, kTensorArenaSize
    );

    // Allocate tensors
    if (interpreter_->AllocateTensors() != kTfLiteOk) {{
        return false;
    }}

    // Get input/output tensors
    input_tensor_ = interpreter_->input(0);
    output_tensor_ = interpreter_->output(0);

    // Store quantization parameters
    input_scale_ = input_tensor_->params.scale;
    input_zero_point_ = input_tensor_->params.zero_point;
    output_scale_ = output_tensor_->params.scale;
    output_zero_point_ = output_tensor_->params.zero_point;

    return true;
}}

bool SynapseInference::invoke(const float* input, float* output, int input_size) {{
    // Quantize input if needed
    if (input_tensor_->type == kTfLiteInt8) {{
        int8_t* input_data = input_tensor_->data.int8;
        for (int i = 0; i < input_size && i < input_tensor_->bytes; ++i) {{
            input_data[i] = static_cast<int8_t>(input[i] / input_scale_ + input_zero_point_);
        }}
    }} else {{
        float* input_data = input_tensor_->data.f;
        for (int i = 0; i < input_size && i < input_tensor_->bytes / sizeof(float); ++i) {{
            input_data[i] = input[i];
        }}
    }}

    // Run inference
    if (interpreter_->Invoke() != kTfLiteOk) {{
        return false;
    }}

    // Dequantize output
    if (output_tensor_->type == kTfLiteInt8) {{
        int8_t* output_data = output_tensor_->data.int8;
        int output_size = output_tensor_->bytes;
        for (int i = 0; i < output_size; ++i) {{
            output[i] = (output_data[i] - output_zero_point_) * output_scale_;
        }}
    }} else {{
        float* output_data = output_tensor_->data.f;
        int output_size = output_tensor_->bytes / sizeof(float);
        for (int i = 0; i < output_size; ++i) {{
            output[i] = output_data[i];
        }}
    }}

    return true;
}}

bool SynapseInference::invoke_int8(const int8_t* input, float* output, int input_size) {{
    if (input_tensor_->type != kTfLiteInt8) {{
        return false;
    }}

    int8_t* input_data = input_tensor_->data.int8;
    for (int i = 0; i < input_size && i < input_tensor_->bytes; ++i) {{
        input_data[i] = input[i];
    }}

    if (interpreter_->Invoke() != kTfLiteOk) {{
        return false;
    }}

    int8_t* output_data = output_tensor_->data.int8;
    int output_size = output_tensor_->bytes;
    for (int i = 0; i < output_size; ++i) {{
        output[i] = (output_data[i] - output_zero_point_) * output_scale_;
    }}

    return true;
}}

void SynapseInference::get_probabilities(float* probs) {{
    if (output_tensor_->type == kTfLiteInt8) {{
        int8_t* output_data = output_tensor_->data.int8;
        int output_size = output_tensor_->bytes;
        for (int i = 0; i < output_size; ++i) {{
            probs[i] = (output_data[i] - output_zero_point_) * output_scale_;
        }}
    }} else {{
        float* output_data = output_tensor_->data.f;
        int output_size = output_tensor_->bytes / sizeof(float);
        for (int i = 0; i < output_size; ++i) {{
            probs[i] = output_data[i];
        }}
    }}
}}

int SynapseInference::get_input_size() const {{
    return input_tensor_ ? input_tensor_->bytes : 0;
}}

int SynapseInference::get_output_size() const {{
    return output_tensor_ ? output_tensor_->bytes : 0;
}}

int SynapseInference::get_num_classes() const {{
    return {num_classes};
}}
"""

        return inference_h, inference_cpp

    def _generate_platformio_ini(self) -> str:
        """Generate platformio.ini for ESP32 build."""
        base = f"""# Auto-generated from SYNAPSE-24 Edge AI pipeline
[env:{self.config.target_board}]
platform = espressif32
board = {self.config.target_board}
framework = {self.config.framework}
monitor_speed = {self.config.monitor_speed}
upload_protocol = {self.config.upload_protocol}

# TFLM library
lib_deps =
    tensorflow/tflite-micro-esp32 @ ^{self.config.tflm_version}

# Build flags
build_flags =
    -DTF_LITE_STATIC_MEMORY
    -DTFLM_TENSOR_ARENA_SIZE={self.config.tensor_arena_size_kb * 1024}
"""

        if self.config.build_flags:
            base += "\n    " + "\n    ".join(self.config.build_flags)

        if self.config.platformio_ini:
            base += "\n" + self.config.platformio_ini

        return base

    def _generate_example_sketch(self, model: EdgeModel) -> str:
        """Generate example Arduino sketch."""
        model_name = self.config.model_name
        num_classes = model.config.num_classes
        labels = model.config.labels

        label_array = "{" + ", ".join(f'"{l}"' for l in labels) + "}"

        return f"""// Auto-generated from SYNAPSE-24 Edge AI pipeline
// Example sketch for {model.config.name} on ESP32

#include "inference.h"
#include "{model_name}.h"

SynapseInference inference;

void setup() {{
    Serial.begin(115200);
    delay(1000);

    Serial.println("SYNAPSE-24 Edge AI Inference");
    Serial.print("Model: ");
    Serial.println({model_name}_name);
    Serial.print("Version: ");
    Serial.println({model_name}_version);
    Serial.print("Quantization: ");
    Serial.println({model_name}_quantization);
    Serial.print("Input size: ");
    Serial.println(inference.get_input_size());
    Serial.print("Output classes: ");
    Serial.println(inference.get_num_classes());

    if (!inference.init()) {{
        Serial.println("ERROR: Failed to initialize inference!");
        while (1) delay(1000);
    }}

    Serial.println("Inference engine ready!");
}}

void loop() {{
    // Example: Generate dummy input (replace with real sensor data)
    const int input_size = inference.get_input_size() / sizeof(float);
    float input[input_size];
    float output[{num_classes}];

    // Fill with dummy data - REPLACE WITH REAL SENSOR DATA
    for (int i = 0; i < input_size; i++) {{
        input[i] = 0.0f;  // Your sensor data here
    }}

    // Run inference
    if (inference.invoke(input, output, input_size)) {{
        Serial.print("Inference: ");
        for (int i = 0; i < {num_classes}; i++) {{
            Serial.print(output[i], 4);
            Serial.print(" ");
        }}
        Serial.println();

        // Print class probabilities
        const char* labels[] = {label_array};
        for (int i = 0; i < {num_classes}; i++) {{
            Serial.print(labels[i]);
            Serial.print(": ");
            Serial.print(output[i] * 100, 1);
            Serial.print("%% ");
        }}
        Serial.println();

        // Get predicted class
        int predicted = 0;
        float max_prob = output[0];
        for (int i = 1; i < {num_classes}; i++) {{
            if (output[i] > max_prob) {{
                max_prob = output[i];
                predicted = i;
            }}
        }}
        Serial.print("Predicted: ");
        Serial.print(labels[predicted]);
        Serial.print(" (");
        Serial.print(max_prob * 100, 1);
        Serial.println("%%)");
    }} else {{
        Serial.println("Inference failed!");
    }}

    delay(1000);  // Run inference every second
}}
"""

    def _generate_metadata(self, model: EdgeModel) -> str:
        """Generate model metadata JSON."""
        metadata = {
            "name": model.config.name,
            "version": model.config.version,
            "model_type": model.config.model_type.value,
            "architecture": model.config.architecture,
            "quantization": model.config.quantization,
            "input_shape": model.config.input_shape,
            "num_classes": model.config.num_classes,
            "sampling_rate": model.config.sampling_rate,
            "window_duration_s": model.config.window_duration_s,
            "labels": model.config.labels,
            "feature_names": model.config.feature_names,
            "target_platform": model.config.target_platform.value,
            "tflite_size_bytes": len(model.tflite_model) if model.tflite_model else 0,
            "tflite_size_kb": len(model.tflite_model) / 1024 if model.tflite_model else 0,
            "sha256": hashlib.sha256(model.tflite_model).hexdigest() if model.tflite_model else "",
            "metrics": model.metrics,
        }
        return json.dumps(metadata, indent=2)

    def _build_project(self, project_dir: Path) -> dict[str, Any]:
        """Build PlatformIO project."""
        try:
            # Check if PlatformIO is available
            result = subprocess.run(
                ["pio", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {"success": False, "error": "PlatformIO not installed"}

            # Build
            result = subprocess.run(
                ["pio", "run", "-d", str(project_dir)],
                capture_output=True,
                text=True,
                timeout=300,
            )

            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }

        except subprocess.TimeoutExpired:
            return {"success": False, "error": "Build timeout"}
        except FileNotFoundError:
            return {"success": False, "error": "PlatformIO not found in PATH"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def upload(self, project_dir: Path, port: str = "") -> dict[str, Any]:
        """Upload firmware to ESP32."""
        upload_port = port or self.config.upload_port

        try:
            cmd = ["pio", "run", "-t", "upload", "-d", str(project_dir)]
            if upload_port:
                cmd.extend(["--upload-port", upload_port])

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            return {
                "success": result.returncode == 0,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }

        except Exception as e:
            return {"success": False, "error": str(e)}


def create_cmake_tflm_project(
    model: EdgeModel,
    output_dir: Path,
    config: DeploymentConfig,
) -> dict[str, Any]:
    """Create a CMake-based TFLM project (for ESP-IDF or bare metal).

    Alternative to PlatformIO for more control.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # This would generate CMakeLists.txt, main.c, etc.
    # Placeholder for future implementation
    return {"success": False, "error": "CMake deployment not yet implemented"}
