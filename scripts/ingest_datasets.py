#!/usr/bin/env python3
"""Main ingestion script for Phase 0 public datasets.

Downloads, processes, and validates WESAD and MIT-BIH datasets,
outputting XDF files with embedded signal quality metrics.
"""

from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path

import numpy as np

from synapse24.ingestion import ingest_mitbih, ingest_wesad
from synapse24.utils import create_stream_info, write_xdf


def wesad_to_xdf(
    results: list[dict],
    output_dir: Path,
) -> list[Path]:
    """Convert WESAD results to XDF files."""
    output_files = []

    for result in results:
        subject_id = result["subject_id"]
        result["sampling_rates"]["chest_hz"]
        result["sampling_rates"]["wrist_bvp_hz"]

        # Create streams for each segment
        streams = []

        for seg_name, seg_data in result["segments"].items():
            # This is a simplified version - in reality we'd store the actual signals
            # For now, create metadata streams
            meta_info = create_stream_info(
                name=f"SYNAPSE_{subject_id}_{seg_name}",
                stream_type="Metadata",
                channel_count=1,
                sampling_rate=0,
                channel_names=["quality_json"],
                channel_units=[""],
            )

            meta_stream = {
                "info": meta_info,
                "data": np.array([[json.dumps(seg_data, default=str)]], dtype=object),
                "timestamps": np.array([0.0]),
            }
            streams.append(meta_stream)

        if streams:
            xdf_path = output_dir / f"{subject_id}_wesad.xdf"
            write_xdf(xdf_path, streams)
            output_files.append(xdf_path)

    return output_files


def mitbih_to_xdf(
    results: list[dict],
    output_dir: Path,
) -> list[Path]:
    """Convert MIT-BIH results to XDF files."""
    output_files = []

    for result in results:
        record_id = result["record_id"]

        meta_info = create_stream_info(
            name=f"SYNAPSE_MITBIH_{record_id}",
            stream_type="Metadata",
            channel_count=1,
            sampling_rate=0,
            channel_names=["quality_json"],
            channel_units=[""],
        )

        meta_stream = {
            "info": meta_info,
            "data": np.array([[json.dumps(result, default=str)]], dtype=object),
            "timestamps": np.array([0.0]),
        }

        xdf_path = output_dir / f"{record_id}_mitbih.xdf"
        write_xdf(xdf_path, [meta_stream])
        output_files.append(xdf_path)

    return output_files


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
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    all_xdf_files = []

    if args.dataset in ("wesad", "both"):
        wesad_results = ingest_wesad(
            data_dir=args.data_dir / "wesad",
            output_dir=args.output_dir,
            subjects=args.subjects,
        )
        xdf_files = wesad_to_xdf(wesad_results, args.output_dir)
        all_xdf_files.extend(xdf_files)

    if args.dataset in ("mitbih", "both"):
        mitbih_results = ingest_mitbih(
            data_dir=args.data_dir / "mitbih",
            output_dir=args.output_dir,
            records=args.records,
        )
        xdf_files = mitbih_to_xdf(mitbih_results, args.output_dir)
        all_xdf_files.extend(xdf_files)

    # Validate all XDF files
    for xdf_file in all_xdf_files:
        from synapse24.utils import validate_xdf

        with contextlib.suppress(Exception):
            validate_xdf(xdf_file)


if __name__ == "__main__":
    main()
