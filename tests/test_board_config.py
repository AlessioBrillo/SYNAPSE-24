"""Board adapter ``create_config`` contract over the frozen ``BoardConfig``.

``BoardConfig`` is frozen, so the old implementations used ``setattr`` and
raised ``FrozenInstanceError`` whenever a caller passed a field outside the
two or three they special-cased (``channels``, ``preset``, ``ip_address``...).
The contract is pinned here for every registered board, including the
hardware-only adapters that CI never opens.
"""

from __future__ import annotations

import pytest

from synapse24.hardware.base import BoardConfig
from synapse24.hardware.registry import BOARD_ADAPTERS

BOARD_KEYS = sorted(BOARD_ADAPTERS)


@pytest.mark.parametrize("board", BOARD_KEYS)
def test_create_config_applies_known_overrides_and_drops_unknown(board: str) -> None:
    adapter = BOARD_ADAPTERS[board]()
    config = adapter.create_config(
        preset="bench",
        channels=[1, 2],
        timeout=7,
        sampling_rate=123,
        not_a_field="ignored",
    )
    assert isinstance(config, BoardConfig)
    assert config.board_id == adapter.board_id
    assert config.preset == "bench"
    assert config.channels == [1, 2]
    assert config.timeout == 7
    assert config.sampling_rate == 123
    assert not hasattr(config, "not_a_field")


@pytest.mark.parametrize("board", BOARD_KEYS)
def test_create_config_keeps_the_adapter_default_rate(board: str) -> None:
    adapter = BOARD_ADAPTERS[board]()
    config = adapter.create_config()
    assert config.board_id == adapter.board_id
    assert config.sampling_rate == adapter.default_sampling_rate
