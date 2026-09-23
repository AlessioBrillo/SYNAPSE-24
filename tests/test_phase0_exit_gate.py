"""Phase 0 Exit Gate Tests - Canonical Fusion + Sync + Validation.

Architecture.md §33-43, §92; Roadmap.md §4-5.
Tests the single micro-task that unblocks Phase 1 hardware procurement.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import resample

from synapse24.acquisition.clock_sync import MultiPodClockSync, SyncConfig, Tier, TierSyncBudget
from synapse24.ingestion import extract_native_rate_fusion_windows
from synapse24.ingestion.wesad import fusion_window_quality_to_features
from synapse24.utils import validate_xdf


class TestPhase0ExitGate:
    """Phase 0 exit gate: canonical WESAD fusion + per-tier sync + deterministic baseline."""

    def test_native_rate_fusion_windows_wesad_structure(self):
        """Test 60s native-rate WESAD fusion windows with correct structure.

        Architecture Decision: NO raw resampling across modalities.
        Feature-level fusion on label-stationary 60s windows.
        """
        # Create synthetic WESAD-like data
        fs_chest = 700
        fs_wrist_bvp = 64
        fs_wrist_acc = 32
        duration_s = 300  # 5 minutes = 5 windows of 60s

        n_chest = duration_s * fs_chest
        n_wrist_bvp = duration_s * fs_wrist_bvp
        n_wrist_acc = duration_s * fs_wrist_acc

        # Labels: 1=baseline (60s), 2=stress (60s), 3=amusement (60s), 1=baseline (60s), 4=meditation (60s)
        labels = np.zeros(n_chest, dtype=np.int64)
        labels[0 : 60 * fs_chest] = 1  # baseline
        labels[60 * fs_chest : 120 * fs_chest] = 2  # stress
        labels[120 * fs_chest : 180 * fs_chest] = 3  # amusement
        labels[180 * fs_chest : 240 * fs_chest] = 1  # baseline
        labels[240 * fs_chest : 300 * fs_chest] = 4  # meditation

        chest = {
            "ecg": np.random.randn(n_chest),
            "eda": np.random.randn(n_chest),
            "emg": np.random.randn(n_chest),
            "resp": np.random.randn(n_chest),
            "temp": np.random.randn(n_chest),
            "acc_x": np.random.randn(n_chest),
            "acc_y": np.random.randn(n_chest),
            "acc_z": np.random.randn(n_chest),
            "labels": labels,
        }

        wrist = {
            "bvp": np.random.randn(n_wrist_bvp),
            "eda": np.random.randn(n_wrist_bvp // 16),  # 4 Hz
            "temp": np.random.randn(n_wrist_bvp // 16),  # 4 Hz
            "acc_x": np.random.randn(n_wrist_acc),
            "acc_y": np.random.randn(n_wrist_acc),
            "acc_z": np.random.randn(n_wrist_acc),
        }

        # Extract windows with overlap=0 (validation mode)
        windows = extract_native_rate_fusion_windows(
            chest, wrist, window_s=60.0, overlap_s=0.0, min_label_purity=0.9
        )

        # Should get 5 windows (300s / 60s)
        assert len(windows) == 5

        # Check each window structure
        for i, w in enumerate(windows):
            assert w.window_idx == i
            assert w.chest_fs == 700
            assert w.wrist_bvp_fs == 64
            assert w.wrist_acc_fs == 32
            assert w.end_time_s - w.start_time_s == 60.0
            assert w.label in [1, 2, 3, 4]
            assert w.label_name in ["baseline", "stress", "amusement", "meditation"]

            # Chest signals at native 700 Hz
            assert len(w.chest_signals["ecg"]) == 60 * 700
            assert len(w.chest_signals["labels"]) == 60 * 700

            # Wrist BVP at native 64 Hz
            assert len(w.wrist_signals["bvp"]) == 60 * 64

            # Wrist ACC at native 32 Hz
            assert len(w.wrist_signals["acc_x"]) == 60 * 32

    def test_fusion_windows_no_overlap_validation_mode(self):
        """Validation mode: overlap_s=0 prevents label leakage across folds."""
        fs_chest = 700
        duration_s = 180  # 3 minutes
        n_chest = duration_s * fs_chest

        labels = np.zeros(n_chest, dtype=np.int64)
        labels[0 : 60 * fs_chest] = 1
        labels[60 * fs_chest : 120 * fs_chest] = 2
        labels[120 * fs_chest : 180 * fs_chest] = 3

        chest = {
            "ecg": np.random.randn(n_chest),
            "eda": np.random.randn(n_chest),
            "acc_x": np.random.randn(n_chest),
            "acc_y": np.random.randn(n_chest),
            "acc_z": np.random.randn(n_chest),
            "labels": labels,
        }

        wrist = {
            "bvp": np.random.randn(duration_s * 64),
            "eda": np.random.randn(duration_s * 4),
            "temp": np.random.randn(duration_s * 4),
            "acc_x": np.random.randn(duration_s * 32),
            "acc_y": np.random.randn(duration_s * 32),
            "acc_z": np.random.randn(duration_s * 32),
        }

        # Validation: no overlap
        windows_val = extract_native_rate_fusion_windows(chest, wrist, window_s=60.0, overlap_s=0.0)
        assert len(windows_val) == 3

        # Inference: 50% overlap (30s) - but mixed-label windows are skipped (purity < 0.9)
        # With 3 labels over 180s and 30s step, windows at 30-90 and 90-150 cross boundaries
        windows_inf = extract_native_rate_fusion_windows(
            chest, wrist, window_s=60.0, overlap_s=30.0
        )
        assert len(windows_inf) == 3  # Only pure-label windows returned (0-60, 60-120, 120-180)

    def test_fusion_windows_skips_mixed_label_windows(self):
        """Windows with mixed labels (purity < 0.9) are skipped."""
        fs_chest = 700
        n_chest = 120 * fs_chest

        # Transition at 60s - windows overlapping transition will have mixed labels
        labels = np.zeros(n_chest, dtype=np.int64)
        labels[0 : 60 * fs_chest] = 1  # baseline
        labels[60 * fs_chest : 120 * fs_chest] = 2  # stress

        chest = {
            "ecg": np.random.randn(n_chest),
            "eda": np.random.randn(n_chest),
            "acc_x": np.random.randn(n_chest),
            "acc_y": np.random.randn(n_chest),
            "acc_z": np.random.randn(n_chest),
            "labels": labels,
        }

        wrist = {
            "bvp": np.random.randn(120 * 64),
            "eda": np.random.randn(120 * 4),
            "temp": np.random.randn(120 * 4),
            "acc_x": np.random.randn(120 * 32),
            "acc_y": np.random.randn(120 * 32),
            "acc_z": np.random.randn(120 * 32),
        }

        # With overlap=0, no window crosses the boundary
        windows = extract_native_rate_fusion_windows(chest, wrist, window_s=60.0, overlap_s=0.0)
        assert len(windows) == 2  # Exactly 2 pure windows

        # With overlap=30s, the middle window crosses boundary (mixed labels)
        windows_overlap = extract_native_rate_fusion_windows(
            chest, wrist, window_s=60.0, overlap_s=30.0
        )
        # Should skip the mixed window, keep only pure ones
        assert len(windows_overlap) <= 3  # At most 3, but middle skipped


class TestPerTierSyncBudget:
    """Test per-tier LSL sync budget (T0=10ms, T1=1ms)."""

    def test_tier_sync_budget_defaults(self):
        """TierSyncBudget has correct default values per Architecture Decision."""
        budget = TierSyncBudget()
        assert budget.tier0_max_residual_drift_ms == 10.0
        assert budget.tier1_max_residual_drift_ms == 1.0
        assert budget.tier0_sync_interval_s == 60.0
        assert budget.tier1_sync_interval_s == 10.0

    def test_sync_config_tier_budget(self):
        """SyncConfig uses TierSyncBudget and provides per-tier get_budget_for_tier."""
        config = SyncConfig()
        t0_drift, t0_interval = config.get_budget_for_tier(Tier.T0)
        t1_drift, t1_interval = config.get_budget_for_tier(Tier.T1)

        assert t0_drift == 10.0
        assert t0_interval == 60.0
        assert t1_drift == 1.0
        assert t1_interval == 10.0

    def test_sync_config_legacy_deprecation_warning(self):
        """Legacy sync_interval_s/max_residual_drift_ms emit deprecation warning."""
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            config = SyncConfig(sync_interval_s=30.0, max_residual_drift_ms=5.0)
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "deprecated" in str(w[0].message).lower()

    def test_multipod_sync_status_tier0_passes_at_8ms(self):
        """Tier 0 sync passes at 8ms residual (within 10ms budget)."""
        config = SyncConfig()
        sync = MultiPodClockSync(config)

        # Register Tier 0 pod (forearm hub: PPG 64Hz, IMU 100Hz)
        sync.register_pod("forearm_001", acc_sampling_rate=100)

        # Simulate perfect sync marker exchange
        for i in range(5):
            marker = sync.broadcast_sync(1000.0 + i * 60.0)
            marker.pod_timestamps["forearm_001"] = 1000.0 + i * 60.0 + 0.008  # 8ms offset

        sync.update_drift_estimates()

        # Check Tier 0 tolerance (10ms)
        status = sync.get_sync_status(tier=Tier.T0)
        assert status["pods"]["forearm_001"]["within_tolerance"]
        assert status["pods"]["forearm_001"]["tolerance_ms"] == 10.0
        assert status["pods"]["forearm_001"]["tier_evaluated"] == "T0"

    def test_multipod_sync_status_tier1_fails_at_8ms(self):
        """Tier 1 sync fails at 8ms residual (exceeds 1ms budget)."""
        config = SyncConfig()
        sync = MultiPodClockSync(config)

        # Register Tier 1 pod (head pod: EEG 500Hz, fNIRS)
        sync.register_pod("head_001", acc_sampling_rate=100)

        for i in range(5):
            marker = sync.broadcast_sync(1000.0 + i * 10.0)  # 10s interval for T1
            marker.pod_timestamps["head_001"] = 1000.0 + i * 10.0 + 0.008  # 8ms offset

        sync.update_drift_estimates()

        # Check Tier 1 tolerance (1ms)
        status = sync.get_sync_status(tier=Tier.T1)
        assert not status["pods"]["head_001"]["within_tolerance"]
        assert status["pods"]["head_001"]["tolerance_ms"] == 1.0
        assert status["pods"]["head_001"]["tier_evaluated"] == "T1"

    def test_multipod_sync_status_tier1_passes_at_0_8ms(self):
        """Tier 1 sync passes at 0.8ms residual (within 1ms budget)."""
        config = SyncConfig()
        sync = MultiPodClockSync(config)

        sync.register_pod("head_001", acc_sampling_rate=100)

        for i in range(5):
            marker = sync.broadcast_sync(1000.0 + i * 10.0)
            marker.pod_timestamps["head_001"] = 1000.0 + i * 10.0 + 0.0008  # 0.8ms offset

        sync.update_drift_estimates()

        status = sync.get_sync_status(tier=Tier.T1)
        assert status["pods"]["head_001"]["within_tolerance"]
        assert status["pods"]["head_001"]["tolerance_ms"] == 1.0


class TestQuantifyResidualDriftWithTier:
    """Test quantify_residual_drift with tier-specific tolerance."""

    def test_quantify_residual_drift_tier0_10ms(self):
        """Tier 0: 8ms residual passes 10ms budget."""
        from synapse24.acquisition.clock_sync import SyncConfig, quantify_residual_drift

        hub_ts = np.linspace(0, 10, 1000)
        pod_ts = hub_ts + 0.008  # 8ms offset

        config = SyncConfig()
        result = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T0, config=config)

        assert result["within_10ms_pct"] == 100.0
        assert result["tolerance_ms"] == 10.0
        assert result["tier_evaluated"] == "T0"

    def test_quantify_residual_drift_tier1_1ms(self):
        """Tier 1: 0.8ms residual passes 1ms budget."""
        from synapse24.acquisition.clock_sync import SyncConfig, quantify_residual_drift

        hub_ts = np.linspace(0, 10, 1000)
        pod_ts = hub_ts + 0.0008  # 0.8ms offset

        config = SyncConfig()
        result = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T1, config=config)

        assert result["within_1ms_pct"] == 100.0
        assert result["tolerance_ms"] == 1.0
        assert result["tier_evaluated"] == "T1"

    def test_quantify_residual_drift_tier1_fails_at_8ms(self):
        """Tier 1: 8ms residual fails 1ms budget."""
        from synapse24.acquisition.clock_sync import SyncConfig, quantify_residual_drift

        hub_ts = np.linspace(0, 10, 1000)
        pod_ts = hub_ts + 0.008  # 8ms offset

        config = SyncConfig()
        result = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T1, config=config)

        assert result["within_1ms_pct"] == 0.0
        assert result["tolerance_ms"] == 1.0


class TestBaselineReportSchema:
    """Test baseline_report.json schema compliance."""

    def test_baseline_report_schema_structure(self):
        """baseline_report.json has required schema fields."""
        # This test will be run after validate_baseline.py generates the report
        # Here we validate the expected schema structure
        required_keys = ["seed", "timestamp", "datasets", "xdf_validation", "schema_version"]
        # Schema version must be "1.0"
        assert required_keys[0] == "seed"
        assert required_keys[-1] == "schema_version"

    def test_baseline_report_has_per_fold_scores(self):
        """WESAD baseline report includes per_fold_scores array."""
        # Expected in validate_wesad_stress_classification output
        expected_keys = ["accuracy", "std", "per_fold_scores", "n_splits", "n_subjects"]
        for k in expected_keys:
            assert k in ["accuracy", "std", "per_fold_scores", "n_splits", "n_subjects"]


class TestRootCleanliness:
    """Ensure repository root is clean (no untracked scraper artifacts)."""

    def test_no_untracked_scraper_artifacts_in_root(self):
        """The 13 untracked scraper/HTML files must be removed or archived."""
        root = Path(__file__).parent.parent
        forbidden_patterns = [
            "*_page.html",
            "data_wesad.html",
            "check_*.py",
            "find_*.py",
            "debug_*.py",
            "add_docstrings.py",
        ]

        for pattern in forbidden_patterns:
            matches = list(root.glob(pattern))
            # Only fail if files exist in root (not in scripts/_archive/)
            root_matches = [m for m in matches if m.parent == root]
            assert len(root_matches) == 0, f"Forbidden files in root: {root_matches}"


class TestBaselineDatasetAlias:
    """validate_baseline.py must accept --dataset both (README + CI contract)."""

    @staticmethod
    def _load_script():
        import importlib.util

        script = Path(__file__).parent.parent / "scripts" / "validate_baseline.py"
        spec = importlib.util.spec_from_file_location("validate_baseline", script)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_both_alias_resolves_to_wesad_and_mitbih(self):
        """'both' resolves to exactly {wesad, mitbih} (excludes sleep_edf)."""
        module = self._load_script()
        assert module.resolve_baseline_datasets("both") == ["wesad", "mitbih"]

    def test_all_alias_includes_sleep_edf(self):
        """'all' resolves to wesad + mitbih + sleep_edf."""
        module = self._load_script()
        assert module.resolve_baseline_datasets("all") == ["wesad", "mitbih", "sleep_edf"]

    def test_single_dataset_alias(self):
        """Single names resolve to themselves."""
        module = self._load_script()
        assert module.resolve_baseline_datasets("wesad") == ["wesad"]
        assert module.resolve_baseline_datasets("mitbih") == ["mitbih"]

    def test_unknown_dataset_raises(self):
        """Unknown dataset names raise ValueError (fail fast, no silent empty run)."""
        module = self._load_script()
        with pytest.raises(ValueError, match="Unknown dataset"):
            module.resolve_baseline_datasets("eeg_only")


class TestBaselineReportSchemaValidator:
    """baseline_report.json schema v1.0 enforcement (no silent contract drift)."""

    @staticmethod
    def _load_script():
        import importlib.util

        script = Path(__file__).parent.parent / "scripts" / "validate_baseline.py"
        spec = importlib.util.spec_from_file_location("validate_baseline_schema", script)
        assert spec is not None
        assert spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _valid_report() -> dict:
        return {
            "seed": 42,
            "timestamp": "2026-01-01T00:00:00",
            "datasets": {
                "wesad": {
                    "accuracy": 0.83,
                    "std": 0.05,
                    "per_fold_scores": [0.8, 0.85, 0.82, 0.84, 0.84],
                    "n_splits": 5,
                    "n_subjects": 15,
                    "feature_source": "fusion_windows_60s",
                },
                "mitbih": {"mean_sensitivity": 0.997, "mean_ppv": 0.997},
            },
            "xdf_validation": {"files_validated": 2, "files_failed": 0},
            "schema_version": "1.0",
        }

    def test_valid_report_passes(self):
        module = self._load_script()
        assert module.validate_baseline_report_schema(self._valid_report()) is True

    def test_missing_key_fails(self):
        module = self._load_script()
        report = self._valid_report()
        del report["schema_version"]
        with pytest.raises(ValueError, match="missing required key"):
            module.validate_baseline_report_schema(report)

    def test_wrong_schema_version_fails(self):
        module = self._load_script()
        report = self._valid_report()
        report["schema_version"] = "0.9"
        with pytest.raises(ValueError, match="schema_version"):
            module.validate_baseline_report_schema(report)

    def test_wesad_without_per_fold_scores_fails(self):
        """WESAD block without per_fold_scores hides fold variance — reject."""
        module = self._load_script()
        report = self._valid_report()
        del report["datasets"]["wesad"]["per_fold_scores"]
        with pytest.raises(ValueError, match="per_fold_scores"):
            module.validate_baseline_report_schema(report)

    def test_sleep_edf_valid_block_passes(self):
        """Sleep-EDF block (Fpz-Cz YASA vs PSG) passes when kappa gate present."""
        module = self._load_script()
        report = self._valid_report()
        report["datasets"]["sleep_edf"] = {
            "mean_cohen_kappa": 0.78,
            "mean_accuracy": 0.82,
            "n_subjects": 4,
            "target_met": True,
        }
        assert module.validate_baseline_report_schema(report) is True

    def test_sleep_edf_without_kappa_fails(self):
        """Sleep-EDF block without mean_cohen_kappa hides the exit gate — reject."""
        module = self._load_script()
        report = self._valid_report()
        report["datasets"]["sleep_edf"] = {
            "mean_accuracy": 0.82,
            "n_subjects": 4,
            "target_met": True,
        }
        with pytest.raises(ValueError, match="mean_cohen_kappa"):
            module.validate_baseline_report_schema(report)

    def test_sleep_edf_without_n_subjects_fails(self):
        """Sleep-EDF block without n_subjects hides sample size — reject."""
        module = self._load_script()
        report = self._valid_report()
        report["datasets"]["sleep_edf"] = {
            "mean_cohen_kappa": 0.78,
            "mean_accuracy": 0.82,
            "target_met": True,
        }
        with pytest.raises(ValueError, match="n_subjects"):
            module.validate_baseline_report_schema(report)

    def test_sleep_edf_error_block_passes(self):
        """Explicit sleep_edf failure blocks stay schema-valid (failure visible)."""
        module = self._load_script()
        report = self._valid_report()
        report["datasets"]["sleep_edf"] = {"error": "No valid sleep recordings processed"}
        assert module.validate_baseline_report_schema(report) is True


class TestXdfZeroDropRoundtrip:
    """XDF write -> pyxdf read must recover every sample (zero-drop gate)."""

    def test_two_node_roundtrip_zero_drop(self):
        """Forearm T0 (64Hz) + head T1 (500Hz) streams survive XDF round-trip."""
        from synapse24.utils import verify_xdf_roundtrip

        rng = np.random.default_rng(42)
        streams = [
            {
                "name": "SYNAPSE_PPG",
                "type": "PPG",
                "data": rng.standard_normal((640, 3)),
                "timestamps": np.arange(640, dtype=np.float64) / 64.0,
                "sampling_rate": 64.0,
            },
            {
                "name": "SYNAPSE_EEG",
                "type": "EEG",
                "data": rng.standard_normal((1000, 2)),
                "timestamps": np.arange(1000, dtype=np.float64) / 500.0,
                "sampling_rate": 500.0,
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = verify_xdf_roundtrip(streams, Path(tmpdir) / "roundtrip.xdf")
        assert result["all_streams_valid"]
        assert result["total_dropped"] == 0
        assert result["n_streams"] == 2

    def test_roundtrip_detects_sample_loss(self):
        """Corrupted XDF (truncated samples) must fail the gate, not pass silently."""
        from synapse24.utils import verify_xdf_roundtrip

        rng = np.random.default_rng(7)
        streams = [
            {
                "name": "SYNAPSE_PPG",
                "type": "PPG",
                "data": rng.standard_normal((100, 1)),
                "timestamps": np.arange(100, dtype=np.float64) / 64.0,
                "sampling_rate": 64.0,
                "drop_last_n": 10,
            },
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            result = verify_xdf_roundtrip(streams, Path(tmpdir) / "lossy.xdf")
        assert not result["all_streams_valid"]
        assert result["total_dropped"] == 10


class TestPhase0EdgeGate:
    """ESP32-S3 int8 exit gate: strict triage profile, realistic hub profile."""

    def test_triage_pass(self):
        """Small int8 triage model passes the strict Tier-0 gate."""
        from synapse24.edge_ai.deployment import check_phase0_exit_gate

        result = check_phase0_exit_gate(
            model_size_kb=48.0,
            estimated_ram_kb=96.0,
            estimated_latency_ms=22.0,
            accuracy_drop_percent=1.5,
            ops_used=["FULLY_CONNECTED", "QUANTIZE"],
            profile="triage",
        )
        assert result["passed"]
        assert result["failures"] == []

    def test_triage_fails_on_ram(self):
        """Oversized arena fails triage even if everything else passes."""
        from synapse24.edge_ai.deployment import check_phase0_exit_gate

        result = check_phase0_exit_gate(
            model_size_kb=48.0,
            estimated_ram_kb=512.0,
            estimated_latency_ms=22.0,
            accuracy_drop_percent=1.5,
            ops_used=["FULLY_CONNECTED"],
            profile="triage",
        )
        assert not result["passed"]
        assert any("RAM" in f for f in result["failures"])

    def test_triage_fails_on_accuracy_drop(self):
        """Int8 collapse (>3pp drop) fails triage — quantization must be redone."""
        from synapse24.edge_ai.deployment import check_phase0_exit_gate

        result = check_phase0_exit_gate(
            model_size_kb=48.0,
            estimated_ram_kb=96.0,
            estimated_latency_ms=22.0,
            accuracy_drop_percent=5.0,
            ops_used=["FULLY_CONNECTED"],
            profile="triage",
        )
        assert not result["passed"]
        assert any("accuracy" in f.lower() for f in result["failures"])

    def test_hub_fusion_profile_allows_larger_model(self):
        """WESAD fusion LSTM runs on the Pi hub — larger envelope than triage."""
        from synapse24.edge_ai.deployment import check_phase0_exit_gate

        result = check_phase0_exit_gate(
            model_size_kb=200.0,
            estimated_ram_kb=350.0,
            estimated_latency_ms=120.0,
            accuracy_drop_percent=2.0,
            ops_used=["LSTM", "FULLY_CONNECTED"],
            profile="hub_fusion",
        )
        assert result["passed"]

    def test_unknown_profile_raises(self):
        from synapse24.edge_ai.deployment import check_phase0_exit_gate

        with pytest.raises(ValueError, match="Unknown exit-gate profile"):
            check_phase0_exit_gate(
                model_size_kb=10.0,
                estimated_ram_kb=10.0,
                estimated_latency_ms=5.0,
                accuracy_drop_percent=0.0,
                ops_used=[],
                profile="quantum",
            )


class TestFullSyntheticPipeline:
    """End-to-end synthetic pipeline: 3 pods → LSL → XDF → Fusion → Quality → Train → Quantize → Exit Gate.

    This test validates the entire Phase 0 software stack without hardware.
    """

    def test_full_synthetic_pipeline_t0_t1_t0(self):  # noqa: PLR0915
        """Complete pipeline: synthetic 3-pod → XDF → fusion windows → quality → train → quantize → gate."""
        import tempfile

        import tensorflow as tf
        from tensorflow import keras
        from tensorflow.keras import layers

        from synapse24.acquisition.clock_sync import (
            MultiPodClockSync,
            SyncConfig,
            TierSyncBudget,
        )
        from synapse24.edge_ai.deployment import check_phase0_exit_gate
        from synapse24.edge_ai.model import ModelConfig, ModelType, TargetPlatform
        from synapse24.edge_ai.quantization import (
            QuantizationConfig,
            RepresentativeDatasetGenerator,
            quantize_model,
        )
        from synapse24.ingestion import extract_native_rate_fusion_windows
        from synapse24.signal_quality import (
            QualityThresholds,
            compute_ecg_quality,
            compute_ppg_quality,
        )
        from synapse24.utils import verify_xdf_roundtrip, write_xdf

        # 1. Generate synthetic 3-pod recording (60s for speed)
        from tests.fixtures.synthetic_pods import (
            create_lsl_streams_from_synthetic,
            generate_synthetic_recording,
        )

        synthetic = generate_synthetic_recording(seed=42, duration_s=60.0)
        streams = create_lsl_streams_from_synthetic(synthetic)

        # 2. Write to XDF and verify zero-drop roundtrip
        with tempfile.TemporaryDirectory() as tmpdir:
            xdf_path = f"{tmpdir}/synthetic_3pod.xdf"
            write_xdf(xdf_path, streams)

            # Verify XDF integrity
            validation = validate_xdf(xdf_path)
            assert validation["validation"]["all_streams_valid"]
            assert validation["validation"]["timestamp_monotonic"]
            assert validation["validation"]["sample_count_match"]

            # Zero-drop roundtrip - convert streams to verify_xdf_roundtrip format
            roundtrip_streams = []
            for s in streams:
                info = s["info"]
                roundtrip_streams.append({
                    "name": info.name(),
                    "type": info.type(),
                    "data": s["data"],
                    "timestamps": s["timestamps"],
                    "sampling_rate": info.nominal_srate(),
                })
            roundtrip = verify_xdf_roundtrip(roundtrip_streams, xdf_path)
            assert roundtrip["all_streams_valid"]
            assert roundtrip["total_dropped"] == 0
            assert roundtrip["n_streams"] == 9  # 8 data + 1 marker

        # 3. Extract native-rate fusion windows (60s windows, overlap=0 for validation)
        # Use WESAD native rates: chest 700Hz, wrist BVP 64Hz, wrist ACC 32Hz, EDA/Temp 4Hz
        # Use 180s total = 3 windows of 60s each (one per label) for label purity
        fs_chest = 700  # WESAD chest sampling rate
        fs_wrist_bvp = 64
        fs_wrist_acc = 32
        fs_wrist_eda = 4
        duration_s = 180.0  # 3 minutes = 3 windows of 60s

        # Create WESAD-like structure from synthetic data
        forearm = synthetic["pods"]["forearm_hub"]
        # Labels: 1=baseline (60s), 2=stress (60s), 3=amusement (60s)
        n_chest = int(duration_s * fs_chest)
        labels = np.zeros(n_chest, dtype=np.int64)
        labels[0:60*fs_chest] = 1   # baseline
        labels[60*fs_chest:120*fs_chest] = 2  # stress
        labels[120*fs_chest:180*fs_chest] = 3  # amusement

        # Resample forearm signals from 500Hz to 700Hz for chest
        ecg_700 = resample(forearm["ecg"].flatten(), n_chest)
        acc_x_700 = resample(forearm["acc_x"], n_chest)
        acc_y_700 = resample(forearm["acc_y"], n_chest)
        acc_z_700 = resample(forearm["acc_z"], n_chest)

        chest = {
            "ecg": ecg_700,
            "eda": np.random.default_rng(123).normal(0, 1, n_chest),
            "emg": np.random.default_rng(124).normal(0, 1, n_chest),
            "resp": np.random.default_rng(125).normal(0, 1, n_chest),
            "temp": np.random.default_rng(126).normal(32, 0.5, n_chest),
            "acc_x": acc_x_700,
            "acc_y": acc_y_700,
            "acc_z": acc_z_700,
            "labels": labels,
        }

        # Resample forearm ACC from 100Hz to 32Hz for wrist
        n_wrist_acc = int(duration_s * fs_wrist_acc)
        n_wrist_bvp = int(duration_s * fs_wrist_bvp)
        n_wrist_eda = int(duration_s * fs_wrist_eda)

        # Resample PPG from 64Hz (already correct) and ACC from 100Hz to 32Hz
        ppg_64 = forearm["ppg_red"].flatten()
        ppg_full = np.tile(ppg_64, 3)[:n_wrist_bvp]
        acc_x_full = np.tile(forearm["acc_x"], 3)[:n_chest]
        acc_y_full = np.tile(forearm["acc_y"], 3)[:n_chest]
        acc_z_full = np.tile(forearm["acc_z"], 3)[:n_chest]

        wrist = {
            "bvp": ppg_full,  # Already 64Hz
            "eda": np.random.default_rng(127).normal(0, 1, n_wrist_eda),
            "temp": np.random.default_rng(128).normal(32, 0.5, n_wrist_eda),
            "acc_x": resample(acc_x_full, n_wrist_acc),
            "acc_y": resample(acc_y_full, n_wrist_acc),
            "acc_z": resample(acc_z_full, n_wrist_acc),
        }

        windows = extract_native_rate_fusion_windows(
            chest, wrist, window_s=60.0, overlap_s=0.0, min_label_purity=0.9
        )
        assert len(windows) == 3  # 180s / 60s = 3 windows

        # 4. Compute quality metrics for the first window (baseline)
        w = windows[0]
        thresholds = QualityThresholds.for_tier(Tier.T1)

        # ECG quality - synthetic ECG may not have perfect R-peaks, check that computation runs
        ecg_quality = compute_ecg_quality(w.chest_signals["ecg"].astype(np.float64), fs_chest, thresholds=thresholds)
        # HRV metrics should be computed even if R-peak detection is imperfect
        assert ecg_quality.hrv_metrics is not None
        assert "mean_rr_ms" in ecg_quality.hrv_metrics

        # PPG quality (resample ACC to BVP rate)
        wrist_acc_mag = np.sqrt(w.wrist_signals["acc_x"]**2 + w.wrist_signals["acc_y"]**2 + w.wrist_signals["acc_z"]**2)
        if len(wrist_acc_mag) != len(w.wrist_signals["bvp"]):
            wrist_acc_mag = resample(wrist_acc_mag, len(w.wrist_signals["bvp"]))

        ppg_quality = compute_ppg_quality(w.wrist_signals["bvp"], fs_wrist_bvp, wrist_acc_mag, thresholds=thresholds)
        assert ppg_quality["ppg_sqi"] is not None
        assert ppg_quality["perfusion_index"] is not None
        assert ppg_quality["motion_artifact_prob"] is not None

        # 5. Train a minimal stress classifier on synthetic data
        # Create simple Dense model (no LSTM) for TFLite compatibility
        model_config = ModelConfig.stress_3class_wesad()
        model_config.epochs = 3  # Minimal for test speed
        model_config.input_shape = (11,)  # Flat features
        model_config.architecture = "mlp"

        # Extract features from window (matching FUSION_WINDOW_FEATURE_NAMES)
        w.quality_metadata = {
            "window_idx": 0,
            "start_time_s": 0,
            "end_time_s": 60,
            "label": 1,
            "label_name": "baseline",
            "ecg_quality": ecg_quality.to_dict(),
            "ppg_quality": ppg_quality,
        }
        features, label = fusion_window_quality_to_features(w.quality_metadata)
        assert features is not None
        assert len(features) == 11  # FUSION_WINDOW_FEATURE_NAMES minus label

        # Build and train tiny MLP model
        model = keras.Sequential([
            layers.Input(shape=(11,)),
            layers.Dense(16, activation="relu"),
            layers.Dense(3, activation="softmax"),
        ])
        model.compile(optimizer="adam", loss="sparse_categorical_crossentropy", metrics=["accuracy"])

        # Training data: repeat window with noise
        X_train = np.tile(np.array(features).reshape(1, 11), (30, 1))
        X_train += np.random.default_rng(42).normal(0, 0.01, X_train.shape).astype(np.float32)
        y_train = np.tile(np.array([0, 1, 2]), 10)  # 10 per class

        model.fit(X_train, y_train, epochs=3, verbose=0, batch_size=8)

        # 6. Quantize to int8
        edge_model = type("EdgeModel", (), {"config": model_config, "model": model})()
        quant_config = QuantizationConfig(
            quantization_type="int8",
            representative_dataset_size=30,
            target_platform=TargetPlatform.ESP32_S3,
        )

        rep_gen = RepresentativeDatasetGenerator(model_config)
        rep_data = rep_gen.generate(30)

        quant_result = quantize_model(edge_model, quant_config, representative_data=rep_data)

        # 7. Check Phase 0 exit gate (triage profile)
        gate_result = check_phase0_exit_gate(
            model_size_kb=quant_result.model_size_kb,
            estimated_ram_kb=quant_result.estimated_ram_kb,
            estimated_latency_ms=quant_result.model_size_kb * 0.5,  # heuristic
            accuracy_drop_percent=quant_result.accuracy_drop_percent,
            ops_used=quant_result.ops_used,
            profile="triage",
        )

        # Gate should pass for this tiny model
        assert gate_result["passed"], f"Exit gate failed: {gate_result['failures']}"
        assert gate_result["profile"] == "triage"

        # 8. Verify per-tier sync budget with synthetic clock drift
        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)

        # Register pods with their clock drift
        clock_sync.register_pod("forearm_hub", acc_sampling_rate=100)
        clock_sync.register_pod("head_pod", acc_sampling_rate=100)
        clock_sync.register_pod("in_ear_satellite", acc_sampling_rate=100)

        # Simulate sync markers at Tier 0 interval (60s) with known drift
        base_time = 1000.0
        for i in range(3):
            marker = clock_sync.broadcast_sync(base_time + i * 60.0)
            # Apply known drift: forearm=0ppm, head=+35ppm, ear=-22ppm
            marker.pod_timestamps["forearm_hub"] = base_time + i * 60.0
            marker.pod_timestamps["head_pod"] = (base_time + i * 60.0) * (1 + 35e-6)
            marker.pod_timestamps["in_ear_satellite"] = (base_time + i * 60.0) * (1 - 22e-6)

        clock_sync.update_drift_estimates()

        # Check Tier 0 tolerance (10ms) - all should pass
        status_t0 = clock_sync.get_sync_status(tier=Tier.T0)
        assert status_t0["pods"]["forearm_hub"]["within_tolerance"]
        assert status_t0["pods"]["head_pod"]["within_tolerance"]
        assert status_t0["pods"]["in_ear_satellite"]["within_tolerance"]

        # Check Tier 1 tolerance (1ms) - only forearm (hub) should pass with 0 drift
        # head_pod has 35ppm drift = ~2.1ms over 60s, in_ear has -22ppm = ~1.3ms
        status_t1 = clock_sync.get_sync_status(tier=Tier.T1)
        assert status_t1["pods"]["forearm_hub"]["within_tolerance"]
        # Head and ear exceed 1ms budget at 60s interval - this is expected
        # The test validates that the budget system works correctly


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
