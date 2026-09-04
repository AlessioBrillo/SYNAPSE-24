"""Device registry with all board adapters."""

from __future__ import annotations

from .base import BOARD_ADAPTERS as BASE_BOARD_ADAPTERS
from .cerelog import BOARD_ADAPTERS as CERELOG_BOARD_ADAPTERS
from .emotibit import BOARD_ADAPTERS as EMOTIBIT_BOARD_ADAPTERS
from .esp32_tier0 import BOARD_ADAPTERS as ESP32_BOARD_ADAPTERS
from .synthetic import BOARD_ADAPTERS as SYNTHETIC_BOARD_ADAPTERS

# Merge all board adapters
BOARD_ADAPTERS = {}
BOARD_ADAPTERS.update(BASE_BOARD_ADAPTERS)
BOARD_ADAPTERS.update(SYNTHETIC_BOARD_ADAPTERS)
BOARD_ADAPTERS.update(EMOTIBIT_BOARD_ADAPTERS)
BOARD_ADAPTERS.update(CERELOG_BOARD_ADAPTERS)
BOARD_ADAPTERS.update(ESP32_BOARD_ADAPTERS)

__all__ = ["BOARD_ADAPTERS", "DeviceRegistry", "SensorPodConfig"]

# Re-export base classes
from .base import DeviceRegistry, SensorPodConfig
