"""Edge AI pipeline for SYNAPSE-24: Edge Impulse → TFLM → ESP32 deployment."""

from .deployment import DeploymentConfig, ESP32Deployer
from .model import EdgeModel, ModelConfig
from .quantization import QuantizationConfig, TFLMQuantizer
from .training import EdgeImpulseTrainer, TrainingConfig

__all__ = [
    "EdgeModel",
    "ModelConfig",
    "EdgeImpulseTrainer",
    "TrainingConfig",
    "TFLMQuantizer",
    "QuantizationConfig",
    "ESP32Deployer",
    "DeploymentConfig",
]
