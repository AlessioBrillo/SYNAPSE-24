#!/usr/bin/env python3
"""One entry point for the bench acceptance tests.

Each subcommand writes a machine-readable evidence file under ``evidence/``:
the test ID, the exact command, its exit code and the observations needed to
reproduce it. The suite does not hide a failed command — it records it.

    uv run python scripts/bench_suite.py factory --port COM3
    uv run python scripts/bench_suite.py stream --live --duration 60
    uv run python scripts/bench_suite.py g1-report --live
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
SERIAL_PREFIX = "SYN-"

Runner = Callable[[list[str]], tuple[int, str]]


def _subprocess_runner(command: list[str]) -> tuple[int, str]:
    proc = subprocess.run(  # noqa: S603
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def write_evidence(test_id: str, payload: dict[str, Any]) -> Path:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = EVIDENCE / f"{test_id}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")
    return path


def _execute(
    test_id: str,
    command: list[str],
    runner: Runner,
    *,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code, output = runner(command)
    evidence = {
        "test_id": test_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "command": command,
        "exit_code": code,
        "output_tail": output[-4000:],
        "pass": code == 0,
        "observations": observations or {},
    }
    write_evidence(test_id, evidence)
    return evidence


def incoming(runner: Runner) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for test_id, command in (
        ("T01-toolchain", [sys.executable, "tools/check_toolchain.py"]),
        ("T01-contract", [sys.executable, "tools/check_contract.py"]),
    ):
        checks[test_id] = _execute(test_id, command, runner)
    evidence = {
        "test_id": "T01",
        "manual_rows": [
            "ESP32-S3 is N8R8",
            "no 3V3/GND short",
            "SDA=8 SCL=9 ECG=1 PPG_INT=5 IMU_INT=6",
            "battery 500 mAh and 1000+ mAh present",
            "INA219 answers at 0x40",
        ],
        "checks": checks,
        "pass": all(item["pass"] for item in checks.values()),
    }
    write_evidence("T01", evidence)
    return evidence


def factory(port: str, runner: Runner) -> dict[str, Any]:
    command = [sys.executable, "scripts/factory_calibrate.py", "--port", port, "--dump"]
    evidence = _execute("T02", command, runner)
    reply = evidence["output_tail"]
    record: dict[str, Any] = {}
    for line in reversed(reply.splitlines()):
        if line.strip().startswith("{"):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                pass
            break
    serial = str(record.get("serial", ""))
    evidence["observations"]["serial"] = serial
    evidence["pass"] = evidence["pass"] and serial.startswith(SERIAL_PREFIX) and len(serial) == 16
    write_evidence("T02", evidence)
    return evidence


def sensors(port: str, runner: Runner) -> dict[str, Any]:
    command = [sys.executable, "scripts/factory_calibrate.py", "--port", port, "--dump"]
    evidence = _execute("T03", command, runner)
    evidence["observations"]["note"] = (
        "I2C addresses 0x57/0x68/0x40 must be read from the boot log or the factory test; "
        "a --dump alone cannot prove them"
    )
    write_evidence("T03", evidence)
    return evidence


def stream(args: argparse.Namespace, runner: Runner) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/validate_phase1_entry.py",
        "--config",
        str(args.config),
        "--duration",
        str(args.duration),
        "--gate-report",
        str(args.gate_report),
    ]
    if args.live:
        if not getattr(args, "manifest", None):
            raise ValueError("--live requires --manifest (consent + pseudonym)")
        command += ["--live-hardware", "--manifest", str(args.manifest)]
    if getattr(args, "verified_by", None):
        command += ["--verified-by", str(args.verified_by)]
    evidence = _execute("T05", command, runner)
    evidence["pass"] = evidence["pass"] and args.gate_report.exists()
    write_evidence("T05", evidence)
    return evidence


def g1_report(args: argparse.Namespace, runner: Runner) -> dict[str, Any]:
    command = [
        sys.executable,
        "scripts/validate_phase1_entry.py",
        "--config",
        str(args.config),
        "--duration",
        str(args.duration),
        "--gate-report",
        str(args.gate_report),
    ]
    if args.live:
        if not getattr(args, "manifest", None):
            raise ValueError("--live requires --manifest (consent + pseudonym)")
        command += ["--live-hardware", "--manifest", str(args.manifest)]
    if getattr(args, "verified_by", None):
        command += ["--verified-by", str(args.verified_by)]
    evidence = _execute("T11", command, runner)
    report: dict[str, Any] = {}
    if args.gate_report.exists():
        try:
            report = json.loads(args.gate_report.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    evidence["observations"]["gate_status"] = report.get("overall_status")
    evidence["observations"]["rehearsal"] = report.get("rehearsal")
    write_evidence("T11", evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("incoming", help="toolchain and contract preflight")
    factory_parser = sub.add_parser("factory", help="factory dump + serial check")
    factory_parser.add_argument("--port", default="COM3")
    sensor_parser = sub.add_parser("sensors", help="sensor presence evidence")
    sensor_parser.add_argument("--port", default="COM3")

    def add_run_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--live", action="store_true", help="use --live-hardware")
        p.add_argument("--manifest", type=Path, help="session manifest for a live recording")
        p.add_argument("--verified-by", help="maintainer who verified the hand-filled rows")
        p.add_argument("--duration", type=int, default=60)
        p.add_argument("--config", type=Path, default=ROOT / "config" / "hardware_bringup.yaml")
        p.add_argument(
            "--gate-report",
            type=Path,
            default=ROOT / "data" / "live_validation" / "gate_report.json",
        )

    add_run_args(sub.add_parser("stream", help="60 s streaming gate"))
    add_run_args(sub.add_parser("g1-report", help="write the SOP gate report"))
    args = parser.parse_args()

    runner: Runner = _subprocess_runner
    if args.command == "incoming":
        result = incoming(runner)
    elif args.command == "factory":
        result = factory(args.port, runner)
    elif args.command == "sensors":
        result = sensors(args.port, runner)
    elif args.command == "stream":
        result = stream(args, runner)
    else:
        result = g1_report(args, runner)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
