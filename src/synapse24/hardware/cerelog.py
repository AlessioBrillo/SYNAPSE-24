"""Cerelog ESP-EEG board adapter."""

from __future__ import annotations

from .base import BoardAdapter, BoardConfig


class CerelogAdapter(BoardAdapter):
    """Adapter for Cerelog ESP-EEG board (8-ch EEG, IMU 9-axis)."""

    @property
    def board_id(self) -> str:
        return "CERELOG_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 250  # EEG at 250 Hz

    @property
    def default_channels(self) -> dict[str, list[int]]:
        # Cerelog ESP-EEG channel mapping
        return {
            "eeg": list(range(8)),  # 8 EEG channels
            "acc": [8, 9, 10],
            "gyro": [11, 12, 13],
            "mag": [14, 15, 16],
        }

    def create_config(self, **overrides) -> BoardConfig:
        config = BoardConfig(
            board_id=self.board_id,
            mac_address=overrides.get("mac_address", ""),
            serial_port=overrides.get("serial_port", ""),
            sampling_rate=overrides.get("sampling_rate", self.default_sampling_rate),
        )
        for key, value in overrides.items():
            if hasattr(config, key) and key not in ("mac_address", "serial_port", "sampling_rate"):
                setattr(config, key, value)
        return config

    def get_stream_mapping(self) -> dict[str, dict]:
        """Get LSL stream mapping for Cerelog ESP-EEG."""
        return {
            "EEG": {"channels": list(range(8)), "type": "EEG_T1", "unit": "µV"},
            "ACC": {"channels": [8, 9, 10], "type": "ACC_T1", "unit": "g"},
            "GYRO": {"channels": [11, 12, 13], "type": "GYRO_T1", "unit": "°/s"},
            "MAG": {"channels": [14, 15, 16], "type": "MAG_T1", "unit": "µT"},
        }


class PiEEGAdapter(BoardAdapter):
    """Adapter for PiEEG board (8/16-ch EEG, IMU 9-axis)."""

    @property
    def board_id(self) -> str:
        return "PIEEG_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 250

    @property
    def default_channels(self) -> dict[str, list[int]]:
        return {
            "eeg": list(range(16)),  # Up to 16 EEG channels
            "acc": [16, 17, 18],
            "gyro": [19, 20, 21],
            "mag": [22, 23, 24],
        }

    def create_config(self, **overrides) -> BoardConfig:
        config = BoardConfig(
            board_id=self.board_id,
            serial_port=overrides.get("serial_port", ""),
            sampling_rate=overrides.get("sampling_rate", self.default_sampling_rate),
        )
        for key, value in overrides.items():
            if hasattr(config, key) and key not in ("serial_port", "sampling_rate"):
                setattr(config, key, value)
        return config

    def get_stream_mapping(self) -> dict[str, dict]:
        return {
            "EEG": {"channels": list(range(16)), "type": "EEG_T1", "unit": "µV"},
            "ACC": {"channels": [16, 17, 18], "type": "ACC_T1", "unit": "g"},
            "GYRO": {"channels": [19, 20, 21], "type": "GYRO_T1", "unit": "°/s"},
            "MAG": {"channels": [22, 23, 24], "type": "MAG_T1", "unit": "µT"},
        }


