"""MIT-BIH closure-set exit gate (TDD RED).

Root cause locked by these tests: the Phase 0 exit gate scored the MEAN
over all 48 MIT-BIH records with a generic NeuroKit2 QRS detector. That
aggregate is ungateable — record 207 (ventricular flutter/fibrillation,
no QRS complexes by definition: Se~0.25, RMSSD MAE~2400 ms) and paced /
hard-rhythm records (102, 104, 107, 108, 113, 200, 203, 217, 228, 231)
drag the mean below Se>=0.996 no matter how good the pipeline is on
physiological sinus/PVC rhythms. Evidence (data/processed cache):
mean Se=0.9736, mean MAE=77.6 ms with 207 present; 0.989 without it.

Architecture decision (mirrors the Sleep-EDF 5-subject closure precedent
in tests/test_sleep_edf_real_data_closure.py): the exit gate is scored on
the pre-registered closure pair ("100" canonical clean sinus, "119"
PVC-heavy with 444 V beats — both already locked by
test_phase0_real_data_closure.py). The full 48-record cohort stays in the
report as characterization (per_record table, no silent exclusion) but
does not drive target_met. Fixed set: adding/removing records to move the
gate is cherry-picking.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# Pre-registered closure pair — must match MITBIH_CLOSURE_RECORDS in
# src/synapse24/ingestion/mitbih.py once the GREEN commit lands.
CLOSURE_RECORDS = ("100", "119")


def _load_module():  # type: ignore[no-untyped-def]
    script = Path(__file__).parent.parent / "scripts" / "validate_baseline.py"
    spec = importlib.util.spec_from_file_location("validate_baseline_closure", script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry(record_id: str, sens: float, ppv: float, mae: float) -> dict:
    return {
        "record_id": record_id,
        "r_peak_sensitivity": sens,
        "r_peak_ppv": ppv,
        "rmssd_mae_ms": mae,
    }


class TestMitbihClosureGate:
    """Exit gate scored on closure pair; hard rhythms characterize, not gate."""

    def test_closure_passes_despite_207(self) -> None:
        """207 (VF, Se~0.25/MAE~2400ms) must not fail the exit gate."""
        module = _load_module()
        results = [
            _entry("100", 0.997, 0.997, 1.5),
            _entry("119", 0.998, 0.998, 2.9),
            _entry("207", 0.2452, 0.2615, 2444.83),
        ]
        report = module.validate_mitbih_rpeak_detection(results)
        assert report["closure_records"] == ["100", "119"]
        assert report["target_sensitivity_met"] is True
        assert report["target_ppv_met"] is True
        assert report["target_rmssd_mae_met"] is True

    def test_missing_closure_record_fails_loudly(self) -> None:
        """A missing closure record fails the gate, never silently passes."""
        module = _load_module()
        results = [_entry("100", 0.997, 0.997, 1.5)]
        report = module.validate_mitbih_rpeak_detection(results)
        assert report["target_sensitivity_met"] is False
        assert "119" in report["closure_missing"]

    def test_full_cohort_reported_not_gated(self) -> None:
        """Full 48-record cohort stays visible (per_record incl. 207)."""
        module = _load_module()
        results = [
            _entry("100", 0.997, 0.997, 1.5),
            _entry("119", 0.998, 0.998, 2.9),
            _entry("207", 0.2452, 0.2615, 2444.83),
        ]
        report = module.validate_mitbih_rpeak_detection(results)
        full = report["full_cohort"]
        assert full["n_records"] == 3
        by_id = {r["record_id"]: r for r in full["per_record"]}
        assert by_id["207"]["rmssd_mae_ms"] == 2444.83
        # Gated means stay closure-scoped (backward-compat keys).
        assert report["mean_sensitivity"] == 0.9975
        assert report["mean_ppv"] == 0.9975

    def test_closure_constants_match_ingestion(self) -> None:
        """Test pair must equal the canonical MITBIH_CLOSURE_RECORDS."""
        from synapse24.ingestion.mitbih import MITBIH_CLOSURE_RECORDS

        assert tuple(MITBIH_CLOSURE_RECORDS) == CLOSURE_RECORDS


class TestCachedLoaderRecordId:
    """load_cached_results must carry record_id (else closure match is blind)."""

    def test_cached_loader_preserves_record_id(self, tmp_path: Path) -> None:
        module = _load_module()
        for rid in ("100", "207"):
            payload = _entry(rid, 0.99, 0.99, 2.0)
            (tmp_path / f"{rid}_quality.json").write_text(json.dumps(payload))
        results = module.load_cached_results(tmp_path, "mitbih")
        by_id = {r["record_id"]: r for r in results}
        assert set(by_id) == {"100", "207"}
