"""Pins for tools/check_baseline_claim.py (the claim consistency checker).

The checker is the mechanism that stopped the roadmap from claiming Phase 0
results the committed artifact does not back. These tests pin the three-state
claim contract (verified / partial / not_run): a checker that cannot fail on a
telekiing document would let the next "✅ PASS" row sail through.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "tools"))

import check_baseline_claim as cbc  # noqa: E402

# A document that speaks of the targets as targets: the safe phrasing.
_DEMOTED_DOC = """\
## Phase 0 Exit Gate Results (targets, not re-verified here)

| Gate | Target |
|------|--------|
| **WESAD 3-class stress (GroupKFold CV)** | >=80% |
| **MIT-BIH R-peak Sensitivity** | >=99.6% |
| **RMSSD MAE** | <5 ms |

**Phase 0 Status**: **open** — nothing above counts as achieved until the
baseline artifact says verified.
"""

# The shapes an unverified tree must never print (the historical sin in one file).
_PASS_TABLE_ROW = "| **WESAD 3-class stress (GroupKFold CV)** | >=80% | >=80% | \u2705 PASS |"
_ASSERTIVE_DOC = f"""\
## Phase 0 Exit Gate Results \u2705 COMPLETE

| Gate | Target | Achieved | Status |
|------|--------|----------|--------|
{_PASS_TABLE_ROW}
| **MIT-BIH R-peak Sensitivity** | >=99.6% | >=99.6% | \u2705 PASS |
| **RMSSD MAE** | <5 ms | <5 ms | \u2705 PASS |
| **Sleep-EDF kappa** | floor | cleared | \u2705 PASS |
| **Test suite** | lint | green | \u2705 PASS |

