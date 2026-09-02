"""Hardware abstraction layer for SYNAPSE-24 biosignal acquisition."""

from .base import BoardConfig, BoardManager, BoardState
from .cerelog import CerelogConfig
from .emotibit import EmotiBitConfig
from .registry import DeviceRegistry, SensorPodConfig
from .synthetic import SyntheticBoardConfig

__all__ = [
    "BoardConfig",
    "BoardManager",
    "BoardState",
    "DeviceRegistry",
    "SensorPodConfig",
    "SyntheticBoardConfig",
    "EmotiBitConfig",
    "CerelogConfig",
]
