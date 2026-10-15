#!/usr/bin/env python3
"""Pre-acquisition gate: one command that answers "can I order the kit?".

Runs the checks that must ALL be green before a euro leaves the account,
because each of them is a property that is either provable today or becomes a
bench weekend later:

  1. the hardware contract still holds (rates, pins, UUIDs, codec)     — check_contract
  2. the firmware actually transmits (behavioural, not compile-only)   — test_research_service_host
  3. the raw wire format round-trips C<->Python                        — test_raw_frame_parity
  4. the firmware host compile gates pass                              — firmware host gates
  5. the live path (decoder, clock model, sync gate, SOP) is exercised — validator/SOP/wrap tests

Exit codes: 0 = ORDER THE KIT, 1 = DO NOT ORDER (a gate failed), 2 = setup error.

Note: `tools/check_toolchain.py` is deliberately NOT part of the verdict —
it depends on ESP-IDF's environment being active in the shell (it reads
IDF_PATH/PATH, not the install directory). Run it separately, as
docs/hardware/BRINGUP_SOP.md instructs; G0 is closed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (label, argv) — every entry must exit 0 for the verdict to be ORDER.
CHECKS: list[tuple[str, list[str]]] = [
    (
        "hardware contract (rates, pins, UUIDs, codec)",
        [sys.executable, str(ROOT / "tools" / "check_contract.py")],
    ),
    (
        "research service transmits (behavioural gate)",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_research_service_host.py",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
    ),
    (
        "raw frame parity C<->Python",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_raw_frame_parity.py",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
    ),
    (
        "firmware host compile gates (research service + codec + INA219 + triage parity)",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_research_service_host.py",
            "tests/test_firmware_host_loadgate.py",
            "tests/test_triage_feature_parity.py",
            "tests/test_firmware_ppg_imu_host.py",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
    ),
    (
        "live path: validator, clock model gates, SOP end-to-end, wrap/soak",
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/test_validator_raw_path.py",
            "tests/test_validator_clock_gate.py",
            "tests/test_sop_end_to_end.py",
            "tests/test_live_wrap_timecompression.py",
            "tests/test_research_ble_client.py",
            "tests/test_raw_ingest.py",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
        ],
    ),
]


def main() -> int:
    failed: list[str] = []
    for label, cmd in CHECKS:
        print(f"\n=== {label} ===", flush=True)
        result = subprocess.run(cmd, cwd=ROOT, check=False)
        if result.returncode != 0:
            failed.append(label)

    print("\n" + "=" * 64)
    if failed:
        print("PRE-ACQUISITION GATE: DO NOT ORDER — the following gates failed:")
        for label in failed:
            print(f"  [FAIL] {label}")
        print("=" * 64)
        return 1
    print("PRE-ACQUISITION GATE: ORDER THE SMOKE KIT")
    print("  All software-side gates pass. The remaining work is physical:")
    print("  order per docs/hardware/PROCUREMENT.md (~EUR 140, one shipment),")
    print("  then follow docs/hardware/BRINGUP_SOP.md and record the bench")
    print("  numbers into data/live_validation/gate_report.json.")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
