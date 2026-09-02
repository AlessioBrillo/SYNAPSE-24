"""Synthetic board adapter for CI/testing."""

from __future__ import annotations

import numpy as np

from .base import BoardAdapter, BoardConfig


class SyntheticBoardConfig:
    """Configuration for synthetic board signal generation."""

    def __init__(
        self,
        board_id: str = "SYNTHETIC_BOARD",
        n_channels: int = 16,
        sampling_rate: int = 500,
        signal_types: dict[int, str] | None = None,
        noise_level: float = 0.1,
        artifact_probability: float = 0.05,
        artifact_types: list[str] | None = None,
    ) -> None:
        self.board_id = board_id
        self.n_channels = n_channels
        self.sampling_rate = sampling_rate
        self.signal_types = signal_types or {
            0: "ecg",
            1: "ppg",
            2: "eeg",
            3: "eeg",
        }
        self.noise_level = noise_level
        self.artifact_probability = artifact_probability
        self.artifact_types = artifact_types or ["motion", "disconnect", "baseline_wander"]

    def to_board_config(self) -> BoardConfig:
        """Convert to BoardConfig."""
        return BoardConfig(
            board_id=self.board_id,
            sampling_rate=self.sampling_rate,
        )


class SyntheticBoardAdapter(BoardAdapter):
    """Adapter for BrainFlow SYNTHETIC_BOARD with realistic signal generation."""

    @property
    def board_id(self) -> str:
        return "SYNTHETIC_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 500

    @property
    def default_channels(self) -> dict[str, list[int]]:
        return {
            "ecg": [0],
            "ppg": [1],
            "eeg": list(range(2, 10)),
            "acc": list(range(10, 13)),
        }

    def create_config(self, **overrides) -> BoardConfig:
        """Create board configuration."""
        config = BoardConfig(board_id=self.board_id, sampling_rate=self.default_sampling_rate)
        for key, value in overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)
        return config

    def get_stream_mapping(self) -> dict[str, dict]:
        """Get LSL stream mapping for synthetic board."""
        return {
            "ECG": {"channels": [0], "type": "ECG_T0", "unit": "µV"},
            "PPG": {"channels": [1], "type": "PPG_T0", "unit": "a.u."},
            "EEG": {"channels": list(range(2, 10)), "type": "EEG_T1", "unit": "µV"},
            "ACC": {"channels": list(range(10, 13)), "type": "ACC_T0", "unit": "g"},
        }

    def generate_synthetic_data(
        self,
        n_samples: int,
        sampling_rate: int | None = None,
        signal_types: dict[int, str] | None = None,
    ) -> np.ndarray:
        """Generate synthetic multimodal data."""
        fs = sampling_rate or self.default_sampling_rate
        n_ch = 16
        t = np.arange(n_samples) / fs
        data = np.zeros((n_ch, n_samples))

        types = signal_types or self.default_signal_types()

        for ch_idx, sig_type in types.items():
            if ch_idx >= n_ch:
                continue

            if sig_type == "ecg":
                data[ch_idx] = self._generate_ecg(t, fs)
            elif sig_type == "ppg":
                data[ch_idx] = self._generate_ppg(t, fs)
            elif sig_type == "eeg":
                data[ch_idx] = self._generate_eeg(t, fs, ch_idx)
            elif sig_type == "acc":
                data[ch_idx] = self._generate_accel(t, fs, ch_idx)

        return data

    def default_signal_types(self) -> dict[int, str]:
        return {0: "ecg", 1: "ppg", 2: "eeg", 3: "eeg"}

    def _generate_ecg(self, t: np.ndarray, fs: int) -> np.ndarray:
        """Generate synthetic ECG with R-peaks."""
        hr = 72  # bpm
        rr_interval = 60.0 / hr
        ecg = np.zeros_like(t)

        # Generate QRS complexes
        for i in np.arange(0, t[-1], rr_interval):
            idx = int(i * fs)
            if idx >= len(t):
                break
            # Gaussian-like R-peak
            window = int(0.08 * fs)
            for j in range(max(0, idx - window), min(len(t), idx + window)):
                dt = (j - idx) / fs
                ecg[j] += 1.5 * np.exp(-(dt**2) / (0.02**2))

        # Add P and T waves
        ecg += 0.2 * np.sin(2 * np.pi * hr / 60 * t)
        ecg += 0.1 * np.sin(2 * np.pi * 2 * hr / 60 * t)

        # Add noise
        ecg += np.random.randn(len(t)) * 0.05

        return ecg * 1000  # µV

    def _generate_ppg(self, t: np.ndarray, fs: int) -> np.ndarray:
        """Generate synthetic PPG."""
        hr = 72
        ppg = 100 + 10 * np.sin(2 * np.pi * hr / 60 * t)
        # Add dicrotic notch
        ppg += 2 * np.sin(2 * np.pi * 2 * hr / 60 * t)
        # Add noise
        ppg += np.random.randn(len(t)) * 0.5
        return ppg

    def _generate_eeg(self, t: np.ndarray, fs: int, ch_idx: int) -> np.ndarray:
        """Generate synthetic EEG with alpha rhythm."""
        # Alpha rhythm (10 Hz) dominant
        eeg = 20 * np.sin(2 * np.pi * 10 * t)
        # Add beta
        eeg += 5 * np.sin(2 * np.pi * 20 * t)
        # Add noise
        eeg += np.random.randn(len(t)) * 3
        return eeg

    def _generate_accel(self, t: np.ndarray, fs: int, axis_idx: int) -> np.ndarray:
        """Generate synthetic accelerometer data."""
        # Low-frequency movement + noise
        accel = 0.5 * np.sin(2 * np.pi * 0.1 * t) + np.random.randn(len(t)) * 0.1
        return accel


class SyntheticPlaybackAdapter(BoardAdapter):
    """Adapter for BrainFlow PLAYBACK_FILE_BOARD."""

    @property
    def board_id(self) -> str:
        return "PLAYBACK_FILE_BOARD"

    @property
    def default_sampling_rate(self) -> int:
        return 0  # From file

    @property
    def default_channels(self) -> dict[str, list[int]]:
        return {}

    def create_config(self, **overrides) -> BoardConfig:
        config = BoardConfig(board_id=self.board_id)
        for key, value in overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)
        return config

    def get_stream_mapping(self) -> dict[str, dict]:
        return {}


BOARD_ADAPTERS = {
    "SYNTHETIC_BOARD": SyntheticBoardAdapter,
    "PLAYBACK_FILE_BOARD": SyntheticPlaybackAdapter,
}
