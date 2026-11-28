"""The fail-on-skip wrapper for the firmware host-C gate.

A skipped host-C test is not a pass: pytest exits 0 and the pipeline goes
green even though the DLL was never loaded. These tests pin the wrapper's
command, its report parsing and—above all—its exit code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import host_c_gate as hcg


def _fake_pytest(
    monkeypatch: pytest.MonkeyPatch,
    summary: dict[str, int],
    tests: list[dict[str, object]] | None = None,
    exitcode: int = 0,
    write_report: bool = True,
) -> list[str]:
    """Replace pytest with a stub that writes a JSON report like the plugin."""
    calls: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(" ".join(command))
        if write_report:
            report_arg = next(arg for arg in command if arg.startswith("--json-report-file="))
            path = Path(report_arg.partition("=")[2])
            path.write_text(
                json.dumps({"summary": summary, "tests": tests or []}), encoding="utf-8"
            )
        return SimpleNamespace(returncode=exitcode, stdout="", stderr="")

    monkeypatch.setattr(hcg.subprocess, "run", fake_run)
    return calls


def test_command_runs_the_exact_gate_modules() -> None:
    command = hcg.build_command(hcg.HOST_C_MODULES, Path("report.json"))
    assert command[:3] == [sys.executable, "-m", "pytest"]
    first_flag = 3 + len(hcg.HOST_C_MODULES)
    assert command[3:first_flag] == list(hcg.HOST_C_MODULES)
    assert command[first_flag:] == [
        "-v",
        "-rs",
        "--no-cov",
        "-p",
        "no:cacheprovider",
        "--json-report",
        "--json-report-file=report.json",
    ]
    assert len(hcg.HOST_C_MODULES) == 13
    assert hcg.PARSER_MODULES == ("tests/test_firmware_config_parser_host.py",)


def test_group_argument_selects_the_parser_step(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_run(group: str) -> int:
        calls.append(group)
        return 0

    monkeypatch.setattr(hcg, "run", fake_run)
    assert hcg.main(["--group", "parser"]) == 0
    assert calls == ["parser"]
    assert hcg.main([]) == 0
    assert calls == ["parser", "host-c"]


def test_passing_summary_is_green(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_pytest(monkeypatch, {"passed": 12, "total": 12})
    assert hcg.run("host-c") == 0
    assert "HOST C GATE OK: 12 passed, 0 skipped, 0 failed" in capsys.readouterr().out


def test_a_single_skip_turns_the_gate_red(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_pytest(
        monkeypatch,
        {"passed": 11, "skipped": 1, "total": 12},
        [
            {
                "nodeid": "tests/test_ina219_host.py::test_power",
                "outcome": "skipped",
                "setup": {
                    "outcome": "skipped",
                    "longrepr": "('test_ina219_host.py', 60, "
                    "'Skipped: OS policy blocked loading the library')",
                },
            }
        ],
    )
    assert hcg.run("host-c") == 1
    captured = capsys.readouterr()
    assert "HOST C GATE FAILED" in captured.err
    assert "1 skipped" in captured.err
    assert "tests/test_ina219_host.py::test_power" in captured.err
    assert "OS policy blocked loading the library" in captured.err


def test_a_failed_test_turns_the_gate_red(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_pytest(
        monkeypatch,
        {"passed": 11, "failed": 1, "total": 12},
        [{"nodeid": "tests/test_x.py::test_y", "outcome": "failed"}],
    )
    assert hcg.run("host-c") == 1
    assert "1 failed" in capsys.readouterr().err


def test_missing_report_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_pytest(monkeypatch, {}, write_report=False)
    assert hcg.run("parser") == 1
    assert "did not write a readable JSON report" in capsys.readouterr().err


def test_pytest_error_without_reported_tests_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_pytest(monkeypatch, {"total": 0, "collected": 0}, exitcode=2)
    assert hcg.run("parser") == 1
    assert "pytest exited 2" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ({"total": 5, "skipped": 2}, "2 skipped"),
        ({"total": 5, "failed": 1}, "1 failed"),
        ({"total": 5, "error": 1}, "1 error"),
        ({"total": 5, "passed": 5}, None),
    ],
)
def test_summary_outcomes(summary: dict[str, int], expected: str | None) -> None:
    problems = hcg.find_problems({"summary": summary, "tests": []}, 0)
    if expected is None:
        assert problems == []
    else:
        assert expected in problems


def test_malformed_summary_fails_closed() -> None:
    assert hcg.find_problems({"summary": {"skipped": 1}}, 0)
    assert hcg.find_problems({"summary": "broken"}, 0)
