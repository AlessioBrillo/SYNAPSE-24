#!/usr/bin/env python3
"""Live Tier 0 validation: ESP32 (AD8232+MAX30102+ICM20948) -> LSL -> real-time quality gates."""

from __future__ import annotations

import argparse
from pathlib import Path

from synapse24.acquisition.live_validator import run_live_validation
from synapse24.signal_quality import Tier


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Tier 0 validation for SYNAPSE-24")
    parser.add_argument(
        "--ecg-stream",
        default="SYNAPSE_ECG_T0",
        help="LSL stream name for ECG",
    )
    parser.add_argument(
        "--ppg-stream",
        default="SYNAPSE_PPG_T0",
        help="LSL stream name for PPG",
    )
    parser.add_argument(
        "--acc-stream",
        default="SYNAPSE_ACC_T0",
        help="LSL stream name for ACC",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=300.0,
        help="Validation duration in seconds (default: 300)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/live_validation"),
        help="Output directory for XDF and JSON",
    )
    parser.add_argument(
        "--tier",
        type=int,
        default=0,
        choices=[0, 1, 2],
        help="Acquisition tier for quality thresholds (0=T0, 1=T1, 2=T2)",
    )
    parser.add_argument(
        "--list-streams",
        action="store_true",
        help="List available LSL streams and exit",
    )

    args = parser.parse_args()

    if args.list_streams:
        list_lsl_streams()
        return

    try:
        result = run_live_validation(
            ecg_stream=args.ecg_stream,
            ppg_stream=args.ppg_stream,
            acc_stream=args.acc_stream,
            duration_s=args.duration,
            output_dir=args.output_dir,
            tier=Tier(args.tier),
        )
        print("\n=== VALIDATION COMPLETE ===")
        print(f"Duration: {result['session_duration_s']:.1f}s")
        print(f"Quality assessments: {result['quality_assessments']}")
        if result["xdf_path"]:
            print(f"XDF: {result['xdf_path']}")
        if result["json_path"]:
            print(f"JSON: {result['json_path']}")
    except Exception as e:
        print(f"ERROR: {e}")
        raise


def list_lsl_streams() -> None:
    """List all available LSL streams."""
    try:
        from pylsl import resolve_streams
        streams = resolve_streams(timeout=5.0)
        if streams:
            print("Available LSL streams:")
            for s in streams:
                print(f"  {s.name()} ({s.type()}) - {s.channel_count()} ch @ {s.nominal_srate()} Hz, source: {s.source_id()}")
        else:
            print("No LSL streams found on network.")
    except Exception as e:
        print(f"Error resolving streams: {e}")


if __name__ == "__main__":
    main()
