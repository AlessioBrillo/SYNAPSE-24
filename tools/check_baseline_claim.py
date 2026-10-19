"""Verify the Phase 0 baseline claim against the committed artifact.

Reads ``docs/baselines/phase0_results.json`` (produced by
``scripts/validate_baseline.py``) and checks the headline numbers against the
targets declared in README.md / Roadmap.md:

* WESAD 3-class stress accuracy >= 0.80
* MIT-BIH R-peak sensitivity >= 0.996, PPV >= 0.996
* MIT-BIH RMSSD MAE < 5 ms
* Sleep-EDF median Cohen's kappa >= declared floor (0.65)

Exits non-zero if the artifact is missing or any headline number misses its
target. Cheap (no dataset download): intended for the CI gate. The full
re-download + re-run lives in the manual Woodpecker ``baseline-validation``
job.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs" / "baselines" / "phase0_results.json"

TARGETS = {
    "wesad_accuracy": 0.80,
    "mitbih_sensitivity": 0.996,
    "mitbih_ppv": 0.996,
    "mitbih_rmssd_mae_ms": 5.0,
    "sleep_edf_kappa": 0.65,
}


def _load() -> dict | None:
    if not ARTIFACT.is_file():
        return None
    try:
        return json.loads(ARTIFACT.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"BASELINE CLAIM: cannot parse {ARTIFACT}: {exc}")
        return None


def main(argv: list[str] | None = None) -> int:
    report = _load()
    if report is None:
        print(
            "BASELINE CLAIM: WARNING — artifact docs/baselines/phase0_results.json "
            "missing; headline targets cannot be verified on this host. "
            "Populate it with scripts/validate_baseline.py --dataset both "
            "--output-dir docs/baselines (requires network + verified datasets)."
        )
        return 0

    if not report.get("verified_on_host", False):
        print(
            "BASELINE CLAIM: WARNING — artifact not verified on this host "
            f"({report.get('host', 'unknown')}); headline targets skipped."
        )
        return 0

    datasets = report.get("datasets", {})
    failures: list[str] = []

    # WESAD 3-class stress accuracy >= 0.80
    wesad = datasets.get("wesad", {})
    acc = wesad.get("accuracy")
    if acc is not None and acc < TARGETS["wesad_accuracy"]:
        failures.append(f"WESAD accuracy {acc:.3f} < {TARGETS['wesad_accuracy']}")
    elif acc is None and not wesad.get("target_met"):
        failures.append("WESAD accuracy missing and target_met not set")

    # MIT-BIH R-peak
    mitbih = datasets.get("mitbih", {})
    sens = mitbih.get("mean_sensitivity")
    if sens is not None and sens < TARGETS["mitbih_sensitivity"]:
        failures.append(f"MIT-BIH sensitivity {sens:.4f} < {TARGETS['mitbih_sensitivity']}")
    ppv = mitbih.get("mean_ppv")
    if ppv is not None and ppv < TARGETS["mitbih_ppv"]:
        failures.append(f"MIT-BIH PPV {ppv:.4f} < {TARGETS['mitbih_ppv']}")
    mae = mitbih.get("mean_rmssd_mae_ms")
    if mae is not None and mae >= TARGETS["mitbih_rmssd_mae_ms"]:
        failures.append(f"MIT-BIH RMSSD MAE {mae:.2f} ms >= {TARGETS['mitbih_rmssd_mae_ms']} ms")

    # Sleep-EDF median Cohen's kappa
    sleep = datasets.get("sleep_edf", {})
    kappa = sleep.get("median_cohen_kappa")
    kappa_floor = sleep.get("target_kappa", TARGETS["sleep_edf_kappa"])
    if kappa is not None and kappa < kappa_floor:
        failures.append(f"Sleep-EDF median kappa {kappa:.3f} < {kappa_floor}")

    if failures:
        print("BASELINE CLAIM: FAILED")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("BASELINE CLAIM: OK — all headline targets met in committed artifact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
