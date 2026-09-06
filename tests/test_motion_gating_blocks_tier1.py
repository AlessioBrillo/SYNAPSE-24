"""Motion-gated Tier0->Tier1 promotion (Architecture.md §74, §33-43).

Root cause locked by these tests: the Tier 0->Tier 1 promotion path was
open-loop on motion quality. AcquisitionController.update_imu() promoted on
immobility + power budget alone, and LiveTier0Validator._assess_ppg_quality()
hardcoded overall=False without ever inhibiting promotion. Since motion
artifact is the dominant EEG/fNIRS failure mode (Architecture.md §74),
high-motion Tier-1 sessions produced contaminated HRV/sleep features, and
the canonical fusion path scored every window regardless of SQI/MAP.

Gate contract (Principal Architect decision):
- Promotion requires ppg_sqi >= 0.5 AND motion_artifact_prob <= 0.5
  (QualityThresholds.for_tier(T0)) for 2 consecutive clean assessments.
- TransitionEvent.metadata carries {sqi, map, consecutive_clean}.
- Fusion windows with measured-bad quality are hard-rejected from the
  feature matrix with quarantine counts surfaced (fail-open on missing
  quality, fail-closed on measured contamination).
"""

from __future__ import annotations

import numpy as np

from synapse24.acquisition.immobility import ImmobilityDetector
from synapse24.acquisition.power_budget import PowerBudgetManager
from synapse24.acquisition.state_machine import AcquisitionController, MotionGateConfig
from synapse24.signal_quality import QualityThresholds, Tier


def _fast_detector() -> ImmobilityDetector:
    """Immobility detector that triggers after 3 still windows (6 samples)."""
    return ImmobilityDetector(
        accel_sampling_rate=10,
        window_duration_s=0.2,
        magnitude_threshold=0.02,
        min_immobility_min=0.01,  # 0.6 s -> 3 windows of 0.2 s
    )


def _rich_budget() -> PowerBudgetManager:
    """Power budget that can always afford Tier 1 (isolates the motion gate)."""
    return PowerBudgetManager(
        hub_capacity_mah=10000.0,
        target_lifetime_h=24.0,
    )


def _drive_immobility(controller: AcquisitionController, n_samples: int = 30) -> None:
    """Feed still IMU samples (magnitude 0.0 g) to reach the trigger."""
    for _ in range(n_samples):
        controller.update_imu(0.0)


class TestMotionGateConfig:
    """Gate defaults match the Tier-0 literature thresholds."""

    def test_defaults_match_tier0_thresholds(self) -> None:
        config = MotionGateConfig()
        t0 = QualityThresholds.for_tier(Tier.T0)
        assert config.sqi_min == t0.ppg_sqi_min == 0.5
        assert config.map_max == t0.map_max == 0.5
        assert config.required_consecutive_clean == 2


class TestCleanMotionPromotes:
    """Clean PPG (SQI high, MAP low) promotes after 2 consecutive windows."""

    def test_two_clean_windows_promote_with_metadata(self) -> None:
        controller = AcquisitionController(
            immobility_detector=_fast_detector(),
            power_budget=_rich_budget(),
        )
        controller.update_motion_quality(ppg_sqi=0.85, motion_artifact_prob=0.10)
        controller.update_motion_quality(ppg_sqi=0.82, motion_artifact_prob=0.12)
        _drive_immobility(controller)

        assert controller.state_machine.is_tier1()
        event = controller.state_machine.transition_history[-1]
        assert event.metadata["sqi"] == 0.82
        assert event.metadata["map"] == 0.12
        assert event.metadata["consecutive_clean"] == 2

    def test_single_clean_window_does_not_promote(self) -> None:
        """Hysteresis: one clean assessment is not enough (no flapping)."""
        controller = AcquisitionController(
            immobility_detector=_fast_detector(),
            power_budget=_rich_budget(),
        )
        controller.update_motion_quality(ppg_sqi=0.85, motion_artifact_prob=0.10)
        _drive_immobility(controller)

        assert controller.state_machine.is_tier0()


