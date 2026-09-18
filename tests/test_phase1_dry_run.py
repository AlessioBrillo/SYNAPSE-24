"""Phase 1 Dry-Run Validation Test.

Validates that the hardware bringup configuration, LSL outlet creation,
clock sync setup, acquisition controller wiring, and XDF round-trip
verification all work correctly without requiring physical hardware.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

# Add project root to path for scripts imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.validate_phase1_entry import Phase1Validator


class TestPhase1DryRun:
    """Test Phase 1 dry-run validation."""

    def test_dry_run_passes(self):
        """Dry-run mode should validate configuration and pass all gates."""
        config_path = Path("config/hardware_bringup.yaml")
        assert config_path.exists(), "hardware_bringup.yaml must exist"

        # Run dry-run validation
        import asyncio
        import logging

        logger = logging.getLogger("test_phase1_dry_run")
        logger.setLevel(logging.INFO)

        validator = Phase1Validator(config_path, logger)

        # This should not raise and should return a passing report
        report = asyncio.run(validator.run(dry_run=True, duration_s=60))

        # Assertions
        assert report.overall_pass is True, f"Dry-run failed: {report.gates}"
        assert report.run_id == "dry-run"
        assert len(report.gates) == 1
        assert report.gates[0].name == "config_validation"
        assert report.gates[0].passed is True
        assert report.xdf_proof["all_streams_valid"] is True
        assert report.xdf_proof["total_dropped"] == 0
        assert report.sample_counts == {}
        assert report.transitions == []

    def test_lsl_outlets_created(self):
        """Dry-run should create 10 LSL outlets (5 T0 + 5 T1)."""
        config_path = Path("config/hardware_bringup.yaml")
        import logging

        logger = logging.getLogger("test_phase1_lsl")
        logger.setLevel(logging.INFO)

        validator = Phase1Validator(config_path, logger)
        validator._create_lsl_outlets()

        # Check 10 outlets created (5 Tier 0 + 5 Tier 1)
        assert len(validator.lsl_manager.outlets) == 10
        expected_streams = {
            "SYNAPSE_ECG_T0",
            "SYNAPSE_PPG_T0",
            "SYNAPSE_ACC_T0",
            "SYNAPSE_GYRO_T0",
            "SYNAPSE_MAG_T0",
            "SYNAPSE_EEG_T1",
            "SYNAPSE_ACC_T1",
            "SYNAPSE_GYRO_T1",
            "SYNAPSE_MAG_T1",
            "SYNAPSE_Markers",
        }
        assert set(validator.lsl_manager.outlets.keys()) == expected_streams

    def test_clock_sync_setup(self):
        """Dry-run should setup clock sync with correct tier budgets."""
        config_path = Path("config/hardware_bringup.yaml")
        import logging

        logger = logging.getLogger("test_phase1_clock")
        logger.setLevel(logging.INFO)

        validator = Phase1Validator(config_path, logger)
        validator._setup_clock_sync()

        # Check clock sync initialized
        assert validator.clock_sync is not None
        # Pods are tracked in config.acc_sampling_rates
        assert "forearm_hub" in validator.clock_sync.config.acc_sampling_rates

        # Check tier budgets
        t0_drift, t0_interval = validator.clock_sync.config.get_budget_for_tier(
            validator.clock_sync.config.tier_budget.__class__.__name__ == "TierSyncBudget"
            and hasattr(validator.clock_sync.config, "tier_budget")
        )
        # Just verify the sync config has the right tier budget structure
        assert validator.clock_sync.config.tier_budget.tier0_max_residual_drift_ms == 10.0
        assert validator.clock_sync.config.tier_budget.tier0_sync_interval_s == 60.0
        assert validator.clock_sync.config.tier_budget.tier1_max_residual_drift_ms == 1.0
        assert validator.clock_sync.config.tier_budget.tier1_sync_interval_s == 10.0

    def test_acquisition_controller_setup(self):
        """Dry-run should setup acquisition controller with all components."""
        config_path = Path("config/hardware_bringup.yaml")
        import logging

        logger = logging.getLogger("test_phase1_controller")
        logger.setLevel(logging.INFO)

        validator = Phase1Validator(config_path, logger)
        validator._setup_acquisition_controller()

        # Check controller initialized with all components
        assert validator.controller is not None
        assert validator.controller.immobility_detector is not None
        assert validator.controller.night_scheduler is not None
        assert validator.controller.power_budget is not None
        assert validator.controller.pod_coordinator is not None
        assert validator.controller.motion_gate is not None

        # Check motion gate config from hardware_bringup.yaml (sqi_min=0.3)
        assert validator.controller.motion_gate.sqi_min == 0.3
        assert validator.controller.motion_gate.map_max == 0.5
        assert validator.controller.motion_gate.required_consecutive_clean == 2

    def test_xdf_verification_structure(self):
        """Dry-run should produce valid XDF proof structure."""
        config_path = Path("config/hardware_bringup.yaml")
        import logging

        logger = logging.getLogger("test_phase1_xdf")
        logger.setLevel(logging.INFO)

        validator = Phase1Validator(config_path, logger)
        validator._create_lsl_outlets()

        # Create minimal stream data for XDF verification
        import numpy as np

        streams_for_xdf = [
            {
                "name": "SYNAPSE_ECG_T0",
                "type": "ECG",
                "data": np.random.randn(100, 1).astype(np.float64),
                "timestamps": np.arange(100, dtype=np.float64) / 500.0,
                "sampling_rate": 500.0,
                "tier": 0,
            },
            {
                "name": "SYNAPSE_PPG_T0",
                "type": "PPG",
                "data": np.random.randn(100, 2).astype(np.float64),
                "timestamps": np.arange(100, dtype=np.float64) / 64.0,
                "sampling_rate": 64.0,
                "tier": 0,
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            xdf_path = Path(tmpdir) / "test.xdf"
            from synapse24.utils import verify_xdf_roundtrip

            xdf_proof = verify_xdf_roundtrip(streams_for_xdf, xdf_path)

            assert xdf_proof["all_streams_valid"] is True
            assert xdf_proof["total_dropped"] == 0
            assert xdf_proof["n_streams"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