class OpenBCIGanglionAdapter(BoardAdapter):
    """Adapter for OpenBCI Ganglion (4-ch EEG, IMU 9-axis)."""

    @property
    def board_id(self) -> str:
        return "GANGLION_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 200

    @property
    def default_channels(self) -> dict[str, list[int]]:
        return {
            "eeg": list(range(4)),
            "acc": [4, 5, 6],
            "gyro": [7, 8, 9],
            "mag": [10, 11, 12],
        }

    def create_config(self, **overrides) -> BoardConfig:
        config = BoardConfig(
            board_id=self.board_id,
            mac_address=overrides.get("mac_address", ""),
            sampling_rate=overrides.get("sampling_rate", self.default_sampling_rate),
        )
        for key, value in overrides.items():
            if hasattr(config, key) and key not in ("mac_address", "sampling_rate"):
                setattr(config, key, value)
        return config

    def get_stream_mapping(self) -> dict[str, dict]:
        return {
            "EEG": {"channels": list(range(4)), "type": "EEG_T1", "unit": "µV"},
            "ACC": {"channels": [4, 5, 6], "type": "ACC_T1", "unit": "g"},
            "GYRO": {"channels": [7, 8, 9], "type": "GYRO_T1", "unit": "°/s"},
            "MAG": {"channels": [10, 11, 12], "type": "MAG_T1", "unit": "µT"},
        }


class OpenBCICytonAdapter(BoardAdapter):
    """Adapter for OpenBCI Cyton (8/16-ch EEG, IMU 9-axis)."""

    @property
    def board_id(self) -> str:
        return "CYTON_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 250

    @property
    def default_channels(self) -> dict[str, list[int]]:
        return {
            "eeg": list(range(16)),
            "acc": [16, 17, 18],
            "gyro": [19, 20, 21],
            "mag": [22, 23, 24],
        }

    def create_config(self, **overrides) -> BoardConfig:
        config = BoardConfig(
            board_id=self.board_id,
            serial_port=overrides.get("serial_port", ""),
            sampling_rate=overrides.get("sampling_rate", self.default_sampling_rate),
        )
        for key, value in overrides.items():
            if hasattr(config, key) and key not in ("serial_port", "sampling_rate"):
                setattr(config, key, value)
        return config

    def get_stream_mapping(self) -> dict[str, dict]:
        return {
            "EEG": {"channels": list(range(16)), "type": "EEG_T1", "unit": "µV"},
            "ACC": {"channels": [16, 17, 18], "type": "ACC_T1", "unit": "g"},
            "GYRO": {"channels": [19, 20, 21], "type": "GYRO_T1", "unit": "°/s"},
            "MAG": {"channels": [22, 23, 24], "type": "MAG_T1", "unit": "µT"},
        }


class MuseSAdapter(BoardAdapter):
    """Adapter for Muse S (4-5 ch EEG, PPG, IMU 9-axis)."""

    @property
    def board_id(self) -> str:
        return "MUSE_S_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 256  # EEG at 256 Hz

    @property
    def default_channels(self) -> dict[str, list[int]]:
        return {
            "eeg": list(range(4)),  # TP9, AF7, AF8, TP10
            "ppg": [4],
            "acc": [5, 6, 7],
            "gyro": [8, 9, 10],
        }

    def create_config(self, **overrides) -> BoardConfig:
        config = BoardConfig(
            board_id=self.board_id,
            mac_address=overrides.get("mac_address", ""),
            sampling_rate=overrides.get("sampling_rate", self.default_sampling_rate),
        )
        for key, value in overrides.items():
            if hasattr(config, key) and key not in ("mac_address", "sampling_rate"):
                setattr(config, key, value)
        return config

    def get_stream_mapping(self) -> dict[str, dict]:
        return {
            "EEG": {"channels": list(range(4)), "type": "EEG_T0", "unit": "µV"},
            "PPG": {"channels": [4], "type": "PPG_T0", "unit": "a.u."},
            "ACC": {"channels": [5, 6, 7], "type": "ACC_T0", "unit": "g"},
            "GYRO": {"channels": [8, 9, 10], "type": "GYRO_T0", "unit": "°/s"},
        }


BOARD_ADAPTERS = {
    "CERELOG_BOARD": CerelogAdapter,
    "PIEEG_BOARD": PiEEGAdapter,
    "GANGLION_BOARD": OpenBCIGanglionAdapter,
    "CYTON_BOARD": OpenBCICytonAdapter,
    "MUSE_S_BOARD": MuseSAdapter,
}
