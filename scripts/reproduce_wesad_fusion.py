#!/usr/bin/env python3
"""Reproduce the WESAD fusion baseline (canonical 60s windows, GroupKFold).

Architecture.md §51-53 + Roadmap.md §4: Schmidt et al. (ICMI 2018)
benchmark is 80% 3-class / 93% binary stress-vs-non-stress.

Behavior:
- If canonical real WESAD is cached (data/wesad/WESAD/S*/S*.pkl), process
  it via process_wesad_subject() and gate on REAL physiology
  (surrogate=False).
- Otherwise (UCI mirrors are dead in CI), fall back to the deterministic
  seeded surrogate (surrogate=True) so the pipeline — windows → 11 feats
  → GroupKFold → XDF zero-drop — stays provable end-to-end. The report
  ALWAYS carries the surrogate flag; a surrogate pass is never reported
  as real.

Outputs (output_dir):
- S*_quality.json per subject (with fusion_windows)
- wesad_fusion_report.json (validator output + provenance)
- S2_wesad_surrogate.xdf (surrogate path only, sync proof)

Usage:
    uv run python scripts/reproduce_wesad_fusion.py
    uv run python scripts/reproduce_wesad_fusion.py --n-subjects 6 --windows-per-class 6
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from synapse24.ingestion.wesad import WESAD_SUBJECTS  # noqa: E402
from synapse24.ingestion.wesad_surrogate import (  # noqa: E402
    SURROGATE_SEED,
    generate_surrogate_subject_results,
    write_surrogate_xdf,
)


def _has_real_wesad(data_dir: Path) -> bool:
    extract_dir = data_dir / "WESAD"
    return extract_dir.exists() and any(extract_dir.glob("S*/S*.pkl"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reproduce WESAD fusion baseline")
    parser.add_argument("--data-dir", type=Path, default=Path("data/wesad"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/processed"))
    parser.add_argument("--n-subjects", type=int, default=15)
    parser.add_argument("--windows-per-class", type=int, default=6)
    parser.add_argument("--seed", type=int, default=SURROGATE_SEED)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Defer heavy imports (sklearn) until needed.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "validate_baseline", Path(__file__).parent / "validate_baseline.py"
    )
    assert spec is not None
    assert spec.loader is not None
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)

    if _has_real_wesad(args.data_dir):
        from synapse24.ingestion.wesad import process_wesad_subject

        extract_dir = args.data_dir / "WESAD"
        results = []
        for sid in WESAD_SUBJECTS:
            if not (extract_dir / sid).exists():
                continue
            try:
                r = process_wesad_subject(sid, extract_dir, args.output_dir)
            except Exception as e:
                print(f"Warning: failed to process {sid}: {e}")
                continue
            results.append(r)
            with open(args.output_dir / f"{sid}_quality.json", "w") as f:
                json.dump(r, f, indent=2, default=str)
        provenance = "real"
    else:
        print("Canonical WESAD not cached — using deterministic surrogate (seed 42).")
        print("To gate on real physiology, manually download from")
        print("https://archive.ics.uci.edu/dataset/465 and place in data/wesad/WESAD/")
        results = generate_surrogate_subject_results(
            n_subjects=args.n_subjects,
            windows_per_class=args.windows_per_class,
            seed=args.seed,
        )
        for r in results:
            with open(args.output_dir / f"{r['subject_id']}_quality.json", "w") as f:
                json.dump(r, f, indent=2, default=str)
        write_surrogate_xdf(args.output_dir / "S2_wesad_surrogate.xdf", subject_id="S2")
        provenance = "surrogate"

    report = validator.validate_wesad_stress_classification(results)
    report["provenance"] = provenance
    report["seed"] = args.seed
    with open(args.output_dir / "wesad_fusion_report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    tag = "[SURROGATE]" if report.get("surrogate") else "[REAL]"
    print(
        f"WESAD 3-class {tag}: {report.get('accuracy', 0):.3f} "
        f"(target >=0.80) [{'PASS' if report.get('target_met') else 'FAIL'}] "
        f"n={report.get('n_samples', 0)} subjects={report.get('n_subjects', 0)} "
        f"source={report.get('feature_source', '?')}"
    )
    return 0 if report.get("target_met") else 1


if __name__ == "__main__":
    sys.exit(main())
