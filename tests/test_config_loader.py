"""Config loader: env expansion, deep merge and the real repository configs.

The loaders feed every hardware script, so the merge order (base < bringup)
and the "missing env var stays visible" rule are pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse24.config.loader import (
    _deep_merge,
    _expand_env_vars,
    load_hardware_config,
    load_hardware_config_from_path,
)


class TestEnvExpansion:
    """``${VAR}`` and ``${VAR:-default}`` semantics."""

    def test_expands_vars_defaults_and_keeps_missing_ones_visible(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SYNAPSE_TEST_ADDR", "aa:bb")
        monkeypatch.delenv("SYNAPSE_TEST_MISSING", raising=False)
        expanded = _expand_env_vars(
            {
                "present": "${SYNAPSE_TEST_ADDR}",
                "default": "${SYNAPSE_TEST_MISSING:-fallback}",
                "missing": "${SYNAPSE_TEST_MISSING}",
                "list": ["${SYNAPSE_TEST_ADDR}"],
                "number": 7,
            }
        )
        assert expanded["present"] == "aa:bb"
        assert expanded["default"] == "fallback"
        assert expanded["missing"] == "${SYNAPSE_TEST_MISSING}"  # never silently blanked
        assert expanded["list"] == ["aa:bb"]
        assert expanded["number"] == 7


class TestDeepMerge:
    """Recursive merge without mutating the base."""

    def test_merge_is_recursive_and_base_is_untouched(self) -> None:
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        override = {"nested": {"y": 3, "z": 4}, "b": 5}
        merged = _deep_merge(base, override)
        assert merged == {"a": 1, "b": 5, "nested": {"x": 1, "y": 3, "z": 4}}
        assert base == {"a": 1, "nested": {"x": 1, "y": 2}}

    def test_scalar_override_wins_over_a_dict(self) -> None:
        merged = _deep_merge({"k": {"a": 1}}, {"k": "plain"})
        assert merged == {"k": "plain"}


class TestLoadFromPath:
    """Explicit-path loading used by the scripts."""

    def test_base_only_bringup_merge_and_missing_override(self, tmp_path: Path) -> None:
        base = tmp_path / "base.yaml"
        base.write_text("ble:\n  name: SYNAPSE\nrate:\n  ecg: 500\n", encoding="utf-8")
        bringup = tmp_path / "bringup.yaml"
        bringup.write_text("bringup:\n  rate:\n    ecg: 250\n", encoding="utf-8")

        plain = load_hardware_config_from_path(base)
        assert plain["rate"]["ecg"] == 500

        merged = load_hardware_config_from_path(base, bringup)
        assert merged["bringup"]["rate"]["ecg"] == 250  # override wins
        assert merged["bringup"]["ble"]["name"] == "SYNAPSE"  # base keys survive

        missing = load_hardware_config_from_path(base, tmp_path / "nope.yaml")
        assert missing["bringup"]["rate"]["ecg"] == 500


class TestRepositoryConfigs:
    """The committed configs must load through the public entry point."""

    def test_base_and_bringup_configs_load(self) -> None:
        base = load_hardware_config()
        assert "ble" in base
        assert "raw_frame" in base

        bringup = load_hardware_config(bringup=True)
        assert "bringup" in bringup
        assert "ble" in bringup["bringup"]
