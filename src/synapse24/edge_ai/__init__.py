"""Edge AI pipeline for SYNAPSE-24: Edge Impulse → TFLM → ESP32 deployment."""

from .deployment import DeploymentConfig, DeploymentResult, deploy_model
from .model import EdgeModel, ModelConfig, ModelType, TargetPlatform
from .quantization import (
    QuantizationConfig,
    QuantizationResult,
    RepresentativeDatasetGenerator,
    estimate_inference_latency,
    quantize_model,
    save_quantization_artifacts,
)
from .training import EdgeImpulseTrainer, TrainingConfig
from .wesad_int8_closure import (
    ClosureMatrix,
    build_closure_matrix,
    estimate_triage_footprint,
    groupkfold_scores,
    run_wesad_int8_closure,
    simulate_int8_roundtrip,
)

__all__ = [
    "EdgeModel",
    "ModelConfig",
    "ModelType",
    "TargetPlatform",
    "EdgeImpulseTrainer",
    "TrainingConfig",
    "quantize_model",
    "QuantizationConfig",
    "QuantizationResult",
    "RepresentativeDatasetGenerator",
    "save_quantization_artifacts",
    "estimate_inference_latency",
    "deploy_model",
    "DeploymentConfig",
    "DeploymentResult",
    "ClosureMatrix",
    "build_closure_matrix",
    "estimate_triage_footprint",
    "groupkfold_scores",
    "run_wesad_int8_closure",
    "simulate_int8_roundtrip",
]
