"""Tests for the static IMU bias tool."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

from synapse24.utils import verify_xdf_roundtrip

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "calibrate_imu", ROOT / "scripts" / "calibrate_imu.py"
)
assert _spec is not None
assert _spec.loader is not None
ci = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ci
_spec.loader.exec_module(ci)


def _recording(tmp_path: Path, accel_bias: np.ndarray, moving: bool = False) -> Path:
    n = 500
    t = np.arange(n, dtype=np.float64) / 50.0 + 1000.0
    jitter = np.random.default_rng(7).normal(0, 0.0002 if not moving else 0.08, (n, 3))
    acc = np.tile(np.array([0.0, 0.0, 1.0]) + accel_bias, (n, 1)) + jitter
    gyro = np.tile(np.array([500.0, -200.0, 0.0]) / ci.RAD_TO_MDPS, (n, 1)) + jitter
    path = tmp_path / "rest.xdf"
    verify_xdf_roundtrip(
        [
            {"name": "SYNAPSE_ACC_T0", "type": "ACC", "data": acc, "timestamps": t},
            {"name": "SYNAPSE_GYRO_T0", "type": "GYRO", "data": gyro, "timestamps": t},
        ],
        path,
    )
    return path


def test_bias_is_measured_in_firmware_units(tmp_path: Path) -> None:
    path = _recording(tmp_path, np.array([0.05, -0.02, 0.0]))
    result = ci.biases(path, duration_s=None, units="g_rads")
    assert result["accel_bias_mg"] == [50, -20, 0]
    assert result["gyro_bias_mdps"] == pytest.approx([500, -200, 0], abs=2)


def test_moving_recording_is_refused(tmp_path: Path) -> None:
    path = _recording(tmp_path, np.zeros(3), moving=True)
    with pytest.raises(ValueError, match="not at rest"):
        ci.biases(path, duration_s=None, units="g_rads")
