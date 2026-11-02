"""Hardware abstraction layer for SYNAPSE-24 biosignal acquisition."""

from .base import (
    BOARD_ADAPTERS,
    BoardAdapter,
    BoardConfig,
    BoardManager,
    BoardSession,
    BoardState,
    DeviceRegistry,
    SensorPodConfig,
)
from .cerelog import (
    CerelogAdapter,
    MuseSAdapter,
    OpenBCICytonAdapter,
    OpenBCIGanglionAdapter,
    PiEEGAdapter,
)
from .emotibit import EmotiBitAdapter
from .esp32_tier0 import ESP32Tier0Config, ESP32Tier0Firmware
from .synthetic import SyntheticBoardAdapter, SyntheticPlaybackAdapter

__all__ = [
    # Base
    "BoardConfig",
    "BoardManager",
    "BoardState",
    "BoardAdapter",
    "BoardSession",
    "SensorPodConfig",
    "DeviceRegistry",
    "BOARD_ADAPTERS",
    # Board Adapters
    "SyntheticBoardAdapter",
    "SyntheticPlaybackAdapter",
    "EmotiBitAdapter",
    "CerelogAdapter",
    "PiEEGAdapter",
    "OpenBCIGanglionAdapter",
    "OpenBCICytonAdapter",
    "MuseSAdapter",
    "ESP32Tier0Firmware",
    "ESP32Tier0Config",
]
