#!/usr/bin/env python3
"""Run a host-C pytest step and fail the gate on *any* skip or failure.

The firmware host tests compile C/C++ translation units with the local
toolchain and then load the freshly built library. On some hosts (Windows
Application Control) the operating system refuses to load that library: the
tests skip with ``-rs`` and pytest exits 0. A green CI step then proves only
that the compiler ran, not that the DSP/parity assertions executed.

This wrapper runs the exact pytest command used by the gate with
``--json-report``, reads the machine-readable summary and exits non-zero when a
test was skipped or failed. The human-readable ``-rs`` output is forwarded
unchanged so the reason for every skip stays visible in the CI log.

Usage:
    uv run python scripts/host_c_gate.py                 # the 13 host-C modules
    uv run python scripts/host_c_gate.py --group parser  # per-unit JSON parser
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Keep the module list byte-for-byte in sync with the host-C step of
# .woodpecker/ci.yml (and scripts/run_gate_locally.sh).
HOST_C_MODULES = (
    "tests/test_research_service_host.py",
    "tests/test_raw_frame_parity.py",
    "tests/test_firmware_host_loadgate.py",
    "tests/test_triage_feature_parity.py",
    "tests/test_firmware_ppg_imu_host.py",
    "tests/test_firmware_ppg_sqi_host.py",
    "tests/test_firmware_ble_contract.py",
    "tests/test_firmware_fit_writer_host.py",
    "tests/test_consumer_v2_parity.py",
    "tests/test_firmware_s3_time_and_loss.py",
    "tests/test_firmware_calibration_host.py",
    "tests/test_ina219_host.py",
    "tests/test_factory_calibrate_cli_host.py",
)

# The compiled per-unit configuration parser is a separate gate step, but it
# needs the same fail-on-skip treatment: a skipped parser test cannot validate
# the JSON contract.
PARSER_MODULES = ("tests/test_firmware_config_parser_host.py",)

GROUPS = {"host-c": HOST_C_MODULES, "parser": PARSER_MODULES}

PYTEST_FLAGS = ("-v", "-rs", "--no-cov", "-p", "no:cacheprovider", "--json-report")

# pytest-json-report uses these keys for outcomes that must never be green.
BAD_SUMMARY_KEYS = ("failed", "error", "errors", "skipped")


def build_command(modules: tuple[str, ...], report: Path) -> list[str]:
    """The gate's pytest command, plus a report file we can parse."""
    return [
        sys.executable,
        "-m",
        "pytest",
        *modules,
        *PYTEST_FLAGS,
        f"--json-report-file={report}",
    ]


def _skip_reason(test: dict[str, Any]) -> str:
    """Extract the ``pytest.skip`` reason from a JSON-report test entry."""
    for phase in ("setup", "call", "teardown"):
        longrepr = (test.get(phase) or {}).get("longrepr")
        if not longrepr:
            continue
        text = str(longrepr)
        marker = "Skipped: "
        if marker in text:
            # longrepr is the repr of pytest's (path, lineno, reason) tuple.
            reason = text.rsplit(marker, 1)[-1].strip()
            if reason.endswith("')"):
                reason = reason[:-2]
            return reason.rstrip("'\"")
        return text.rsplit("\\n", 1)[-1].strip()
    return "(no reason recorded)"


def find_problems(report: dict[str, Any], pytest_exitcode: int) -> list[str]:
    """Everything that makes this gate fail, with a printable reason."""
    problems: list[str] = []
    summary = report.get("summary") or {}
    if not isinstance(summary, dict) or "total" not in summary:
        return ["pytest produced an unreadable JSON summary (missing 'total')"]

    for key in BAD_SUMMARY_KEYS:
        try:
            count = int(summary.get(key, 0))
        except (TypeError, ValueError):
            return [f"pytest produced an invalid JSON summary for '{key}'"]
        if count:
            problems.append(f"{count} {key}")

    for test in report.get("tests") or []:
        if test.get("outcome") == "skipped":
            problems.append(f"SKIPPED {test.get('nodeid', '?')}: {_skip_reason(test)}")
        elif test.get("outcome") in {"failed", "error"}:
            problems.append(f"{test.get('outcome', 'error').upper()} {test.get('nodeid', '?')}")

    if pytest_exitcode != 0 and not problems:
        problems.append(f"pytest exited {pytest_exitcode} without a reported failure")
    return problems


def _report_from(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def run(group: str) -> int:
    """Run pytest for ``group``, returning the gate exit code."""
    modules = GROUPS[group]
    with tempfile.TemporaryDirectory(prefix="host-c-gate-") as tmp:
        report_path = Path(tmp) / "report.json"
        command = build_command(modules, report_path)
        proc = subprocess.run(  # noqa: S603 - command is built from fixed module names
            command,
            cwd=ROOT,
            text=True,
            check=False,
        )
        report = _report_from(report_path)
        if report is None:
            print(
                "HOST C GATE FAILED: pytest did not write a readable JSON report "
                f"(exit code {proc.returncode})",
                file=sys.stderr,
            )
            return 1
        problems = find_problems(report, proc.returncode)

    summary = report.get("summary") or {}
    passed = summary.get("passed", 0) if isinstance(summary, dict) else 0
    if not problems:
        print(f"HOST C GATE OK: {passed} passed, 0 skipped, 0 failed")
        return 0

    print(
        "HOST C GATE FAILED: "
        + "; ".join(p for p in problems if not p.startswith(("SKIPPED ", "FAILED ", "ERROR "))),
        file=sys.stderr,
    )
    for problem in problems:
        if problem.startswith(("SKIPPED ", "FAILED ", "ERROR ")):
            print(f"  {problem}", file=sys.stderr)
    print(
        "A skip is not a pass: replay this step in the gate's Linux container "
        "where no OS policy blocks the freshly built library.",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--group",
        choices=sorted(GROUPS),
        default="host-c",
        help="which pytest step to run (default: the 13 host-C modules)",
    )
    return run(parser.parse_args(argv).group)


if __name__ == "__main__":
    raise SystemExit(main())
