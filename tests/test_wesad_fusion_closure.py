"""WESAD fusion closure gate: canonical 60s windows → GroupKFold ≥80%.

Architecture.md §51-53 + Roadmap.md §4 (Schmidt et al. ICMI 2018: 80%
3-class / 93% binary): the Phase 0 exit gate requires a reproducible
3-class stress classifier on 60s native-rate fusion windows
(FUSION_WINDOW_CONFIG, overlap_s=0, purity>=0.9, GroupKFold by subject).

Root cause locked by these tests: UCI WESAD mirrors are dead, so CI can
never download real WESAD. The pipeline must therefore be provable on a
deterministic, explicitly-flagged surrogate that emits the EXACT
quality_metadata dict shape consumed by fusion_window_quality_to_features(),
while the real-data path stays intact and skip-guarded when
data/wesad/WESAD/*.pkl is absent. A surrogate pass must NEVER be
misreported as real physiology (surrogate flag required end-to-end).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from tests.constants import (
    MIN_SUBJECTS_FOR_CLOSURE,
    SURROGATE_FUSION_N_FOLDS,
    SURROGATE_N_SUBJECTS,
    SURROGATE_SEED,
    SURROGATE_WINDOWS_PER_CLASS,
    SURROGATE_WINDOWS_PER_SUBJECT,
    WESAD_TARGET_ACCURACY,
    XDF_ACC_SAMPLES_32HZ_10S,
    XDF_DEFAULT_N_SAMPLES,
    XDF_PPG_SAMPLES_64HZ_10S,
)

DATA_DIR = Path(__file__).parent.parent / "data"
WESAD_DIR = DATA_DIR / "wesad" / "WESAD"

try:
    from synapse24.ingestion.wesad_surrogate import (
        SURROGATE_SEED as _SURROGATE_SEED,
    )
    from synapse24.ingestion.wesad_surrogate import (
        generate_surrogate_subject_results,
    )

    _SURROGATE_IMPORTABLE = True
except ImportError:
    _SURROGATE_IMPORTABLE = False

requires_real_wesad = pytest.mark.skipif(
    not (WESAD_DIR.exists() and any(WESAD_DIR.glob("S*/S*.pkl"))),
    reason="Canonical WESAD not cached (manual download required)",
)


class TestSurrogateFusionContract:
    """Surrogate must speak the exact canonical window contract."""

    def test_surrogate_module_importable(self) -> None:
        assert _SURROGATE_IMPORTABLE, "wesad_surrogate module missing (RED)"
        assert _SURROGATE_SEED == SURROGATE_SEED

    def test_surrogate_results_carries_fusion_windows(self) -> None:
        assert _SURROGATE_IMPORTABLE
        results = generate_surrogate_subject_results(
            n_subjects=SURROGATE_N_SUBJECTS, windows_per_class=SURROGATE_WINDOWS_PER_CLASS
        )
        assert len(results) == SURROGATE_N_SUBJECTS
        for r in results:
            assert r.get("surrogate") is True
            windows = r.get("fusion_windows", [])
            assert len(windows) == SURROGATE_WINDOWS_PER_SUBJECT
            labels = {w["label_name"] for w in windows}
            assert labels == {"baseline", "stress", "amusement"}

    def test_surrogate_windows_map_to_11_feat_vectors(self) -> None:
        assert _SURROGATE_IMPORTABLE
        from synapse24.ingestion.wesad import fusion_window_quality_to_features

        results = generate_surrogate_subject_results(n_subjects=2, windows_per_class=2)
        for r in results:
            for w in r["fusion_windows"]:
                parsed = fusion_window_quality_to_features(w)
                assert parsed is not None
                feats, _label_id = parsed
                assert len(feats) == 11


class TestSurrogateClosureGate:
    """Pipeline proof on surrogate: GroupKFold 3-class ≥80%, windows-sourced."""

    def _load_validation_module(self, module_name: str):
        script_path = Path(__file__).parent.parent / "scripts" / "validate_baseline.py"
        spec = importlib.util.spec_from_file_location(module_name, script_path)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_groupkfold_3class_ge_80_on_surrogate(self) -> None:
        assert _SURROGATE_IMPORTABLE
        module = self._load_validation_module("validate_baseline")

        results = generate_surrogate_subject_results(
            n_subjects=SURROGATE_N_SUBJECTS, windows_per_class=SURROGATE_WINDOWS_PER_CLASS
        )
        out = module.validate_wesad_stress_classification(results)
        assert out["feature_source"] == "fusion_windows_60s"
        assert out["n_samples"] == SURROGATE_N_SUBJECTS * SURROGATE_WINDOWS_PER_SUBJECT
        assert out["n_subjects"] == SURROGATE_N_SUBJECTS
        assert len(out["per_fold_scores"]) == SURROGATE_FUSION_N_FOLDS
        assert out["accuracy"] >= WESAD_TARGET_ACCURACY
        assert out.get("target_met") is True

    def test_surrogate_flag_survives_validation(self) -> None:
        assert _SURROGATE_IMPORTABLE
        module = self._load_validation_module("validate_baseline_sf")

        results = generate_surrogate_subject_results(
            n_subjects=SURROGATE_N_SUBJECTS, windows_per_class=SURROGATE_WINDOWS_PER_CLASS
        )
        out = module.validate_wesad_stress_classification(results)
        assert out.get("surrogate") is True


class TestSurrogateXdfProof:
    """Surrogate-seeded XDF must round-trip with zero drops (sync gate)."""

    def test_surrogate_xdf_zero_drop(self, tmp_path: Path) -> None:
        assert _SURROGATE_IMPORTABLE
        from synapse24.ingestion.wesad_surrogate import write_surrogate_xdf
        from synapse24.utils import validate_xdf

        out = tmp_path / "S2_wesad_surrogate.xdf"
        write_surrogate_xdf(out, subject_id="S2", seed=SURROGATE_SEED)
        assert out.read_bytes()[:4] == b"XDF:"
        summary = validate_xdf(out)
        assert summary["validation"]["all_streams_valid"]
        recovered = {s["name"]: int(s["n_samples"]) for s in summary["streams"]}
        assert sum("SURROGATE" in name for name in recovered) == 3
        assert recovered["SYNAPSE_ECG_SURROGATE_S2"] == XDF_DEFAULT_N_SAMPLES
        assert recovered["SYNAPSE_PPG_SURROGATE_S2"] == XDF_PPG_SAMPLES_64HZ_10S
        assert recovered["SYNAPSE_ACC_SURROGATE_S2"] == XDF_ACC_SAMPLES_32HZ_10S


@pytest.mark.baseline
class TestRealWesadClosure:
    """Real-data closure: runs only when canonical WESAD is manually cached."""

    @requires_real_wesad
    def test_real_wesad_3class_gate(self) -> None:
        from synapse24.ingestion.wesad import WESAD_SUBJECTS, extract_wesad_window_features

        module = self._load_validation_module("validate_baseline_real")

        from synapse24.ingestion.wesad import process_wesad_subject

        results = []
        for sid in WESAD_SUBJECTS:
            subject_dir = WESAD_DIR / sid
            if not subject_dir.exists():
                continue
            try:
                r = process_wesad_subject(sid, WESAD_DIR, WESAD_DIR / ".." / "processed")
            except Exception:
                continue
            if r.get("surrogate"):
                continue
            if extract_wesad_window_features(r) is not None:
                results.append(r)
        assert len(results) >= MIN_SUBJECTS_FOR_CLOSURE, (
            f"Need >={MIN_SUBJECTS_FOR_CLOSURE} real subjects for a GroupKFold gate"
        )
        out = module.validate_wesad_stress_classification(results)
        assert out["feature_source"] == "fusion_windows_60s"
        assert out.get("surrogate", False) is False
