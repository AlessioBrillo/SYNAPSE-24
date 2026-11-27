"""Tests for the PPG LED-current sweep."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

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
            "samples": 1500,
            "duration_s": 30.0,
        },
        write=written.append,
    )
    assert captured == ["1A,1A", "1F,1F", "24,24"]
    assert report["chosen"] == "1F,1F"
    assert report["verdict"] == "PASS"
    assert written == [report]


def test_a_sweep_with_no_qualifying_capture_is_a_fail() -> None:
    written: list[dict[str, object]] = []
    report = ps.run_sweep(
        ["1A,1A"],
        apply_led=lambda _pair: None,
        capture=lambda _pair: Path("capture.xdf"),
        analyse=lambda _path: {
            "sqi": 0.1,
            "perfusion_index": 0.1,
            "samples": 1500,
            "duration_s": 30,
        },
        write=written.append,
    )
    assert report["chosen"] is None
    assert report["verdict"] == "FAIL"
    assert written == [report]


def test_short_or_low_sqi_captures_are_not_eligible() -> None:
    report = ps.run_sweep(
        ["a", "b", "c"],
        apply_led=lambda _pair: None,
        capture=Path,
        analyse=lambda path: {
            "sqi": {"a": 0.9, "b": 0.9, "c": 0.1}[path.name],
            "perfusion_index": 0.1,
            "samples": {"a": 10, "b": 1500, "c": 1500}[path.name],
            "duration_s": 30.0,
        },
        write=lambda _report: None,
    )
    assert report["chosen"] == "b"


def test_reboot_between_uses_the_transport_or_fails_clearly() -> None:
    calls: list[str] = []

    class Transport:
        def reboot(self) -> None:
            calls.append("reboot")

    ps._reboot_band(Transport())
    assert calls == ["reboot"]
    with pytest.raises(RuntimeError, match="power-cycle"):
        ps._reboot_band(object())


def test_both_validator_xdf_directories_are_searched(tmp_path: Path, monkeypatch) -> None:
    import json

    config = ROOT / "config" / "hardware_bringup.yaml"
    dirs = ps._xdf_dirs(config)
    assert Path("data/bench") in dirs
    assert Path("data/processed/live_validation") in dirs

    gate = tmp_path / "data" / "live_validation" / "ppg_sweep_gate.json"
    gate.parent.mkdir(parents=True)
    bench_dir = tmp_path / "data" / "bench"
    bench_dir.mkdir(parents=True)
    produced = bench_dir / "synapse_live_1.xdf"
    produced.write_text("xdf", encoding="utf-8")
    gate.write_text(json.dumps({"artifact": {"path": str(produced)}}), encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run(command: list[str], check: bool) -> object:
        captured["command"] = command
        captured["check"] = check
        return type("Proc", (), {"returncode": 0})()

    monkeypatch.setattr(ps.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    capture = ps._live_capture(30, config, tmp_path / "manifest.json", "maintainer")
    assert capture("1F,1F") == produced
    command = captured["command"]
    assert isinstance(command, list)
    assert "--manifest" in command
    assert "--verified-by" in command
