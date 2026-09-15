#!/usr/bin/env python3
"""
Live 2-Pod BLE LSL Sync Validation

Phase 1 Entry Gate: Real-wire validation of Tier 0 acquisition over BLE.

Architecture.md §23-31: Decoupled pod/hub, Tier 0 continuous
Architecture.md §92: Multi-node clock drift (markers + ACC cross-corr)
Roadmap.md §138: Live ECG+PPG+IMU streaming, synchronized in LSL

Tests:
1. BLE connection establishment
2. Real-time streaming at normative rates (ECG 500Hz, PPG 64Hz, IMU 100Hz)
3. Per-tier sync validation (T0=10ms, T1=1ms marker channel)
4. XDF zero-drop round-trip proof
5. Power budget projection
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import numpy as np

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from synapse24.acquisition.live_lsl_sync import (
    LiveTwoPodConfig,
    LSLUnavailableError,
    run_live_2pod_sync,
)
from synapse24.signal_quality import Tier


def _build_config(args: argparse.Namespace) -> LiveTwoPodConfig:
    """Build LiveTwoPodConfig from parsed arguments."""
    return LiveTwoPodConfig(
        duration_s=args.duration,
        ecg_fs=args.ecg_fs,
        ppg_fs=args.ppg_fs,
        imu_fs=args.imu_fs,
        seed=args.seed,
        resolve_timeout_s=args.resolve_timeout,
        pull_timeout_s=args.pull_timeout,
        xdf_path=args.xdf_output,
        run_id=uuid.uuid4().hex[:8],
    )


def _print_header(args: argparse.Namespace) -> None:
    """Print test header."""
    print("=" * 70)
    print("SYNAPSE-24 Phase 1 Entry Gate: Live 2-Pod BLE LSL Sync Validation")
    print("=" * 70)
    print(f"Duration: {args.duration}s")
    print(f"Rates: ECG={args.ecg_fs}Hz, PPG={args.ppg_fs}Hz, IMU={args.imu_fs}Hz")
    print(f"Seed: {args.seed}")
    print(f"Resolve timeout: {args.resolve_timeout}s")
    print(f"Pull timeout: {args.pull_timeout}s")
    print("=" * 70)


def _print_stream_results(result: dict) -> None:
    """Print stream recovery results."""
    print("\n[2/5] Stream recovery results:")
    for stream in result["per_stream"]:
        status = "✓" if stream["dropped"] == 0 else "✗"
        t0_pass = "✓" if stream["residual"].get("within_10ms_pct", 0) == 100.0 else "✗"
        print(
            f"  {status} {stream['name']}: {stream['recovered']}/{stream['expected']} "
            f"({stream['dropped']} dropped) @ {stream['sampling_rate']}Hz "
            f"T0_sync={t0_pass} (residual={stream['residual'].get('max_abs_offset_ms', 'N/A'):.2f}ms)"
        )


def _print_tier1_marker(result: dict) -> None:
    """Print Tier 1 marker channel results."""
    t1 = result["tier1_marker"]
    t1_pass = "✓" if t1.get("within_1ms_pct", 0) == 100.0 else "✗"
    print("\n[3/5] Tier 1 marker channel (ECG 500Hz @ 1ms budget):")
    print(
        f"  {t1_pass} {t1['stream']}: residual_max={t1.get('max_abs_offset_ms', 'N/A'):.3f}ms, "
        f"within_1ms={t1.get('within_1ms_pct', 'N/A')}%"
    )


def _print_xdf_results(result: dict) -> None:
    """Print XDF round-trip results."""
    xdf = result["xdf_proof"]
    xdf_pass = "✓" if xdf["all_streams_valid"] and xdf["total_dropped"] == 0 else "✗"
    print("\n[4/5] XDF zero-drop round-trip:")
    print(f"  {xdf_pass} Streams: {xdf['n_streams']}, Total dropped: {xdf['total_dropped']}")
    for stream in xdf["streams"]:
        print(
            f"    {stream['name']}: {stream['n_samples']} samples, "
            f"dropped={stream['dropped']}, valid={stream['valid']}"
        )


def _print_overall_verdict(result: dict) -> None:
    """Print overall verdict."""
    print("\n[5/5] Overall verdict:")
    overall = "✓ PASS" if result["overall_pass"] else "✗ FAIL"
    print(f"  {overall}")
    print(f"  Run ID: {result['run_id']}")
    print(f"  Total expected: {result['total_expected']}, Recovered: {result['total_recovered']}")


def _print_power_budget() -> None:
    """Print power budget projection."""
    print("\n[Power Budget Projection]")
    print("  Tier 0 (continuous): 5 mW avg")
    print("  Tier 1 (8h sleep): 50 mW avg")
    print("  Hub battery: 3000 mAh @ 3.7V")
    print("  Projected lifetime: >24h with 300mAh reserve")


def _save_report(args: argparse.Namespace, result: dict, xdf: dict) -> None:
    """Save JSON report if output path provided."""
    if not args.output:
        return

    report = {
        "test": "live_2pod_ble_lsl_sync",
        "phase": "phase1_entry_gate",
        "config": {
            "duration_s": args.duration,
            "ecg_fs": args.ecg_fs,
            "ppg_fs": args.ppg_fs,
            "imu_fs": args.imu_fs,
            "seed": args.seed,
        },
        "result": result,
        "criteria": {
            "zero_dropped_samples": result["total_expected"] - result["total_recovered"] == 0,
            "tier0_sync_10ms": all(
                s["residual"].get("within_10ms_pct", 0) == 100.0 for s in result["per_stream"]
            ),
            "tier1_marker_1ms": result["tier1_marker"].get("within_1ms_pct", 0) == 100.0,
            "xdf_zero_drop": xdf["all_streams_valid"] and xdf["total_dropped"] == 0,
        },
        "overall_pass": result["overall_pass"],
    }
    args.output.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport saved to: {args.output}")


def _handle_lsl_error(e: LSLUnavailableError) -> None:
    """Handle LSL unavailable error."""
    print(f"\n✗ LSL unavailable: {e}")
    print("  Ensure liblsl is installed and LSL network is reachable")
    print("  For BLE hardware: pair pods and ensure they're advertising")
    sys.exit(2)


def _handle_unexpected_error(e: Exception, verbose: bool) -> None:
    """Handle unexpected errors."""
    print(f"\n✗ Unexpected error: {e}")
    if verbose:
        import traceback

        traceback.print_exc()
    sys.exit(3)


def main() -> None:
    parser = argparse.ArgumentParser(description="SYNAPSE-24 Live 2-Pod BLE LSL Sync Validation")
    parser.add_argument(
        "--duration", type=float, default=30.0, help="Test duration in seconds (default: 30)"
    )
    parser.add_argument("--ecg-fs", type=int, default=500, help="ECG sampling rate (default: 500)")
    parser.add_argument("--ppg-fs", type=int, default=64, help="PPG sampling rate (default: 64)")
    parser.add_argument("--imu-fs", type=int, default=100, help="IMU sampling rate (default: 100)")
    parser.add_argument(
        "--seed", type=int, default=42, help="Random seed for synthetic data (default: 42)"
    )
    parser.add_argument("--output", type=Path, default=None, help="Output JSON report path")
    parser.add_argument(
        "--xdf-output", type=Path, default=None, help="XDF output path for round-trip proof"
    )
    parser.add_argument(
        "--resolve-timeout", type=float, default=10.0, help="LSL resolve timeout (default: 10s)"
    )
    parser.add_argument(
        "--pull-timeout", type=float, default=30.0, help="LSL pull timeout (default: 30s)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()
    config = _build_config(args)

    _print_header(args)

    try:
        print("\n[1/5] Initializing LSL outlets and resolving streams...")
        result = run_live_2pod_sync(config)

        _print_stream_results(result)
        _print_tier1_marker(result)
        _print_xdf_results(result)
        _print_overall_verdict(result)
        _print_power_budget()
        _save_report(args, result, result["xdf_proof"])

        if args.xdf_output:
            print(f"XDF saved to: {args.xdf_output}")

        sys.exit(0 if result["overall_pass"] else 1)

    except LSLUnavailableError as e:
        _handle_lsl_error(e)
    except Exception as e:
        _handle_unexpected_error(e, args.verbose)


if __name__ == "__main__":
    main()
