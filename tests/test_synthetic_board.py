"""Synthetic board adapter: the hardware-free generator CI relies on.

The adapter is the contract every BrainFlow board shares, so its identity,
channel map and generated signals are pinned here instead of on a bench.
"""

from __future__ import annotations

import numpy as np

from synapse24.hardware.base import BoardConfig
from synapse24.hardware.synthetic import (
    BOARD_ADAPTERS,
    SyntheticBoardAdapter,
    SyntheticBoardConfig,
    SyntheticPlaybackAdapter,
)


class TestSyntheticBoardConfig:
    """Board configuration defaults and conversion."""

    def test_defaults_describe_a_multimodal_board(self) -> None:
        cfg = SyntheticBoardConfig()
        assert cfg.board_id == "SYNTHETIC_BOARD"
        assert cfg.n_channels == 16
        assert cfg.sampling_rate == 500
        assert cfg.signal_types == {0: "ecg", 1: "ppg", 2: "eeg", 3: "eeg"}
        assert cfg.noise_level == 0.1
        assert cfg.artifact_probability == 0.05
        assert cfg.artifact_types == ["motion", "disconnect", "baseline_wander"]

    def test_to_board_config_carries_the_rate(self) -> None:
        board = SyntheticBoardConfig(sampling_rate=250).to_board_config()
        assert isinstance(board, BoardConfig)
        assert board.board_id == "SYNTHETIC_BOARD"
        assert board.sampling_rate == 250


class TestSyntheticBoardAdapter:
    """Adapter identity, mapping and signal generation."""

    def test_identity_and_channel_map(self) -> None:
        adapter = SyntheticBoardAdapter()
        assert adapter.board_id == "SYNTHETIC_BOARD"
        assert adapter.default_sampling_rate == 500
        assert adapter.default_channels["ecg"] == [0]
        assert adapter.default_channels["eeg"] == list(range(2, 10))
        assert adapter.default_channels["acc"] == list(range(10, 13))

    def test_create_config_applies_only_known_frozen_fields(self) -> None:
        adapter = SyntheticBoardAdapter()
        cfg = adapter.create_config(sampling_rate=250, not_a_field=123)
        assert cfg.board_id == "SYNTHETIC_BOARD"
        assert cfg.sampling_rate == 250
        assert not hasattr(cfg, "not_a_field")

    def test_stream_mapping_covers_the_modalities(self) -> None:
        mapping = SyntheticBoardAdapter().get_stream_mapping()
        assert set(mapping) == {"ECG", "PPG", "EEG", "ACC"}
        assert mapping["ECG"]["channels"] == [0]
        assert mapping["EEG"]["type"] == "EEG_T1"
        assert mapping["ACC"]["unit"] == "g"

    def test_generated_data_has_every_default_signal(self) -> None:
        data = SyntheticBoardAdapter().generate_synthetic_data(1000, sampling_rate=250)
        assert data.shape == (16, 1000)
        assert np.isfinite(data).all()
        # ECG is in µV: the R-peaks must be real, not a flat line.
        assert data[0].max() > 300.0
        # Only channels 0..3 carry a default signal.
        assert not data[4:].any()

    def test_custom_signal_types_skip_out_of_range_channels(self) -> None:
        adapter = SyntheticBoardAdapter()
        data = adapter.generate_synthetic_data(
            500, sampling_rate=100, signal_types={0: "acc", 12: "ppg", 99: "ecg"}
        )
        assert data.shape == (16, 500)
        assert np.isfinite(data).all()
        assert data[0].std() > 0.0  # accel on channel 0
        assert data[12].mean() > 50.0  # PPG baseline on channel 12
        assert not data[1].any()  # nothing was requested for channel 1


class TestSyntheticPlaybackAdapter:
    """The playback adapter exposes the file-driven identity."""

    def test_playback_adapter_has_no_fixed_streams(self) -> None:
        adapter = SyntheticPlaybackAdapter()
        assert adapter.board_id == "PLAYBACK_FILE_BOARD"
        assert adapter.default_sampling_rate == 0  # from the file
        assert adapter.default_channels == {}
        assert adapter.get_stream_mapping() == {}
        cfg = adapter.create_config(sampling_rate=100, not_a_field=1)
        assert isinstance(cfg, BoardConfig)
        assert cfg.sampling_rate == 100

    def test_registry_resolves_both_boards(self) -> None:
        assert BOARD_ADAPTERS["SYNTHETIC_BOARD"] is SyntheticBoardAdapter
        assert BOARD_ADAPTERS["PLAYBACK_FILE_BOARD"] is SyntheticPlaybackAdapter
