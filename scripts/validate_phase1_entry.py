#!/usr/bin/env python3
"""Phase 1 Entry Gate Validation Script.

Standalone script that runs the complete tier promotion cycle test
and outputs a JSON report for CI/CD gatekeeping.

Usage:
    python scripts/validate_phase1_entry.py [--output-dir DIR] [--verbose]
    python scripts/validate_phase1_entry.py --live-hardware [--config CONFIG] [--flash] [--duration SEC]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# Add src and tests to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tests"))

from test_tier_promotion_cycle import (
    MockClock,
    TierPromotionTestHarness,
    TierTransition,
)


def run_validation(output_dir: Path, verbose: bool = False) -> dict:
    """Run the Phase 1 entry gate validation (synthetic data)."""
    if verbose:
        print("=" * 60)
        print("SYNAPSE-24 Phase 1 Entry Gate Validation (Synthetic)")
        print("=" * 60)

    clock = MockClock(0.0)
    harness = TierPromotionTestHarness(clock)
    results = harness.run_promotion_cycle()

    return _evaluate_results(results, output_dir, verbose, mode="synthetic")


async def run_live_validation(
    output_dir: Path,
    config_path: Path,
    flash: bool,
    duration: float | None,
    verbose: bool,
) -> dict:
    """Run the Phase 1 entry gate validation on live hardware."""
    if verbose:
        print("=" * 60)
        print("SYNAPSE-24 Phase 1 Entry Gate Validation (LIVE HARDWARE)")
        print("=" * 60)

    # Import hardware bringup
    sys.path.insert(0, str(Path(__file__).parent))
    from hardware_bringup import HardwareBringup

    bringup = HardwareBringup(config_path)
    results = await bringup.run(flash=flash, duration_s=duration)

    if not results.get("success"):
        # Create a failed report
        report = {
            "schema_version": "1.0",
            "phase": "Phase 1 Entry Gate (Live)",
            "timestamp": time.time(),
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "overall_pass": False,
            "criteria": {},
            "summary": {"total_criteria": 7, "passed": 0, "failed": 7},
            "error": results.get("error", "Unknown hardware error"),
            "mode": "live",
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "phase1_entry_gate_report.json"
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        return report

    return _evaluate_results(results, output_dir, verbose, mode="live")


def _evaluate_results(results: dict, output_dir: Path, verbose: bool, mode: str) -> dict:
    """Evaluate results against Phase 1 criteria and generate report."""
    promotion_transitions = [
        t for t in results.get("transitions", [])
        if t["transition"] == TierTransition.T0_TO_T1_IMMOBILITY.value
    ]
    demotion_transitions = [
        t for t in results.get("transitions", [])
        if t["transition"] == TierTransition.T1_TO_T0_MOVEMENT.value
    ]

    criteria = {
        "promotion_occurred": {
            "passed": len(promotion_transitions) >= 1,
            "details": "Tier 0 -> Tier 1 promotion on immobility",
        },
        "demotion_occurred": {
            "passed": len(demotion_transitions) >= 1,
            "details": "Tier 1 -> Tier 0 demotion on movement",
        },
        "final_tier_correct": {
            "passed": results.get("final_tier") == "T1",
            "details": "Final tier should be T1 after continuous immobility",
        },
        "xdf_zero_drop": {
            "passed": results.get("xdf_proof", {}).get("total_dropped", -1) == 0,
            "dropped": results.get("xdf_proof", {}).get("total_dropped", -1),
            "expected": results.get("xdf_proof", {}).get("total_expected", 0),
            "recovered": results.get("xdf_proof", {}).get("total_recovered", 0),
            "details": "XDF round-trip zero sample loss",
        },
        "tier0_sync": {
            "passed": all(
                p["within_tolerance"]
                for p in results.get("tier0_sync_residuals", {}).get("pods", {}).values()
            ),
            "pods": {
                pid: {
                    "offset_ms": p["offset_ms"],
                    "tolerance_ms": p["tolerance_ms"],
                    "within": p["within_tolerance"],
                }
                for pid, p in results.get("tier0_sync_residuals", {}).get("pods", {}).items()
            },
            "details": "Tier 0 clock sync residual <= 10ms (p99)",
        },
        "tier1_sync": {
            "passed": all(
                p["within_tolerance"]
                for p in results.get("tier1_sync_residuals", {}).get("pods", {}).values()
            ),
            "pods": {
                pid: {
                    "offset_ms": p["offset_ms"],
                    "tolerance_ms": p["tolerance_ms"],
                    "within": p["within_tolerance"],
                }
                for pid, p in results.get("tier1_sync_residuals", {}).get("pods", {}).items()
            },
            "details": "Tier 1 clock sync residual <= 1ms (p99)",
        },
        "power_budget_24h": {
            "passed": results.get("power_budget", {}).get("estimated_remaining_h", 0) >= 24.0,
            "estimated_remaining_h": results.get("power_budget", {}).get("estimated_remaining_h", 0),
            "threshold_h": 24.0,
            "battery_remaining_mah": results.get("power_budget", {}).get("battery_remaining_mah", 0),
            "details": "Projected battery life >= 24h on 3000 mAh",
        },
    }

    overall_pass = all(c["passed"] for c in criteria.values())

    report = {
        "schema_version": "1.0",
        "phase": f"Phase 1 Entry Gate ({mode.capitalize()})",
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall_pass": overall_pass,
        "criteria": criteria,
        "summary": {
            "total_criteria": len(criteria),
            "passed": sum(1 for c in criteria.values() if c["passed"]),
            "failed": sum(1 for c in criteria.values() if not c["passed"]),
        },
        "transitions": results.get("transitions", []),
        "stream_counts": results.get("stream_counts", {}),
        "power_budget": results.get("power_budget", {}),
        "mode": mode,
    }

    # Write report
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "phase1_entry_gate_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    if verbose:
        print("\nCriterion                      Status   Details")
        print("-" * 80)
        for name, criterion in criteria.items():
            status = "PASS" if criterion["passed"] else "FAIL"
            details = criterion.get("details", "").replace("\u2192", "->")
            print(f"{name:<30} {status:<8} {details}")

        print(f"\n{'='*60}")
        print(f"OVERALL: {'PASS' if overall_pass else 'FAIL'}")
        print(f"Passed: {report['summary']['passed']}/{report['summary']['total_criteria']}")
        print(f"Mode: {mode}")
        print(f"Report: {report_path}")
        print(f"{'='*60}")

    return report


def main():
    parser = argparse.ArgumentParser(description="SYNAPSE-24 Phase 1 Entry Gate Validation")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed"),
        help="Output directory for JSON report (default: data/processed)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Exit with non-zero code on any failure",
    )
    parser.add_argument(
        "--live-hardware",
        action="store_true",
        help="Run validation on live hardware via BLE (requires hardware_bringup.py)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/hardware_bringup.yaml"),
        help="Path to hardware bringup config (for --live-hardware)",
    )
    parser.add_argument(
        "--flash",
        action="store_true",
        help="Flash firmware before live validation (for --live-hardware)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Validation duration in seconds (for --live-hardware)",
    )

    args = parser.parse_args()

    try:
        if args.live_hardware:
            report = asyncio.run(run_live_validation(
                args.output_dir,
                args.config,
                args.flash,
                args.duration,
                args.verbose,
            ))
        else:
            report = run_validation(args.output_dir, args.verbose)

        if args.fail_fast and not report["overall_pass"]:
            sys.exit(1)

        sys.exit(0 if report["overall_pass"] else 1)

    except Exception as e:
        print(f"Validation failed with error: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        sys.exit(2)


if __name__ == "__main__":
    main()
