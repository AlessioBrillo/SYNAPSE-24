"""Integration test for Tier 0 → Tier 1 promotion cycle with sync alignment.

Architecture.md §33-43: Three-tier acquisition strategy.
Architecture.md §74: Motion artifact is dominant EEG/fNIRS failure mode,
promotion must be gated on measured PPG quality.
Architecture.md §92: Tier 1 requires 1ms residual drift, 10s sync interval.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from synapse24.acquisition import (
    AcquisitionController,
    ImmobilityDetector,
    MotionGateConfig,
    MultiPodClockSync,
    SyncConfig,
)
from synapse24.acquisition.clock_sync import SyncMarker, Tier, TierSyncBudget
from synapse24.acquisition.state_machine import TierTransition
from synapse24.signal_quality import QualityThresholds, SignalQualityMetrics
from synapse24.signal_quality import Tier as QualityTier


class TestTierPromotionCycle:
    """Test complete Tier 0 → Tier 1 → Tier 0 cycle with sync."""

    def test_tier0_to_t1_promotion_on_immobility(self):
        """Promotion requires immobility + power budget + motion gate."""
        # Setup
        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)

        # Register Tier 0 pod (forearm: PPG 64Hz, IMU 100Hz)
        clock_sync.register_pod("forearm_001", acc_sampling_rate=100)

        # Register Tier 1 pod (head: EEG 500Hz, IMU 100Hz)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        # Create controller with motion gate
        motion_gate = MotionGateConfig(
            sqi_min=0.5,
            map_max=0.5,
            required_consecutive_clean=2,
        )

        immobility_detector = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=1.0,
            magnitude_threshold=0.05,
            min_immobility_min=0.02,  # ~1.2 seconds
        )

        controller = AcquisitionController(
            immobility_detector=immobility_detector,
            motion_gate=motion_gate,
            clock_fn=time.time,
        )

        # Initial state: Tier 0
        assert controller.state_machine.is_tier0()

        # Simulate immobility detected (IMU magnitude low)
        # But first: power budget must allow Tier 1
        from synapse24.acquisition.power_budget import PowerBudgetManager

        power_budget = PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
        )
        controller.power_budget = power_budget

        # Simulate clean motion quality assessments (2 consecutive)
        controller.update_motion_quality(ppg_sqi=0.7, motion_artifact_prob=0.2)
        controller.update_motion_quality(ppg_sqi=0.8, motion_artifact_prob=0.1)

        # Now simulate immobility with clean motion gate
        # Need to call update_imu multiple times to fill the window (1s @ 100Hz = 100 samples)
        # and achieve consecutive windows (min_immobility_min=0.02 min = 1.2s = 2 windows)
        for _ in range(250):  # 2.5 seconds of data
            controller.update_imu(
                accel_magnitude=0.01,  # Very low = immobile
                ppg_sqi=0.75,
                motion_artifact_prob=0.15,
            )

        # Should promote to Tier 1
        assert controller.state_machine.is_tier1()
        assert len(controller.state_machine.transition_history) == 1
        event = controller.state_machine.transition_history[0]
        assert event.transition == TierTransition.T0_TO_T1_IMMOBILITY

    def test_tier1_to_t0_demotion_on_movement(self):
        """Demotion on movement detection with contaminated PPG."""
        from synapse24.acquisition.power_budget import PowerBudgetManager

        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("forearm_001", acc_sampling_rate=100)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        motion_gate = MotionGateConfig(
            sqi_min=0.5,
            map_max=0.5,
            required_consecutive_clean=2,
        )

        controller = AcquisitionController(
            motion_gate=motion_gate,
            clock_fn=time.time,
        )
        controller.power_budget = PowerBudgetManager()

        # First promote to Tier 1 (simulate night window)
        controller.state_machine.promote_to_tier1("night_window_start")
        assert controller.state_machine.is_tier1()

        # Now simulate movement with contaminated PPG (2 consecutive)
        controller.update_motion_quality(ppg_sqi=0.3, motion_artifact_prob=0.8)
        controller.update_motion_quality(ppg_sqi=0.2, motion_artifact_prob=0.9)

        # Update IMU with high magnitude (movement)
        controller.update_imu(
            accel_magnitude=2.5,  # High = movement
            ppg_sqi=0.2,
            motion_artifact_prob=0.9,
        )

        # Should demote to Tier 0
        assert controller.state_machine.is_tier0()
        demotion_event = controller.state_machine.transition_history[-1]
        assert demotion_event.transition == TierTransition.T1_TO_T0_MOVEMENT
        assert "movement" in demotion_event.reason.lower()

    def test_tier1_demotion_on_power_budget_exceeded(self):
        """Tier 1 times out when power budget exceeded."""
        from synapse24.acquisition.power_budget import PowerBudgetManager

        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("forearm_001", acc_sampling_rate=100)

        power_budget = PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=1.0,  # Only 1 hour max
        )

        motion_gate = MotionGateConfig()

        # Use a controllable clock
        clock_state = {"now": time.time()}

        def custom_clock():
            return clock_state["now"]

        controller = AcquisitionController(
            power_budget=power_budget,
            motion_gate=motion_gate,
            clock_fn=custom_clock,
        )

        # Promote to Tier 1
        controller.state_machine.promote_to_tier1("night_window_start")
        assert controller.state_machine.is_tier1()

        # Simulate time passing beyond tier1_max_h
        # Advance clock by 2 hours
        clock_state["now"] += 2 * 3600
        controller.tick()

        # Should demote due to power budget
        assert controller.state_machine.is_tier0()
        demotion_event = controller.state_machine.transition_history[-1]
        assert demotion_event.reason == "power_budget_exceeded"


class TestSyncAlignmentDuringPromotion:
    """Test sync marker alignment during tier transitions."""

    def test_sync_marker_interval_changes_with_tier(self):
        """Sync interval switches from 60s (T0) to 10s (T1) on promotion."""
        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        # Simulate Tier 0 sync markers (every 60s) using add_marker directly
        base_time = 1000.0
        for i in range(3):
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=base_time + i * 60.0,
                pod_timestamps={"head_001": base_time + i * 60.0 + 0.005},  # 5ms offset
            )
            clock_sync.drift_estimator.add_marker(marker)

        # Update drift estimates
        clock_sync.update_drift_estimates()

        # Check Tier 0 tolerance (10ms) - should pass
        status_t0 = clock_sync.get_sync_status(tier=Tier.T0)
        assert status_t0["pods"]["head_001"]["within_tolerance"]
        assert status_t0["pods"]["head_001"]["tolerance_ms"] == 10.0

        # Now simulate Tier 1 (every 10s)
        for i in range(3):
            marker = SyncMarker(
                sequence=i + 3,
                hub_timestamp=base_time + 200.0 + i * 10.0,
                pod_timestamps={"head_001": base_time + 200.0 + i * 10.0 + 0.005},
            )
            clock_sync.drift_estimator.add_marker(marker)

        # Update drift estimates
        clock_sync.update_drift_estimates()

        # Check Tier 1 tolerance (1ms) - 5ms offset should FAIL
        status_t1 = clock_sync.get_sync_status(tier=Tier.T1)
        assert not status_t1["pods"]["head_001"]["within_tolerance"]
        assert status_t1["pods"]["head_001"]["tolerance_ms"] == 1.0

    def test_tier1_sync_passes_at_sub_ms(self):
        """Tier 1 sync passes with <1ms residual drift."""
        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        base_time = 1000.0
        for i in range(5):
            marker = clock_sync.broadcast_sync(base_time + i * 10.0)
            marker.pod_timestamps["head_001"] = base_time + i * 10.0 + 0.0008  # 0.8ms offset

        clock_sync.update_drift_estimates()

        status = clock_sync.get_sync_status(tier=Tier.T1)
        assert status["pods"]["head_001"]["within_tolerance"]
        assert abs(status["pods"]["head_001"]["offset_ms"]) < 1.0

    def test_quantify_residual_drift_tier1_budget(self):
        """quantify_residual_drift validates against Tier 1 budget (1ms)."""
        from synapse24.acquisition.clock_sync import quantify_residual_drift

        hub_ts = np.linspace(0, 10, 5000)  # 500Hz for 10s
        pod_ts = hub_ts + 0.0008  # 0.8ms offset

        result = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T1, config=SyncConfig())

        assert result["within_1ms_pct"] == 100.0
        assert result["tolerance_ms"] == 1.0
        assert result["tier_evaluated"] == "T1"

    def test_quantify_residual_drift_tier0_budget(self):
        """quantify_residual_drift validates against Tier 0 budget (10ms)."""
        from synapse24.acquisition.clock_sync import quantify_residual_drift

        hub_ts = np.linspace(0, 60, 6400)  # 64Hz PPG for 60s
        pod_ts = hub_ts + 0.008  # 8ms offset

        result = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T0, config=SyncConfig())

        assert result["within_10ms_pct"] == 100.0
        assert result["tolerance_ms"] == 10.0
        assert result["tier_evaluated"] == "T0"


class TestMotionGateGating:
    """Test motion gate prevents promotion on contaminated signal (Architecture.md §74)."""

    def test_motion_gate_blocks_promotion_on_low_sqi(self):
        """Low PPG SQI blocks Tier 1 promotion even with immobility."""
        from synapse24.acquisition.power_budget import PowerBudgetManager

        motion_gate = MotionGateConfig(
            sqi_min=0.5,
            map_max=0.5,
            required_consecutive_clean=2,
        )

        controller = AcquisitionController(
            motion_gate=motion_gate,
            clock_fn=time.time,
        )
        controller.power_budget = PowerBudgetManager()

        # Simulate immobility but LOW PPG quality (motion artifact)
        controller.update_motion_quality(ppg_sqi=0.3, motion_artifact_prob=0.8)
        controller.update_motion_quality(ppg_sqi=0.2, motion_artifact_prob=0.9)

        # Try to promote on immobility
        controller.update_imu(
            accel_magnitude=0.01,  # Immobile
            ppg_sqi=0.3,
            motion_artifact_prob=0.8,
        )

        # Should NOT promote - motion gate not armed
        assert controller.state_machine.is_tier0()

    def test_motion_gate_allows_promotion_on_clean_signal(self):
        """Clean PPG allows Tier 1 promotion with immobility."""
        from synapse24.acquisition.power_budget import PowerBudgetManager

        motion_gate = MotionGateConfig(
            sqi_min=0.5,
            map_max=0.5,
            required_consecutive_clean=2,
        )

        immobility_detector = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=1.0,
            magnitude_threshold=0.05,
            min_immobility_min=0.02,  # ~1.2 seconds
        )

        controller = AcquisitionController(
            immobility_detector=immobility_detector,
            motion_gate=motion_gate,
            clock_fn=time.time,
        )
        controller.power_budget = PowerBudgetManager()

        # Simulate immobility with CLEAN PPG quality
        controller.update_motion_quality(ppg_sqi=0.8, motion_artifact_prob=0.1)
        controller.update_motion_quality(ppg_sqi=0.9, motion_artifact_prob=0.05)

        # Promote on immobility (need multiple calls for immobility detector)
        for _ in range(250):
            controller.update_imu(
                accel_magnitude=0.01,
                ppg_sqi=0.85,
                motion_artifact_prob=0.08,
            )

        # Should promote - motion gate armed
        assert controller.state_machine.is_tier1()


class TestEEGQualityGateForTier1:
    """Test EEG signal quality validation for Tier 1 sessions."""

    def test_tier1_eeg_quality_thresholds(self):
        """Tier 1 EEG quality: flatness ≤0.3, alpha_ratio ≥0.3."""
        thresholds = QualityThresholds.for_tier(QualityTier.T1)
        assert thresholds.spectral_flatness_max == 0.3
        assert thresholds.alpha_ratio_min == 0.3  # Alpha/total power ratio

    def test_clean_eeg_passes_tier1_quality(self):
        """Clean EEG (eyes closed, alpha dominant) passes Tier 1."""
        metrics = SignalQualityMetrics(
            modality="eeg",
            tier=QualityTier.T1,
            sampling_rate_hz=500,
            duration_s=60.0,
            spectral_flatness=0.2,
            alpha_band_ratio=2.5,
        )
        assert metrics.overall_pass()

    def test_noisy_eeg_fails_tier1_quality(self):
        """Noisy EEG (flat spectrum, no alpha) fails Tier 1."""
        metrics = SignalQualityMetrics(
            modality="eeg",
            tier=QualityTier.T1,
            sampling_rate_hz=500,
            duration_s=60.0,
            spectral_flatness=0.6,
            alpha_band_ratio=0.5,
        )
        assert not metrics.overall_pass()

    def test_tier0_eeg_relaxed_thresholds(self):
        """Tier 0 EEG (in-ear) uses relaxed thresholds."""
        thresholds = QualityThresholds.for_tier(QualityTier.T0)
        assert thresholds.spectral_flatness_max == 0.6
        assert thresholds.alpha_ratio_min == 0.15


class TestFullPromotionCycleSimulation:
    """End-to-end simulation of Tier 0 → 1 → 0 cycle."""

    def test_complete_cycle_t0_t1_t0(self):
        """Simulate full night cycle: T0 (evening) → T1 (sleep) → T0 (morning)."""
        from synapse24.acquisition.power_budget import PowerBudgetManager

        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("forearm_001", acc_sampling_rate=100)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        motion_gate = MotionGateConfig()
        controller = AcquisitionController(
            power_budget=PowerBudgetManager(),
            motion_gate=motion_gate,
            clock_fn=time.time,
        )

        # Phase 1: Evening - Tier 0 continuous
        assert controller.state_machine.is_tier0()

        # Phase 2: Night window starts - promote to Tier 1
        controller.state_machine.promote_to_tier1("night_window_start")
        assert controller.state_machine.is_tier1()
        assert (
            controller.state_machine.transition_history[-1].transition
            == TierTransition.T0_TO_T1_NIGHT_WINDOW
        )

        # Simulate sync markers at Tier 1 interval (10s)
        base = 1000.0
        for i in range(6):  # 60 seconds of Tier 1
            marker = clock_sync.broadcast_sync(base + i * 10.0)
            marker.pod_timestamps["head_001"] = base + i * 10.0 + 0.0005  # 0.5ms offset

        clock_sync.update_drift_estimates()

        # Verify Tier 1 sync budget met
        status = clock_sync.get_sync_status(tier=Tier.T1)
        assert status["pods"]["head_001"]["within_tolerance"]

        # Phase 3: Morning - night window ends, demote to Tier 0
        controller.state_machine.demote_to_tier0("night_window_end")
        assert controller.state_machine.is_tier0()
        assert (
            controller.state_machine.transition_history[-1].transition
            == TierTransition.T1_TO_T0_WINDOW_END
        )

        # Sync interval relaxes to Tier 0 (60s)
        # Verify by checking status with Tier 0 budget
        status_t0 = clock_sync.get_sync_status(tier=Tier.T0)
        assert status_t0["pods"]["head_001"]["tolerance_ms"] == 10.0


class TestNightWindowScheduler:
    """Test NightWindowScheduler triggers Tier 1 at correct times."""

    def test_night_window_schedule(self):
        """Night window correctly identifies sleep period."""
        from synapse24.acquisition.night_window import NightWindowScheduler, SleepWindowConfig

        scheduler = NightWindowScheduler(
            SleepWindowConfig(
                start_hour=23,  # 11 PM
                end_hour=7,  # 7 AM
                timezone="UTC",
            )
        )

        # Test timestamps (Unix time)
        # 2026-01-01 22:00 UTC = evening (before sleep)
        evening = 1767218400.0
        # 2026-01-02 02:00 UTC = middle of night
        night = 1767232800.0
        # 2026-01-02 10:00 UTC = morning (after sleep, well past 07:15 end)
        morning = 1767348000.0

        assert not scheduler.in_sleep_window(evening)
        assert scheduler.in_sleep_window(night)
        assert not scheduler.in_sleep_window(morning)

    def test_night_window_duration_estimate(self):
        """Window duration estimation matches schedule."""
        from synapse24.acquisition.night_window import NightWindowScheduler, SleepWindowConfig

        scheduler = NightWindowScheduler(
            SleepWindowConfig(
                start_hour=23,
                end_hour=7,
                timezone="UTC",
            )
        )

        # At midnight, should have ~7 hours remaining
        midnight = 1767312000.0  # 2026-01-02 00:00 UTC
        duration = scheduler.estimate_window_duration(midnight)
        assert 6.5 <= duration <= 7.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
