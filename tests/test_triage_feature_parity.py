"""C <-> Python parity for the 26-feature triage extractor (Architecture.md 3A).

Compiles firmware/common/triage/triage_dsp.c on the host and compares it to
extract_triage_features_live() on bit-identical float32 inputs.
"""

from __future__ import annotations

import ctypes

import numpy as np
import pytest

from synapse24.edge_ai.feature_extraction import (
    TRIAGE_IMU_FS_HZ,
    TRIAGE_NUM_FEATURES,
    extract_triage_features_live,
)
from tests.firmware_host import COMMON, build, load  # noqa: E402

FS = int(TRIAGE_IMU_FS_HZ)
N = 4 * FS


@pytest.fixture(scope="module")
def dsp(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
    lib = build(
        tmp_path_factory.mktemp("triage"),
        [COMMON / "triage" / "triage_dsp.c", COMMON / "sensors" / "ppg_dsp.c"],
    )
    d = load(lib)
    fp = ctypes.POINTER(ctypes.c_float)
    d.ppg_bvp_bandpass.restype = None
    d.ppg_bvp_bandpass.argtypes = [fp, ctypes.c_int, fp]
    d.triage_features_from_window.restype = None
    d.triage_features_from_window.argtypes = [
        fp,
        ctypes.c_int,
        fp,
        ctypes.c_int,
        ctypes.c_float,
        fp,
    ]
    return d


def _c_features(d: ctypes.CDLL, imu: np.ndarray, ir: np.ndarray | None) -> np.ndarray:
    fp = ctypes.POINTER(ctypes.c_float)
    imu_c = np.ascontiguousarray(imu[:, :6], dtype=np.float32)
    bvp = np.zeros(0, dtype=np.float32)
    if ir is not None:
        ir_c = np.ascontiguousarray(ir, dtype=np.float32)
        bvp = np.zeros(len(ir_c), dtype=np.float32)
        d.ppg_bvp_bandpass(ir_c.ctypes.data_as(fp), len(ir_c), bvp.ctypes.data_as(fp))
    out = np.zeros(TRIAGE_NUM_FEATURES, dtype=np.float32)
    d.triage_features_from_window(
        imu_c.ctypes.data_as(fp), len(imu_c), bvp.ctypes.data_as(fp), len(bvp),
        ctypes.c_float(FS), out.ctypes.data_as(fp),
    )  # fmt: skip
    return out


def _py_features(imu: np.ndarray, ir: np.ndarray | None) -> np.ndarray:
    ppg = None if ir is None else np.column_stack([np.zeros_like(ir), ir]).astype(np.float64)
    return extract_triage_features_live(imu.astype(np.float64), ppg)


def _case(name: str) -> tuple[np.ndarray, np.ndarray | None]:
    rng = np.random.default_rng(24)
    t = np.arange(N) / FS
    if name == "rest":
        imu = np.zeros((N, 9))
        for i, f in enumerate((0.5, 1.25, 2.5)):
            imu[:, i] = 0.1 * np.sin(2 * np.pi * f * t)
            imu[:, 3 + i] = 5.0 * np.sin(2 * np.pi * (f + 0.25) * t)
        imu[:, 2] += 9.81
        imu += rng.normal(0, 0.01, imu.shape)
        ir = 1e5 + 300 * np.sin(2 * np.pi * 1.2 * t) + rng.normal(0, 5, N)
    elif name == "motion":
        imu = np.cumsum(rng.normal(0, 0.2, (N, 9)), axis=0)
        ir = 1e5 + np.cumsum(rng.normal(0, 20, N))
    elif name == "ir_drift":  # slow DC wander + step: where a free-running filter would diverge
        imu = rng.normal(0, 0.1, (N, 9))
        ir = 1e5 + 2000 * t + 5000 * (t > 2) + 300 * np.sin(2 * np.pi * 1.1 * t)
    elif name == "partial_ppg":
        imu = rng.normal(0, 0.1, (N, 9))
        ir = 1e5 + 300 * np.sin(2 * np.pi * 1.0 * t[:50])
    else:
        raise ValueError(name)
    return imu.astype(np.float32), None if ir is None else ir.astype(np.float32)


@pytest.mark.parametrize("name", ["rest", "motion", "ir_drift", "partial_ppg"])
def test_c_matches_python(dsp: ctypes.CDLL, name: str) -> None:
    imu, ir = _case(name)
    atol = np.full(TRIAGE_NUM_FEATURES, 1e-4)
    if ir is not None:  # BVP mean/std: float32 filter error scales with the input AC amplitude
        atol[24:] = max(1e-4, 1e-6 * float(np.ptp(ir)))
    c, py = _c_features(dsp, imu, ir), _py_features(imu, ir)
    bad = np.abs(c - py) > atol + 1e-4 * np.abs(py)
    assert not bad.any(), (
        f"features {np.flatnonzero(bad)}: C={c[bad]} py={py[bad]} atol={atol[bad]}"
    )


def test_short_imu_returns_zeros(dsp: ctypes.CDLL) -> None:
    imu = np.random.default_rng(1).normal(0, 0.1, (5, 9)).astype(np.float32)
    assert not _c_features(dsp, imu, None).any()
    assert not _py_features(imu, None).any()
