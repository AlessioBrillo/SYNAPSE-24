"""EmotiBit board adapter."""

from __future__ import annotations

from .base import BoardAdapter, BoardConfig


class EmotiBitAdapter(BoardAdapter):
    """Adapter for EmotiBit board (PPG 3λ, EDA, IMU 9-axis, Temp)."""

    @property
    def board_id(self) -> str:
        return "EMOTIBIT_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 15  # PPG at 15 Hz

    @property
    def default_channels(self) -> dict[str, list[int]]:
        # EmotiBit channel mapping per BrainFlow docs
        return {
            "ppg": [0, 1, 2],  # Green, IR, Red
            "eda": [3],
            "temp": [4],
            "acc": [5, 6, 7],
            "gyro": [8, 9, 10],
            "mag": [11, 12, 13],
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
        """Get LSL stream mapping for EmotiBit."""
        return {
            "PPG_GREEN": {"channels": [0], "type": "PPG_T0", "unit": "a.u."},
            "PPG_IR": {"channels": [1], "type": "PPG_T0", "unit": "a.u."},
            "PPG_RED": {"channels": [2], "type": "PPG_T0", "unit": "a.u."},
            "EDA": {"channels": [3], "type": "EDA_T0", "unit": "µS"},
            "TEMP": {"channels": [4], "type": "Temp_T0", "unit": "°C"},
            "ACC": {"channels": [5, 6, 7], "type": "ACC_T0", "unit": "g"},
            "GYRO": {"channels": [8, 9, 10], "type": "GYRO_T0", "unit": "°/s"},
            "MAG": {"channels": [11, 12, 13], "type": "MAG_T0", "unit": "µT"},
        }


BOARD_ADAPTERS = {
    "EMOTIBIT_BOARD": EmotiBitAdapter,
}
