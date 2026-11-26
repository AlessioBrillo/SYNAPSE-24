"""Tests for the PPG LED-current sweep."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np

from synapse24.utils import verify_xdf_roundtrip

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ppg_sweep", ROOT / "scripts" / "ppg_sweep.py")
assert _spec is not None
assert _spec.loader is not None
ps = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ps
_spec.loader.exec_module(ps)


def test_analyse_a_capture(tmp_path: Path) -> None:
    n = 1500
    t = np.arange(n, dtype=np.float64) / 50.0 + 1000.0
    wave = 5000 + 300 * np.sin(2 * np.pi * 1.0 * t)
    path = tmp_path / "ppg.xdf"
    verify_xdf_roundtrip(
        [
            {
                "name": "SYNAPSE_PPG_T0",
                "type": "PPG",
                "data": np.column_stack([wave, wave]),
                "timestamps": t,
            }
        ],
        path,
    )
    result = ps.analyse_capture(path)
    assert result["samples"] > 0
    assert 0.0 <= result["sqi"] <= 1.0
    assert result["perfusion_index"] > 0


def test_the_best_pair_is_chosen_and_persisted() -> None:
    captured: list[str] = []
    written: list[dict[str, object]] = []
    report = ps.run_sweep(
        ["1A,1A", "1F,1F", "24,24"],
        apply_led=captured.append,
        capture=lambda pair: Path(pair.replace(",", "_") + ".xdf"),
        analyse=lambda path: {
            "sqi": {"1A_1A.xdf": 0.3, "1F_1F.xdf": 0.8, "24_24.xdf": 0.5}[path.name],
            "perfusion_index": 0.1,
        },
        write=written.append,
    )
    assert captured == ["1A,1A", "1F,1F", "24,24"]
    assert report["chosen"] == "1F,1F"
    assert written == [report]
