"""Edge AI pipeline for SYNAPSE-24: Edge Impulse → TFLM → ESP32 deployment."""

from .deployment import DeploymentConfig, DeploymentResult, deploy_model
from .model import EdgeModel, ModelConfig, ModelType, TargetPlatform
from .quantization import (
    QuantizationConfig,
    QuantizationResult,
    RepresentativeDatasetGenerator,
    quantize_model,
)
from .training import EdgeImpulseTrainer, TrainingConfig

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
    "deploy_model",
    "DeploymentConfig",
    "DeploymentResult",
]
