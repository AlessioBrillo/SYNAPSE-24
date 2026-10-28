#!/usr/bin/env python3
"""Compare the Phase 0 baseline claim with what the docs assert.

The committed artifact (``docs/baselines/phase0_results.json``, produced by
``scripts/validate_baseline.py``) declares exactly one claim level, and every
document talking about the Phase 0 results must assert that level and no
other:

* ``verified``  -  the headline numbers were re-measured on this host. The
  artifact's numbers are checked against the targets (WESAD >=80%, MIT-BIH
  Se/PPV >=0.996, RMSSD MAE <5 ms, Sleep-EDF kappa >=0.65), documents may
  claim results, and ``verified_on_host`` must back the level.
* ``partial``  -  something was measured somewhere, but NOT re-verified on this
  host. Documents must present the targets as targets; asserting achieved
  results is a violation.
* ``not_run``  -  nothing exists to check (artifact missing or a pure
  placeholder). Same document rule as ``partial``.

The level lives in the artifact under ``claim_level`` (schema 1.1). Legacy
artifacts without the field get it inferred: ``verified_on_host`` truthy ->
verified, any real number -> partial, otherwise not_run.

The document scan is a tripwire over the claim shapes this tree has carried
("Exit Gate Results âœ… COMPLETE", "All exit criteria met", results tables with
an Achieved column and "âœ… PASS"). It is not a proof of honesty  -  a doc that
drops the marker words passes  -  which is why the target-level statements are
the only forms the checker recognizes as safe.

Exits 0 when the tree is consistent, 1 with the violations, 2 when the
artifact cannot be parsed. Cheap (no network, no datasets): intended for the
CI gate. The full re-download + re-run lives in the manual Woodpecker
``baseline-validation`` job.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs" / "baselines" / "phase0_results.json"
DEFAULT_DOCS = [ROOT / "Roadmap.md", ROOT / "README.md"]

VERIFIED = "verified"
PARTIAL = "partial"
NOT_RUN = "not_run"
DEFAULT_LEVEL = NOT_RUN
EXPECTED_SCHEMA = "1.1"

TARGETS = {
    "wesad_accuracy": 0.80,
    "mitbih_sensitivity": 0.996,
    "mitbih_ppv": 0.996,
    "mitbih_rmssd_mae_ms": 5.0,
    "sleep_edf_kappa": 0.65,
}

# Claim shapes a partial/not-run tree must not print.
_CLAIM_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"Phase 0\s+Exit Gate Results[^\n]*(?:\u2705|COMPLETE)"),
        "heading claims the exit gate is COMPLETE",
    ),
    (
        re.compile(r"Phase 0 Status[^\n]{0,60}COMPLETE"),
        "status line claims Phase 0 COMPLETE",
    ),
    (
        re.compile(r"All exit criteria met"),
        "text says every exit criterion is met",
    ),
    (
        re.compile(
            r"^\|[^|\n]*(?:WESAD|MIT-BIH|RMSSD|Sleep-EDF)[^|\n]*"
            r"\|[^|\n]*\|[^|\n]*\|\s*\u2705\s*PASS\s*\|",
            re.MULTILINE,
        ),
        "results-table row claims PASS through an Achieved/Status column",
    ),
)


def _artifact_level(report: dict) -> str:
    """Read the declared claim level, or infer it for legacy artifacts."""
    declared = report.get("claim_level")
    if declared in {VERIFIED, PARTIAL, NOT_RUN}:
        return str(declared)
    if report.get("verified_on_host", False):
        return VERIFIED
    datasets = report.get("datasets", {})
    if any(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in _flatten_numbers(datasets)
    ):
        return PARTIAL
    return DEFAULT_LEVEL


def _flatten_numbers(value: object) -> list[float]:
    """Every numeric leaf of a nested dict, for the 'any real number' test."""
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, dict):
        out: list[float] = []
        for item in value.values():
            out.extend(_flatten_numbers(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(_flatten_numbers(item))
        return out
    return []


def _load(path: Path) -> tuple[dict | None, str | None]:
    """Load the artifact. Returns (report, None) or (None, reason)."""
    if not path.is_file():
        return None, f"artifact {path.name} is missing"
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return None, f"cannot parse {path.name}: {exc}"
    if not isinstance(report, dict):
        return None, f"{path.name} must be a JSON object"
    return report, None


def _check_numbers(report: dict, fail: list[str]) -> None:
    """The verified level means the numbers themselves pass, not merely exist."""
    datasets = report.get("datasets", {}) if isinstance(report.get("datasets", {}), dict) else {}

    wesad = datasets.get("wesad", {}) if isinstance(datasets.get("wesad", {}), dict) else {}
    mitbih = datasets.get("mitbih", {}) if isinstance(datasets.get("mitbih", {}), dict) else {}
    sleep = datasets.get("sleep_edf", {}) if isinstance(datasets.get("sleep_edf", {}), dict) else {}

    checks = [
        ("WESAD accuracy", wesad.get("accuracy"), TARGETS["wesad_accuracy"], "ge"),
        (
            "MIT-BIH R-peak sensitivity",
            mitbih.get("mean_sensitivity"),
            TARGETS["mitbih_sensitivity"],
            "ge",
        ),
        ("MIT-BIH R-peak PPV", mitbih.get("mean_ppv"), TARGETS["mitbih_ppv"], "ge"),
        (
            "MIT-BIH RMSSD MAE",
            mitbih.get("mean_rmssd_mae_ms"),
            TARGETS["mitbih_rmssd_mae_ms"],
            "lt",
        ),
        (
            "Sleep-EDF median kappa",
            sleep.get("median_cohen_kappa"),
            TARGETS["sleep_edf_kappa"],
            "ge",
        ),
    ]
    for label, value, target, kind in checks:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            fail.append(f"verified claim but '{label}' is missing/broken in the artifact")
            continue
        ok = value >= target if kind == "ge" else value < target
        if not ok:
            want = f">= {target}" if kind == "ge" else f"< {target}"
            fail.append(f"verified claim but {label} = {value} ({want} required)")


def _scan_docs(docs: list[Path], violations: list[str]) -> None:
    """Flag any document that asserts achieved results."""
    for doc in docs:
        if not doc.is_file():
            violations.append(f"declared document is missing: {doc}")
            continue
        text = doc.read_text(encoding="utf-8")
        for pattern, reason in _CLAIM_PATTERNS:
            for match in pattern.finditer(text):
                lineno = text.count("\n", 0, match.start()) + 1
                violations.append(f"{doc.name}:{lineno}  -  {reason}")


def main(argv: list[str] | None = None) -> int:
    """Check the committed artifact against the documented claim."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--artifact",
        type=Path,
        default=ARTIFACT,
        help="Baseline artifact to check (default: docs/baselines/phase0_results.json)",
    )
    parser.add_argument(
        "--doc",
        type=Path,
        action="append",
        dest="docs",
        default=None,
        metavar="PATH",
        help="Document to scan; repeatable. Default: , ".join(str(d.name) for d in DEFAULT_DOCS),
    )
    args = parser.parse_args(argv)
    docs = args.docs if args.docs else list(DEFAULT_DOCS)

    report, error = _load(args.artifact)
    if error is not None:
        if "cannot parse" in error:
            print(f"BASELINE CLAIM: artifact corrupt  -  {error}")
            print("Fix or regenerate docs/baselines/phase0_results.json before trusting the claim.")
            return 2
        # Missing artifact: claim is not_run.
        report = {}
        level = NOT_RUN
        notes = [error]
    else:
        assert report is not None
        level = _artifact_level(report)
        notes = []
        schema = report.get("schema_version")
        if schema != EXPECTED_SCHEMA:
            notes.append(
                f"schema_version={schema!r} (expected {EXPECTED_SCHEMA}); run "
                "scripts/validate_baseline.py or migrate the artifact"
            )

    fail: list[str] = []

    if level == VERIFIED:
        if not report.get("verified_on_host", False):
            fail.append("claim_level says verified but verified_on_host is false")
        _check_numbers(report, fail)
    else:
        if report.get("verified_on_host", False):
            notes.append(
                "verified_on_host is true but claim_level says "
                f"{level}; level up the artifact or set the flag honestly"
            )
        _scan_docs(docs, fail)
        if level == NOT_RUN:
            numbers = (
                _flatten_numbers(report.get("datasets", {}))
                if isinstance(report.get("datasets", {}), dict)
                else []
            )
            if numbers:
                notes.append(
                    "artifact carries numbers under a not_run claim; set claim_level=partial/verified"
                )

    if fail:
        print(f"BASELINE CLAIM VIOLATIONS ({len(fail)}):")
        for item in fail:
            print(f"  - {item}")
        print()
        print(f"docs claim more than the artifact's claim_level={level} supports.")
        print("Rephrase the documents as targets (unverified), or run")
        print("scripts/validate_baseline.py --dataset both --output-dir docs/baselines")
        print("and set claim_level=verified only from a re-measured host run.")
        return 1

    for note in notes:
        print(f"BASELINE CLAIM: note ({level})  -  {note}")
    print(f"BASELINE CLAIM: {level}  -  documents and artifact are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
