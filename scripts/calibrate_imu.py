#!/usr/bin/env python3
"""Compute static IMU biases from a short, motionless XDF recording.

The firmware stores the bias it must SUBTRACT, in mg and mdps. The live
validator writes ACC in g and GYRO in rad/s into LSL; ``synapse record`` writes
mg and dps. Pick the matching input with ``--units``.

    uv run python scripts/calibrate_imu.py rest.xdf --duration 10 > imu.json
    uv run python scripts/factory_calibrate.py --port COM3 --imu-json imu.json

A recording that is not at rest is refused: a bias learned while the band was
moving would make the motion gate blind instead of calibrating it.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

from synapse24.export import load_xdf_streams

ACC_STREAM = "SYNAPSE_ACC_T0"
GYRO_STREAM = "SYNAPSE_GYRO_T0"
REST_ACCEL_STD_G = 0.02  # Architecture.md immobility threshold
RAD_TO_MDPS = 180.0 / math.pi * 1000.0  # rad/s -> millidegrees/s


def _window(stream: Any, duration_s: float | None) -> np.ndarray:
    data = np.asarray(stream.data, dtype=np.float64)
    if data.ndim != 2 or data.shape[1] != 3:
        raise ValueError(f"{stream.name}: expected 3 channels, got shape {data.shape}")
    if duration_s is not None and duration_s > 0:
        stamps = np.asarray(stream.timestamps, dtype=np.float64)
        if stamps.size > 2:
            start = stamps[-1] - duration_s
            data = data[stamps >= start]
    if data.shape[0] < 50:
        raise ValueError(f"{stream.name}: need at least 50 samples, got {data.shape[0]}")
    return data


def biases(
    path: Path, *, duration_s: float | None, units: str, gravity_axis: str = "z"
) -> dict[str, Any]:
    streams = {stream.name: stream for stream in load_xdf_streams(path)}
    if ACC_STREAM not in streams or GYRO_STREAM not in streams:
        raise ValueError(f"{path}: needs {ACC_STREAM} and {GYRO_STREAM}")

    accel = _window(streams[ACC_STREAM], duration_s)
    gyro = _window(streams[GYRO_STREAM], duration_s)
    accel_g = accel if units == "g_rads" else accel / 1000.0
    gyro_rads = gyro if units == "g_rads" else gyro / (180.0 / math.pi)

    if float(np.std(accel_g, axis=0).max()) > REST_ACCEL_STD_G:
        raise ValueError(
            "the band was not at rest (accel std above "
            f"{REST_ACCEL_STD_G} g): repeat the recording on a still bench"
        )

    # Gravity is not sensor bias: on a still bench it points along one axis.
    # Put the band flat with that axis up and tell the tool which one it is.
    expected = np.zeros(3)
    if gravity_axis != "none":
        axis = "xyz".index(gravity_axis)
        mean_g = np.mean(accel_g, axis=0)
        others = [abs(float(mean_g[i])) for i in range(3) if i != axis]
        # A band resting on its side would otherwise bake +-1 g into the bias
        # and permanently skew the motion gate. Require the declared axis to
        # carry gravity and be clearly dominant.
        if abs(float(mean_g[axis])) < 0.8 or max(others) > abs(float(mean_g[axis])) / 2.0:
            raise ValueError(
                f"the declared gravity axis '{gravity_axis}' does not carry a "
                f"dominant 1 g ({mean_g.round(3).tolist()} g): lay the band flat "
                "with that axis up and pass the correct --gravity-axis"
            )
        expected[axis] = 1.0
    accel_bias_mg = (np.mean(accel_g, axis=0) - expected) * 1000.0

    return {
        "accel_bias_mg": [int(round(float(v))) for v in accel_bias_mg],
        "gyro_bias_mdps": [int(round(float(v))) for v in np.mean(gyro_rads, axis=0) * RAD_TO_MDPS],
        "samples_accel": int(accel.shape[0]),
        "samples_gyro": int(gyro.shape[0]),
        "units": units,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xdf", type=Path)
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument("--units", choices=["g_rads", "mg_dps"], default="g_rads")
    parser.add_argument("--gravity-axis", choices=["x", "y", "z", "none"], default="z")
    args = parser.parse_args()
    try:
        result = biases(
            args.xdf,
            duration_s=args.duration,
            units=args.units,
            gravity_axis=args.gravity_axis,
        )
    except (ValueError, OSError) as exc:
        print(f"IMU calibration failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