**Phase 0 Status**: **COMPLETE** — All exit criteria met. Ready for Phase 1.
"""

_PASSING_NUMBERS = {
    "wesad": {"accuracy": 0.86, "target_met": True, "surrogate": None, "integrity_ok": True},
    "mitbih": {
        "mean_sensitivity": 0.997,
        "mean_ppv": 0.996,
        "mean_rmssd_mae_ms": 3.5,
        "target_sensitivity_met": True,
        "target_ppv_met": True,
        "target_rmssd_mae_met": True,
        "integrity_ok": True,
    },
    "sleep_edf": {
        "median_cohen_kappa": 0.67,
        "target_met": True,
        "target_kappa": 0.65,
        "integrity_ok": True,
    },
}


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _artifact(tmp_path: Path, **patch: object) -> str:
    """A one-off phase0_results.json with `patch` merged over the placeholder."""
    payload: dict = {
        "claim_level": "partial",
        "verified_on_host": False,
        "host": "test",
        "schema_version": cbc.EXPECTED_SCHEMA,
        "datasets": {
            "wesad": {
                "accuracy": None,
                "target_met": None,
                "surrogate": None,
                "integrity_ok": False,
            },
            "mitbih": {
                "mean_sensitivity": None,
                "mean_ppv": None,
                "mean_rmssd_mae_ms": None,
                "target_sensitivity_met": None,
                "target_ppv_met": None,
                "target_rmssd_mae_met": None,
                "integrity_ok": True,
            },
            "sleep_edf": {
                "median_cohen_kappa": None,
                "target_met": None,
                "target_kappa": 0.65,
                "integrity_ok": False,
            },
        },
        "xdf_validation": None,
    }
    payload.update(patch)
    return str(_write(tmp_path, "phase0_results.json", json.dumps(payload, indent=2)))


def _check(tmp_path: Path, artifact: str, doc: Path) -> int:
    return cbc.main(argv=["--artifact", artifact, "--doc", str(doc)])


def test_missing_artifact_with_demoted_docs_passes(tmp_path: Path) -> None:
    """No artifact at all: claim is not_run and target-level docs are fine."""
    doc = _write(tmp_path, "Roadmap.md", _DEMOTED_DOC)
    assert _check(tmp_path, str(tmp_path / "missing.json"), doc) == 0


def test_partial_with_assertive_doc_fails(tmp_path: Path, capsys) -> None:
    """Partial + a results-claiming document must name the file and the shape."""
    doc = _write(tmp_path, "Roadmap.md", _ASSERTIVE_DOC)
    code = _check(tmp_path, _artifact(tmp_path), doc)
    assert code == 1
    out = capsys.readouterr().out
    for needle in (
        "Roadmap.md:",
        "heading claims the exit gate is COMPLETE",
        "status line claims Phase 0 COMPLETE",
        "results-table row claims PASS",
    ):
        assert needle in out, f"missing from the report: {needle!r}\n{out}"


def test_partial_with_target_doc_passes(tmp_path: Path, capsys) -> None:
    """Partial + targets-only documents: consistent, print the level."""
    doc = _write(tmp_path, "Roadmap.md", _DEMOTED_DOC)
    assert _check(tmp_path, _artifact(tmp_path), doc) == 0
    assert "BASELINE CLAIM: partial" in capsys.readouterr().out


def test_verified_claim_allows_result_docs_but_checks_numbers(tmp_path: Path, capsys) -> None:
    """Verified + results docs is consistent ONLY with passing numbers."""
    doc = _write(tmp_path, "Roadmap.md", _ASSERTIVE_DOC)
    good = _artifact(
        tmp_path / "verified",
        claim_level="verified",
        verified_on_host=True,
        datasets=_PASSING_NUMBERS,
    )
    assert _check(tmp_path, good, doc) == 0
    assert "BASELINE CLAIM: verified" in capsys.readouterr().out


def test_verified_with_failing_number_is_a_violation(tmp_path: Path, capsys) -> None:
    """A verified claim with a number under its target fails on the number."""
    below = json.loads(json.dumps(_PASSING_NUMBERS))
    below["wesad"]["accuracy"] = 0.74
    doc = _write(tmp_path, "Roadmap.md", _DEMOTED_DOC)
    bad = _artifact(tmp_path, claim_level="verified", verified_on_host=True, datasets=below)
    assert _check(tmp_path, bad, doc) == 1
    assert "WESAD accuracy" in capsys.readouterr().out


def test_verified_without_numbers_is_a_violation(tmp_path: Path, capsys) -> None:
    """claim_level=verified with null metrics is a lie, not a pass."""
    doc = _write(tmp_path, "Roadmap.md", _DEMOTED_DOC)
    empty = _artifact(tmp_path, claim_level="verified", verified_on_host=True)
    assert _check(tmp_path, empty, doc) == 1
    assert "missing/broken" in capsys.readouterr().out


def test_verified_requires_the_host_flag(tmp_path: Path) -> None:
    """claim_level=verified while verified_on_host is false contradicts itself."""
    doc = _write(tmp_path, "Roadmap.md", _DEMOTED_DOC)
    artifact = _artifact(tmp_path, claim_level="verified", verified_on_host=False)
    assert _check(tmp_path, artifact, doc) == 1


def test_corrupt_artifact_is_exit_two(tmp_path: Path, capsys) -> None:
    """Unparseable artifact: code 2 and wording that says how to fix it."""
    doc = _write(tmp_path, "Roadmap.md", _DEMOTED_DOC)
    corrupt = _write(tmp_path, "phase0_results.json", "{not json")
    assert _check(tmp_path, str(corrupt), doc) == 2
    assert "artifact corrupt" in capsys.readouterr().out


def test_legacy_artifact_without_claim_level_is_inferred(tmp_path: Path, capsys) -> None:
    """Legacy 1.0 artifacts carry no claim_level: infer partial from numbers."""
    doc = _write(tmp_path, "Roadmap.md", _DEMOTED_DOC)
    legacy = Path(
        _artifact(tmp_path, datasets={"mitbih": {"mean_sensitivity": 0.997, "integrity_ok": True}})
    )
    payload = json.loads(legacy.read_text(encoding="utf-8"))
    payload.pop("claim_level")
    payload["schema_version"] = "1.0"
    legacy.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    code = _check(tmp_path, str(legacy), doc)
    assert code == 0
    printed = capsys.readouterr().out
    assert "BASELINE CLAIM: partial" in printed
    assert "schema_version='1.0'" in printed
