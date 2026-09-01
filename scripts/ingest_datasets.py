#!/usr/bin/env python3
"""Main ingestion script for Phase 0 public datasets.

Downloads, processes, and validates WESAD and MIT-BIH datasets,
outputting XDF files with embedded signal quality metrics.
"""

from __future__ import annotations

import argparse
import contextlib
from pathlib import Path

from synapse24.ingestion import Tier, ingest_mitbih, ingest_wesad
from synapse24.utils import validate_xdf


def main():
    parser = argparse.ArgumentParser(description="SYNAPSE-24 Phase 0: Public Dataset Ingestion")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="Root data directory",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed"),
        help="Output directory for processed data",
    )
    parser.add_argument(
        "--dataset",
        choices=["wesad", "mitbih", "both"],
        default="both",
        help="Which dataset to process",
    )
    parser.add_argument(
        "--subjects",
        nargs="+",
        help="Specific WESAD subjects to process (e.g., S2 S3)",
    )
    parser.add_argument(
        "--records",
        nargs="+",
        help="Specific MIT-BIH records to process (e.g., 100 101)",
    )
    parser.add_argument(
        "--tier",
        type=int,
        choices=[0, 1, 2],
        default=1,
        help="Acquisition tier (0=continuous, 1=high-density, 2=calibration)",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    tier = Tier(args.tier)

    if args.dataset in ("wesad", "both"):
        print(f"Processing WESAD with Tier {tier.name} thresholds...")
        ingest_wesad(
            data_dir=args.data_dir / "wesad",
            output_dir=args.output_dir,
            subjects=args.subjects,
            tier=tier,
        )

    if args.dataset in ("mitbih", "both"):
        print(f"Processing MIT-BIH with Tier {tier.name} thresholds...")
        ingest_mitbih(
            data_dir=args.data_dir / "mitbih",
            output_dir=args.output_dir,
            records=args.records,
            tier=tier,
        )

    # Validate all XDF files
    print("Validating XDF outputs...")
    for xdf_file in args.output_dir.glob("*.xdf"):
        with contextlib.suppress(Exception):
            summary = validate_xdf(xdf_file)
            if not summary["validation"]["all_streams_valid"]:
                print(f"⚠️  Validation issues in {xdf_file.name}: {summary['validation']}")
            else:
                print(f"✅ {xdf_file.name} valid ({summary['n_streams']} streams)")


if __name__ == "__main__":
    main()
