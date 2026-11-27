#!/usr/bin/env python3
"""One entry point for the bench acceptance tests.

Each subcommand writes a machine-readable evidence file under ``evidence/``:
the test ID, the exact command, its exit code, the *claim* being made and the
verdict derived from the observations. A subprocess exit code alone is never a
verdict: a gate report that says FAIL, a missing I²C address or a stale report
from a previous run all produce a red evidence file.

    uv run python scripts/bench_suite.py incoming
    uv run python scripts/bench_suite.py sensors --port COM3 --log boot.log
    uv run python scripts/bench_suite.py stream --live --duration 60
    uv run python scripts/bench_suite.py g1-report --live
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
# The addresses the bringup rig must answer on the shared I²C bus.
REQUIRED_I2C = {"0x57": "MAX30102 PPG", "0x68": "ICM-20948 IMU", "0x40": "INA219"}
I2C_LINE = re.compile(r"(0x5[47]|0x68|0x40)", re.IGNORECASE)

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


def verdict(passed: bool, claim: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    """One canonical status field for every evidence file."""
    return {
        "claim": claim,
        # `pass` is kept as an alias for the existing tooling; `passed` and
        # `status` are the fields new evidence consumers should read.
        "pass": bool(passed),
        "passed": bool(passed),
        "status": "PASS" if passed else "FAIL",
        "details": details or {},
    }


def _execute(
    test_id: str,
    command: list[str],
    runner: Runner,
    *,
    claim: str,
    observations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    code, output = runner(command)
    evidence = {
        "test_id": test_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "command": command,
        "exit_code": code,
        "output_tail": output[-4000:],
        **verdict(code == 0, claim),
        "observations": observations or {},
    }
    write_evidence(test_id, evidence)
    return evidence


def _read_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _report_verdict(path: Path, previous: dict[str, Any], command: list[str]) -> dict[str, Any]:
    """A gate report is evidence only when it is fresh, live and green.

    ``run_id`` must differ from the report that was on disk before the command
    ran: otherwise a failed command could inherit a PASS from an earlier run.
    A synthetic run writes a green report too (the normal spool path carries
    no ``rehearsal`` marker), so the command itself must ask for live hardware.
    """
    report = _read_report(path)
    run_id = str(report.get("run_id") or "")
    live = "--live-hardware" in command
    details = {
        "run_id": run_id or None,
        "overall_pass": report.get("overall_pass"),
        "rehearsal": report.get("rehearsal"),
        "live_hardware": live,
        "fresh": bool(run_id) and run_id != str(previous.get("run_id") or ""),
    }
    passed = (
        details["fresh"]
        and live
        and report.get("overall_pass") is True
        and report.get("rehearsal") is False
    )
    return verdict(passed, "the SOP gate report of THIS run is live and PASS", details)


def incoming(runner: Runner) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    for test_id, command in (
        ("T01-toolchain", [sys.executable, "tools/check_toolchain.py"]),
        ("T01-contract", [sys.executable, "tools/check_contract.py"]),
    ):
        checks[test_id] = _execute(
            test_id, command, runner, claim="toolchain and contract are green"
        )
    evidence = {
        "test_id": "T01",
        "timestamp": datetime.now(UTC).isoformat(),
        "manual_rows": [
            "ESP32-S3 is N8R8",
            "no 3V3/GND short",
            "SDA=8 SCL=9 ECG=1 PPG_INT=5 IMU_INT=6",
            "battery 500 mAh and 1000+ mAh present",
            "INA219 answers at 0x40",
        ],
        "checks": checks,
        **verdict(all(item["passed"] for item in checks.values()), "kit and toolchain are usable"),
    }
    write_evidence("T01", evidence)
    return evidence


def factory(port: str, runner: Runner) -> dict[str, Any]:
    command = [sys.executable, "scripts/factory_calibrate.py", "--port", port, "--dump"]
    evidence = _execute(
        "T02",
        command,
        runner,
        claim="the calibration record carries the identity-bound SYN- + 12 hex serial",
    )
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
    valid = re.fullmatch(r"SYN-[0-9A-F]{12}", serial) is not None
    evidence.update(
        verdict(
            evidence["passed"] and valid,
            "DIS and calibration carry the same SYN- + 12 hex serial",
            {"serial": serial},
        )
    )
    write_evidence("T02", evidence)
    return evidence


def sensors(port: str, log: Path | None, runner: Runner) -> dict[str, Any]:
    """T03 must prove the addresses, not merely that a dump command exited 0."""
    if log is None:
        evidence = {
            "test_id": "T03",
            "timestamp": datetime.now(UTC).isoformat(),
            **verdict(
                False,
                "0x57 PPG, 0x68 IMU and 0x40 INA219 answer on the bus",
                {
                    "reason": "--log <boot or factory log> is required; a --dump alone proves nothing"
                },
            ),
        }
        write_evidence("T03", evidence)
        return evidence
    command = [sys.executable, "scripts/factory_calibrate.py", "--port", port, "--dump"]
    evidence = _execute(
        "T03", command, runner, claim="0x57 PPG, 0x68 IMU and 0x40 INA219 answer on the bus"
    )
    text = log.read_text(encoding="utf-8", errors="replace")
    seen = set(I2C_LINE.findall(text.lower()))
    missing = sorted(set(REQUIRED_I2C) - seen)
    evidence["observations"].update(
        {"log": str(log), "addresses_seen": sorted(seen), "missing": missing}
    )
    evidence.update(
        verdict(
            evidence["passed"] and not missing,
            "0x57 PPG, 0x68 IMU and 0x40 INA219 answer on the bus",
            {"missing": missing},
        )
    )
    write_evidence("T03", evidence)
    return evidence


def _run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--live", action="store_true", help="use --live-hardware")
    p.add_argument("--manifest", type=Path, help="session manifest (human or bench)")
    p.add_argument("--verified-by", help="maintainer who verified the hand-filled rows")
    p.add_argument("--duration", type=int, default=60)
    p.add_argument("--config", type=Path, default=ROOT / "config" / "hardware_bringup.yaml")
    p.add_argument(
        "--gate-report",
        type=Path,
        default=ROOT / "data" / "live_validation" / "gate_report.json",
    )
    p.add_argument("--soak", action="store_true", help="record the soak via LabRecorderCLI")


def _validator_command(args: argparse.Namespace) -> list[str]:
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
            raise ValueError("--live requires --manifest (human consent or bench identity)")
        command += ["--live-hardware", "--manifest", str(args.manifest)]
    if getattr(args, "verified_by", None):
        command += ["--verified-by", str(args.verified_by)]
    if getattr(args, "soak", False):
        command += ["--soak"]
    return command


def _stream_evidence(
    test_id: str, claim: str, args: argparse.Namespace, runner: Runner
) -> dict[str, Any]:
    command = _validator_command(args)
    previous = _read_report(args.gate_report)
    code, output = runner(command)
    result = _report_verdict(args.gate_report, previous, command)
    evidence = {
        "test_id": test_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "command": command,
        "exit_code": code,
        "output_tail": output[-4000:],
        "observations": {"gate_report": str(args.gate_report)},
        "claim": claim,
        "status": "PASS" if result["passed"] and code == 0 else "FAIL",
        "pass": result["passed"] and code == 0,
        "passed": result["passed"] and code == 0,
        "details": result["details"],
    }
    write_evidence(test_id, evidence)
    return evidence


def sync(pods: list[str], config: Path, out: Path, runner: Runner) -> dict[str, Any]:
    """T10: the pod↔pod residual that gates the Cerelog order."""
    command = [
        sys.executable,
        "scripts/measure_two_pod_sync.py",
        "--pods",
        *pods,
        "--config",
        str(config),
        "--out",
        str(out),
    ]
    evidence = _execute("T10", command, runner, claim="pod-to-pod residual <= 10 ms over 30 min")
    if out.exists():
        try:
            measurement = json.loads(out.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            measurement = {}
        evidence["observations"]["measurement"] = measurement
        residual = measurement.get("residual_ms")
        correlated = bool(measurement.get("correlated"))
        evidence.update(
            verdict(
                evidence["passed"]
                and correlated
                and isinstance(residual, (int, float))
                and float(residual) <= 10.0,
                "pod-to-pod residual <= 10 ms over 30 min",
                {"residual_ms": residual, "correlated": correlated},
            )
        )
    write_evidence("T10", evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("incoming", help="toolchain and contract preflight (T01)")
    factory_parser = sub.add_parser("factory", help="identity + calibration record (T02)")
    factory_parser.add_argument("--port", default="COM3")
    sensor_parser = sub.add_parser("sensors", help="I²C presence from the boot log (T03)")
    sensor_parser.add_argument("--port", default="COM3")
    sensor_parser.add_argument("--log", type=Path, help="boot/factory log to parse")
    _run_args(sub.add_parser("stream", help="60 s streaming gate (T05)"))
    _run_args(sub.add_parser("g1-report", help="SOP gate report (T11)"))
    _run_args(sub.add_parser("soak", help="six-hour soak (T12)"))
    sync_parser = sub.add_parser("sync", help="two-pod residual (T10)")
    sync_parser.add_argument("--pods", nargs=2, required=True, metavar=("XDF_A", "XDF_B"))
    # The sync thresholds live in the main contract; hardware_bringup.yaml
    # only selects the sync *mode* and carries no correlation threshold.
    sync_parser.add_argument("--config", type=Path, default=ROOT / "config" / "hardware.yaml")
    sync_parser.add_argument(
        "--out", type=Path, default=ROOT / "data" / "live_validation" / "pod_pair_sync.json"
    )
    args = parser.parse_args()

    runner: Runner = _subprocess_runner
    if args.command == "incoming":
        result = incoming(runner)
    elif args.command == "factory":
        result = factory(args.port, runner)
    elif args.command == "sensors":
        result = sensors(args.port, args.log, runner)
    elif args.command == "sync":
        result = sync(args.pods, args.config, args.out, runner)
    elif args.command == "stream":
        result = _stream_evidence("T05", "60 s of raw streaming without loss", args, runner)
    elif args.command == "soak":
        args.soak = True
        result = _stream_evidence(
            "T12", "six-hour soak: zero drops, monotonic XDF, real recording", args, runner
        )
    else:
        result = _stream_evidence(
            "T11", "the SOP gate report of THIS run is live and PASS", args, runner
        )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
