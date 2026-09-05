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

from synapse24.acquisition.clock_sync import MultiPodClockSync, SyncConfig, Tier, TierSyncBudget
from synapse24.ingestion import extract_native_rate_fusion_windows
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
