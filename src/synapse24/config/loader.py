"""Configuration loader for SYNAPSE-24 hardware and bringup configs.

Provides deep merging of base hardware config with bringup-specific overrides.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import yaml


def _expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in strings."""
    if isinstance(obj, str):
        # Handle ${VAR} and ${VAR:-default} syntax
        import re

        def replace_var(match: re.Match[str]) -> str:
            full = match.group(0)
            inner = match.group(1)
            if ":-" in inner:
                var, default = inner.split(":-", 1)
                return os.environ.get(var, default)
            return os.environ.get(inner, full)

        return re.sub(r"\$\{([^}]+)\}", replace_var, obj)
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    return obj


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override into base, returning new dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _wrap_base_for_bringup(base_config: dict[str, Any]) -> dict[str, Any]:
    """Wrap base hardware config under 'bringup' key for bringup script compatibility."""
    return {"bringup": base_config}


def load_hardware_config(bringup: bool = False) -> dict[str, Any]:
    """Load hardware configuration with optional bringup overrides.

    Args:
        bringup: If True, merge with hardware_bringup.yaml overrides and
            wrap under 'bringup' key for script compatibility.

    Returns:
        Merged configuration dictionary with env vars expanded.
    """
    config_dir = Path(__file__).parent.parent.parent.parent / "config"

    # Load base hardware config
    base_path = config_dir / "hardware.yaml"
    with base_path.open("r", encoding="utf-8") as f:
        base_config = cast("dict[str, Any]", yaml.safe_load(f))

    if not bringup:
        return cast("dict[str, Any]", _expand_env_vars(base_config))

    # Load bringup overrides
    bringup_path = config_dir / "hardware_bringup.yaml"
    if not bringup_path.exists():
        return cast("dict[str, Any]", _expand_env_vars(_wrap_base_for_bringup(base_config)))

    with bringup_path.open("r", encoding="utf-8") as f:
        bringup_config = cast("dict[str, Any]", yaml.safe_load(f))

    # Wrap base config under 'bringup' key, then merge bringup overrides
    wrapped_base = _wrap_base_for_bringup(base_config)
    merged = _deep_merge(wrapped_base, bringup_config)
    return cast("dict[str, Any]", _expand_env_vars(merged))


def load_hardware_config_from_path(
    base_path: str | Path, bringup_path: str | Path | None = None
) -> dict[str, Any]:
    """Load hardware config from explicit paths (for scripts)."""
    base_path = Path(base_path)
    with base_path.open("r", encoding="utf-8") as f:
        base_config = cast("dict[str, Any]", yaml.safe_load(f))

    if bringup_path is None:
        return cast("dict[str, Any]", _expand_env_vars(base_config))

    bringup_path = Path(bringup_path)
    if not bringup_path.exists():
        return cast("dict[str, Any]", _expand_env_vars(_wrap_base_for_bringup(base_config)))

    with bringup_path.open("r", encoding="utf-8") as f:
        bringup_config = cast("dict[str, Any]", yaml.safe_load(f))

    # Wrap base config under 'bringup' key, then merge bringup overrides
    wrapped_base = _wrap_base_for_bringup(base_config)
    merged = _deep_merge(wrapped_base, bringup_config)
    return cast("dict[str, Any]", _expand_env_vars(merged))
