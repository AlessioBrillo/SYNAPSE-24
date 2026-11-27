"""The bench suite must not publish a green claim over a red gate.

Regression pins for the three fail-open paths found in review: a subprocess
exit code used as a verdict, a sensor row that passed without proving the I²C
addresses, and a gate report inherited from an earlier run.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import bench_suite  # noqa: E402


@pytest.fixture(autouse=True)
def evidence_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench_suite, "EVIDENCE", tmp_path)


def _latest(tmp_path: Path, test_id: str) -> dict:
    files = sorted(tmp_path.glob(f"{test_id}_*.json"))
    assert files, f"no {test_id} evidence written"
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _runner(code: int = 0, output: str = "") -> bench_suite.Runner:
    return lambda _command: (code, output)


def _gate_report(tmp_path: Path, run_id: str, *, passed: bool, rehearsal: bool = False) -> Path:
    path = tmp_path / "gate_report.json"
    path.write_text(
        json.dumps({"run_id": run_id, "overall_pass": passed, "rehearsal": rehearsal}),
        encoding="utf-8",
    )
    return path


class TestVerdictComesFromEvidence:
    def test_a_green_exit_code_with_a_red_gate_report_is_fail(self, tmp_path: Path) -> None:
        report = _gate_report(tmp_path, "run-1", passed=False)
        evidence = bench_suite._stream_evidence(
            "T05",
            "60 s without loss",
            type(
                "Args",
                (),
                {
                    "config": ROOT / "config" / "hardware_bringup.yaml",
                    "duration": 60,
                    "gate_report": report,
                    "live": False,
                    "manifest": None,
                    "verified_by": None,
                    "soak": False,
                },
            )(),
            _runner(0, "PHASE 1 ENTRY GATE: FAIL"),
        )
        assert evidence["exit_code"] == 0
        assert evidence["status"] == "FAIL"
        assert evidence["passed"] is False
        assert _latest(tmp_path, "T05")["status"] == "FAIL"

    def test_a_stale_pass_report_cannot_cover_a_failed_command(self, tmp_path: Path) -> None:
        report = _gate_report(tmp_path, "old-run", passed=True)
        args = type(
            "Args",
            (),
            {
                "config": ROOT / "config" / "hardware_bringup.yaml",
                "duration": 60,
                "gate_report": report,
                "live": False,
                "manifest": None,
                "verified_by": None,
                "soak": False,
            },
        )()
        bench_suite._stream_evidence("T11", "gate report PASS", args, _runner(2, "crashed"))
        evidence = _latest(tmp_path, "T11")
        assert evidence["status"] == "FAIL"
        assert evidence["details"]["fresh"] is False

    def test_a_rehearsal_report_is_never_live_evidence(self, tmp_path: Path) -> None:
        report = _gate_report(tmp_path, "run-2", passed=True, rehearsal=True)
        args = type(
            "Args",
            (),
            {
                "config": ROOT / "config" / "hardware_bringup.yaml",
                "duration": 60,
                "gate_report": report,
                "live": False,
                "manifest": None,
                "verified_by": None,
                "soak": False,
            },
        )()
        bench_suite._stream_evidence("T12", "six-hour soak", args, _runner(0))
        assert _latest(tmp_path, "T12")["status"] == "FAIL"

    def test_a_live_green_report_is_pass(self, tmp_path: Path) -> None:
        # The report on disk before the command is the previous run; the
        # runner writes a new one with a different run_id.
        report = _gate_report(tmp_path, "previous-run", passed=True)
        manifest = tmp_path / "bench.json"
        manifest.write_text("{}", encoding="utf-8")
        args = type(
            "Args",
            (),
            {
                "config": ROOT / "config" / "hardware_bringup.yaml",
                "duration": 60,
                "gate_report": report,
                "live": True,
                "manifest": manifest,
                "verified_by": None,
                "soak": False,
            },
        )()

        def runner(_command: list[str]) -> tuple[int, str]:
            _gate_report(tmp_path, "new-run", passed=True)
            return 0, ""

        evidence = bench_suite._stream_evidence("T05", "60 s without loss", args, runner)
        assert evidence["status"] == "PASS"
        assert evidence["details"]["live_hardware"] is True

    def test_a_synthetic_green_report_is_not_live_evidence(self, tmp_path: Path) -> None:
        report = _gate_report(tmp_path, "run-synth", passed=True)
        args = type(
            "Args",
            (),
            {
                "config": ROOT / "config" / "hardware_bringup.yaml",
                "duration": 60,
                "gate_report": report,
                "live": False,
                "manifest": None,
                "verified_by": None,
                "soak": False,
            },
        )()
        evidence = bench_suite._stream_evidence("T05", "60 s without loss", args, _runner(0))
        assert evidence["status"] == "FAIL"
        assert "--live-hardware" not in evidence["command"]


class TestTwoPodSyncCommand:
    def test_the_default_config_carries_the_sync_thresholds(self) -> None:
        import yaml

        source = (ROOT / "scripts" / "bench_suite.py").read_text(encoding="utf-8")
        assert 'default=ROOT / "config" / "hardware.yaml"' in source
        sync = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))[
            "sync"
        ]
        assert sync["min_acc_correlation"] == 0.7
        assert "acc_correlation_window_s" in sync


class TestSensorPresence:
    def test_without_a_log_the_row_is_fail_even_if_the_dump_succeeds(self, tmp_path: Path) -> None:
        evidence = bench_suite.sensors("COM3", None, _runner(0))
        assert evidence["status"] == "FAIL"
        assert "--log" in evidence["details"]["reason"]

    def test_a_log_missing_an_address_fails(self, tmp_path: Path) -> None:
        log = tmp_path / "boot.log"
        log.write_text("I2C scan: 0x57 answers (MAX30102)\n0x68 answers (ICM)\n", encoding="utf-8")
        evidence = bench_suite.sensors("COM3", log, _runner(0))
        assert evidence["status"] == "FAIL"
        assert "0x40" in evidence["observations"]["missing"]

    def test_all_three_addresses_pass(self, tmp_path: Path) -> None:
        log = tmp_path / "boot.log"
        log.write_text("0x57 MAX30102\n0x68 ICM-20948\n0x40 INA219\n", encoding="utf-8")
        evidence = bench_suite.sensors("COM3", log, _runner(0))
        assert evidence["status"] == "PASS"
