#!/usr/bin/env python3
"""Phase 1 Entry Gate Validation Script.

Standalone script that runs the complete tier promotion cycle test
and outputs a JSON report for CI/CD gatekeeping.

Usage:
    python scripts/validate_phase1_entry.py [--output-dir DIR] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tests.test_tier_promotion_cycle import (
    MockClock,
    TierPromotionTestHarness,
    TierTransition,
)


def run_validation(output_dir: Path, verbose: bool = False) -> dict:
    """Run the Phase 1 entry gate validation."""
    if verbose:
        print("=" * 60)
        print("SYNAPSE-24 Phase 1 Entry Gate Validation")
        print("=" * 60)

    clock = MockClock(0.0)
    harness = TierPromotionTestHarness(clock)
    results = harness.run_promotion_cycle()

    # Determine pass/fail for each criterion
    promotion_latency_ms = results["promotion_latency_ms"]
    demotion_latency_ms = results["demotion_latency_ms"]

    criteria = {
        "promotion_occurred": {
            "passed": results["promotion_occurred"],
            "details": "Tier 0 → Tier 1 promotion on immobility",
        },
        "demotion_occurred": {
            "passed": results["demotion_occurred"],
            "details": "Tier 1 → Tier 0 demotion on movement",
        },
        "promotion_latency": {
            "passed": promotion_latency_ms is not None and promotion_latency_ms < 5000,
            "value_ms": promotion_latency_ms,
            "threshold_ms": 5000,
            "details": "Promotion latency < 5s (IMU trigger to T1 streams active)",
        },
        "demotion_latency": {
            "passed": demotion_latency_ms is not None and demotion_latency_ms < 2000,
            "value_ms": demotion_latency_ms,
            "threshold_ms": 2000,
            "details": "Demotion latency < 2s (movement onset to T0 only)",
        },
        "xdf_zero_drop": {
            "passed": results["xdf_proof"]["total_dropped"] == 0,
            "dropped": results["xdf_proof"]["total_dropped"],
            "expected": results["xdf_proof"]["total_expected"],
            "recovered": results["xdf_proof"]["total_recovered"],
            "details": "XDF round-trip zero sample loss",
        },
        "tier0_sync": {
            "passed": all(
                p["within_tolerance"]
                for p in results["tier0_sync_residuals"].get("pods", {}).values()
            ),
            "pods": {
                pid: {
                    "offset_ms": p["offset_ms"],
                    "tolerance_ms": p["tolerance_ms"],
                    "within": p["within_tolerance"],
                }
                for pid, p in results["tier0_sync_residuals"].get("pods", {}).items()
            },
            "details": "Tier 0 clock sync residual ≤ 10ms (p99)",
        },
        "tier1_sync": {
            "passed": all(
                p["within_tolerance"]
                for p in results["tier1_sync_residuals"].get("pods", {}).values()
            ),
            "pods": {
                pid: {
                    "offset_ms": p["offset_ms"],
                    "tolerance_ms": p["tolerance_ms"],
                    "within": p["within_tolerance"],
                }
                for pid, p in results["tier1_sync_residuals"].get("pods", {}).items()
            },
            "details": "Tier 1 clock sync residual ≤ 1ms (p99)",
        },
        "power_budget_24h": {
            "passed": results["power_budget"]["estimated_remaining_h"] >= 24.0,
            "estimated_remaining_h": results["power_budget"]["estimated_remaining_h"],
            "threshold_h": 24.0,
            "battery_remaining_mah": results["power_budget"]["battery_remaining_mah"],
            "details": "Projected battery life ≥ 24h on 3000 mAh",
        },
    }

    overall_pass = all(c["passed"] for c in criteria.values())

    report = {
        "schema_version": "1.0",
        "phase": "Phase 1 Entry Gate",
        "timestamp": time.time(),
        "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "overall_pass": overall_pass,
        "criteria": criteria,
        "summary": {
            "total_criteria": len(criteria),
            "passed": sum(1 for c in criteria.values() if c["passed"]),
            "failed": sum(1 for c in criteria.values() if not c["passed"]),
        },
        "transitions": results["transitions"],
        "stream_counts": results["stream_counts"],
        "power_budget": results["power_budget"],
    }

    # Write report
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "phase1_entry_gate_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    if verbose:
        print(f"\n{'Criterion':<30} {'Status':<8} {'Details'}")
        print("-" * 80)
        for name, criterion in criteria.items():
            status = "PASS" if criterion["passed"] else "FAIL"
            print(f"{name:<30} {status:<8} {criterion.get('details', '')}")

        print(f"\n{'=' * 60}")
        print(f"OVERALL: {'PASS' if overall_pass else 'FAIL'}")
        print(f"Passed: {report['summary']['passed']}/{report['summary']['total_criteria']}")
        print(f"Report: {report_path}")
        print(f"{'=' * 60}")

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

    args = parser.parse_args()

    try:
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