class TestHighMotionBlocks:
    """Contaminated PPG blocks Tier-1 promotion even when still + powered."""

    def test_high_map_blocks_promotion(self) -> None:
        controller = AcquisitionController(
            immobility_detector=_fast_detector(),
            power_budget=_rich_budget(),
        )
        controller.update_motion_quality(ppg_sqi=0.85, motion_artifact_prob=0.95)
        controller.update_motion_quality(ppg_sqi=0.80, motion_artifact_prob=0.90)
        _drive_immobility(controller)

        assert controller.state_machine.is_tier0()
        assert controller.state_machine.transition_history == []

    def test_low_sqi_blocks_promotion(self) -> None:
        controller = AcquisitionController(
            immobility_detector=_fast_detector(),
            power_budget=_rich_budget(),
        )
        controller.update_motion_quality(ppg_sqi=0.10, motion_artifact_prob=0.10)
        controller.update_motion_quality(ppg_sqi=0.12, motion_artifact_prob=0.10)
        _drive_immobility(controller)

        assert controller.state_machine.is_tier0()

    def test_inline_quality_kwargs_block_promotion(self) -> None:
        """Quality passed inline with IMU samples also gates promotion."""
        controller = AcquisitionController(
            immobility_detector=_fast_detector(),
            power_budget=_rich_budget(),
        )
        for _ in range(30):
            controller.update_imu(0.0, ppg_sqi=0.2, motion_artifact_prob=0.8)

        assert controller.state_machine.is_tier0()

    def test_recovery_after_contamination_promotes(self) -> None:
        """Clean streak after contamination resets and promotes (no latch-up)."""
        controller = AcquisitionController(
            immobility_detector=_fast_detector(),
            power_budget=_rich_budget(),
        )
        controller.update_motion_quality(ppg_sqi=0.1, motion_artifact_prob=0.9)
        controller.update_motion_quality(ppg_sqi=0.85, motion_artifact_prob=0.1)
        controller.update_motion_quality(ppg_sqi=0.86, motion_artifact_prob=0.1)
        _drive_immobility(controller)

        assert controller.state_machine.is_tier1()


class TestLegacyPathUnchanged:
    """Callers without motion quality keep the legacy immobility+power path."""

    def test_no_quality_args_preserves_promotion(self) -> None:
        controller = AcquisitionController(
            immobility_detector=_fast_detector(),
            power_budget=_rich_budget(),
        )
        _drive_immobility(controller)

        assert controller.state_machine.is_tier1()


class TestFusionWindowQualityQuarantine:
    """Contaminated fusion windows are rejected with visible counts."""

    def test_filter_keeps_clean_rejects_contaminated(self) -> None:
        from synapse24.ingestion.wesad import filter_fusion_windows_by_quality

        windows = [
            {"label_name": "baseline", "ppg_quality": {"ppg_sqi": 0.85, "motion_artifact_prob": 0.1}},
            {"label_name": "stress", "ppg_quality": {"ppg_sqi": 0.2, "motion_artifact_prob": 0.9}},
            {"label_name": "amusement"},  # unmeasured -> fail-open, kept
        ]
        kept, quarantine = filter_fusion_windows_by_quality(windows)

        assert len(kept) == 2
        assert quarantine["n_rejected_low_sqi"] == 1
        assert quarantine["n_rejected_high_map"] == 1
        assert quarantine["n_kept"] == 2

    def test_surrogate_windows_all_pass_gate(self) -> None:
        """Surrogate centroids (SQI 0.80+, MAP <= 0.15) survive the gate."""
        from synapse24.ingestion.wesad_surrogate import generate_surrogate_subject_results

        results = generate_surrogate_subject_results(n_subjects=2, windows_per_class=2)
        from synapse24.ingestion.wesad import filter_fusion_windows_by_quality

        for result in results:
            kept, quarantine = filter_fusion_windows_by_quality(result["fusion_windows"])
            assert len(kept) == len(result["fusion_windows"])
            assert quarantine["n_rejected_low_sqi"] == 0
            assert quarantine["n_rejected_high_map"] == 0


class TestLiveValidatorOverall:
    """Live PPG assessment reports a real overall verdict (no hardcoded False)."""

    def test_clean_ppg_assessment_passes(self) -> None:
        from synapse24.acquisition.live_validator import LiveTier0Validator

        validator = LiveTier0Validator()
        fs = 64
        t = np.arange(fs * 30, dtype=np.float64) / fs
        clean = (np.sin(2.0 * np.pi * 1.2 * t) + 2.0).astype(np.float64)
        validator._ppg_red_buffer = list(clean)
        validator._acc_mag_buffer = [1.0] * len(clean)
        validator._assess_ppg_quality(1000.0)

        assert len(validator._quality_results) == 1
        result = validator._quality_results[0]
        assert result["overall"] is True
        assert result["quality"]["ppg_sqi"] >= 0.5
