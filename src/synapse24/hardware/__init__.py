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
from .ble_lsl_client import (
    GATT_CHAR_UUID_ECG,
    GATT_CHAR_UUID_IMU,
    GATT_CHAR_UUID_POWER,
    GATT_CHAR_UUID_PPG,
    GATT_CHAR_UUID_SYNC,
    GATT_SERVICE_UUID,
    SAMPLE_NOTIFY_SIZE,
    ClockSyncHost,
    SynapseBleClient,
    SyncExchange,
    pack_sync_request,
    parse_sensor_sample,
    unpack_sync_response,
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
    # BLE LSL bridge host client
    "SynapseBleClient",
    "ClockSyncHost",
    "SyncExchange",
    "pack_sync_request",
    "unpack_sync_response",
    "parse_sensor_sample",
    "GATT_SERVICE_UUID",
    "GATT_CHAR_UUID_PPG",
    "GATT_CHAR_UUID_ECG",
    "GATT_CHAR_UUID_IMU",
    "GATT_CHAR_UUID_SYNC",
    "GATT_CHAR_UUID_POWER",
    "SAMPLE_NOTIFY_SIZE",
]
