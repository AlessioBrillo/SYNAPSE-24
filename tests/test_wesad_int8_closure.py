"""WESAD 11-feat int8 quantization closure (TF-free, Roadmap §4 step 3).

Architecture.md §49-53 (edge triage + fusion), §55-62 (energy); Roadmap.md
§4 (Edge Impulse → TFLM → ESP32 loop) + §5 Phase 0 exit.

Gap closed: fusion accuracy (≥80% GroupKFold) was proven, and the int8
exit-gate envelope (check_phase0_exit_gate) was proven, but never on the
SAME canonical 11-feat distribution (fusion_window_quality_to_features).
This gate ties them: FP32 GroupKFold on real 11-feat vectors → simulated
int8 calibration noise (per-feature round-trip, deterministic) → measured
accuracy drop in pp → triage/hub_fusion exit gate.

TF-free by design: TFLite conversion tests (test_edge_ai.py) skip without
TensorFlow. This closure runs in CI on sklearn + the surrogate contract
(SURROGATE_SEED=42, surrogate=True end-to-end). The real-data path is
skip-guarded on data/wesad/WESAD/*.pkl and stays the only non-surrogate
source of truth.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DATA_DIR = Path(__file__).parent.parent / "data"
WESAD_DIR = DATA_DIR / "wesad" / "WESAD"

requires_real_wesad = pytest.mark.skipif(
    not (WESAD_DIR.exists() and any(WESAD_DIR.glob("S*/S*.pkl"))),
    reason="Canonical WESAD not cached (manual download required)",
)


class TestClosureMatrixContract:
    """Surrogate results map to the canonical (n, 11) int8-closure matrix."""

    def test_matrix_shape_labels_groups_and_surrogate_flag(self) -> None:
        from synapse24.edge_ai.wesad_int8_closure import build_closure_matrix
        from synapse24.ingestion.wesad_surrogate import generate_surrogate_subject_results

        results = generate_surrogate_subject_results(n_subjects=6, windows_per_class=4)
        matrix = build_closure_matrix(results)

        assert matrix.X.shape == (6 * 3 * 4, 11)
        assert matrix.y.shape == (72,)
        assert set(int(v) for v in matrix.y) == {0, 1, 2}
        assert len(matrix.groups) == 72
        assert len(set(matrix.groups)) == 6
        assert matrix.all_surrogate is True
        assert matrix.feature_names == [
            "mean_rr_ms",
            "sdnn_ms",
            "rmssd_ms",
            "pnn50",
            "hr_mean_bpm",
            "lf_power",
            "hf_power",
            "lf_hf_ratio",
            "ppg_sqi",
            "perfusion_index",
            "motion_artifact_prob",
        ]

    def test_empty_results_raise(self) -> None:
        from synapse24.edge_ai.wesad_int8_closure import build_closure_matrix

        with pytest.raises(ValueError, match="No valid"):
            build_closure_matrix([])

    def test_non_three_class_windows_skipped(self) -> None:
        from synapse24.edge_ai.wesad_int8_closure import build_closure_matrix

        results = [
            {
                "subject_id": "S2",
                "surrogate": True,
                "fusion_windows": [
                    {
                        "label_name": "meditation",
                        "ecg_quality": {"metrics": {"ecg": {"hrv_metrics": {}}}},
                        "ppg_quality": {},
                    }
                ],
            }
        ]
        with pytest.raises(ValueError, match="No valid"):
            build_closure_matrix(results)


class TestSurrogateInt8ClosureGate:
    """Pipeline proof on surrogate: FP32 ≥80% + int8 drop ≤3pp + triage GREEN."""

    def test_groupkfold_fp32_ge_80_with_measured_int8_drop(self) -> None:
        from synapse24.edge_ai.wesad_int8_closure import run_wesad_int8_closure
        from synapse24.ingestion.wesad_surrogate import generate_surrogate_subject_results

        results = generate_surrogate_subject_results(n_subjects=6, windows_per_class=4)
        closure = run_wesad_int8_closure(results, profile="triage")

        assert closure["accuracy"] >= 0.80
        assert len(closure["per_fold_scores"]) == 3
        assert closure["accuracy_drop_pp"] <= 3.0
        assert closure["gate"]["passed"]
        assert closure["gate"]["failures"] == []
        assert closure["feature_source"] == "fusion_windows_60s_int8sim"
        assert closure["surrogate"] is True
        assert closure["n_subjects"] == 6

    def test_hub_fusion_profile_passes_same_closure(self) -> None:
        from synapse24.edge_ai.wesad_int8_closure import run_wesad_int8_closure
        from synapse24.ingestion.wesad_surrogate import generate_surrogate_subject_results

        results = generate_surrogate_subject_results(n_subjects=6, windows_per_class=4)
        closure = run_wesad_int8_closure(results, profile="hub_fusion")

        assert closure["gate"]["passed"]
        assert closure["gate"]["profile"] == "hub_fusion"

    def test_unknown_profile_raises(self) -> None:
        from synapse24.edge_ai.wesad_int8_closure import run_wesad_int8_closure
        from synapse24.ingestion.wesad_surrogate import generate_surrogate_subject_results

        results = generate_surrogate_subject_results(n_subjects=6, windows_per_class=4)
        with pytest.raises(ValueError, match="Unknown exit-gate profile"):
            run_wesad_int8_closure(results, profile="quantum")


class TestRealWesadInt8Closure:
    """Real-data promotion: same gate on cached canonical WESAD (skipped in CI)."""

    @requires_real_wesad
    def test_real_wesad_closure_passes_triage_gate(self) -> None:
        from synapse24.edge_ai.wesad_int8_closure import run_wesad_int8_closure
        from synapse24.ingestion.wesad import process_wesad_subject

        subject_dirs = sorted(WESAD_DIR.glob("S*/S*.pkl"))
        results = []
        for pkl in subject_dirs[:6]:
            subject_id = pkl.parent.name
            result = process_wesad_subject(subject_id, WESAD_DIR)
            result["surrogate"] = False
            results.append(result)

        closure = run_wesad_int8_closure(results, profile="triage")
        assert closure["surrogate"] is False
        assert closure["accuracy"] >= 0.80
        assert closure["accuracy_drop_pp"] <= 3.0
        assert closure["gate"]["passed"]
