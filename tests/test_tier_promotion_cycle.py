"""Integration test for Tier 0 → Tier 1 promotion cycle with sync alignment.

Architecture.md §33-43: Three-tier acquisition strategy.
Architecture.md §74: Motion artifact is dominant EEG/fNIRS failure mode,
promotion must be gated on measured PPG quality.
Architecture.md §92: Tier 1 requires 1ms residual drift, 10s sync interval.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from synapse24.acquisition import (
    AcquisitionController,
    ImmobilityDetector,
    MotionGateConfig,
    MultiPodClockSync,
    SyncConfig,
)
from synapse24.acquisition.clock_sync import SyncMarker, Tier, quantify_residual_drift
from synapse24.acquisition.night_window import NightWindowScheduler, SleepWindowConfig
from synapse24.acquisition.power_budget import PowerBudgetManager
from synapse24.acquisition.state_machine import TierTransition
from synapse24.signal_quality import QualityThresholds, SignalQualityMetrics
from synapse24.signal_quality import Tier as QualityTier
from tests.constants import (
    DEFAULT_HUB_BATTERY_MAH,
    DEFAULT_TARGET_LIFETIME_H,
    MOTION_GATE_MAP_MAX,
    MOTION_GATE_REQUIRED_CONSECUTIVE,
    MOTION_GATE_SQI_MIN,
    NIGHT_WINDOW_MAX_HOURS,
    NIGHT_WINDOW_MIN_HOURS,
    SYNC_TIER0_TOLERANCE_MS,
    SYNC_TIER1_TOLERANCE_MS,
    TIER0_ALPHA_RATIO_MIN,
    TIER0_SPECTRAL_FLATNESS_MAX,
    TIER1_ALPHA_RATIO_MIN,
    TIER1_SPECTRAL_FLATNESS_MAX,
)


class TestTierPromotionCycle:
    """Test complete Tier 0 → Tier 1 → Tier 0 cycle with sync."""

    def test_tier0_to_t1_promotion_on_immobility(self):
        """Promotion requires immobility + power budget + motion gate."""
        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)

        clock_sync.register_pod("forearm_001", acc_sampling_rate=100)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        motion_gate = MotionGateConfig(
            sqi_min=MOTION_GATE_SQI_MIN,
            map_max=MOTION_GATE_MAP_MAX,
            required_consecutive_clean=MOTION_GATE_REQUIRED_CONSECUTIVE,
        )

        immobility_detector = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=1.0,
            magnitude_threshold=0.05,
            min_immobility_min=0.02,
        )

        power_budget = PowerBudgetManager(
            hub_battery_mah=DEFAULT_HUB_BATTERY_MAH,
            target_lifetime_h=DEFAULT_TARGET_LIFETIME_H,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
        )

        controller = AcquisitionController(
            immobility_detector=immobility_detector,
            motion_gate=motion_gate,
            power_budget=power_budget,
            clock_fn=time.time,
        )

        assert controller.state_machine.is_tier0()

        controller.update_motion_quality(ppg_sqi=0.7, motion_artifact_prob=0.2)
        controller.update_motion_quality(ppg_sqi=0.8, motion_artifact_prob=0.1)

        for _ in range(250):
            controller.update_imu(
                accel_magnitude=0.01,
                ppg_sqi=0.75,
                motion_artifact_prob=0.15,
            )

        assert controller.state_machine.is_tier1()
        assert len(controller.state_machine.transition_history) == 1
        event = controller.state_machine.transition_history[0]
        assert event.transition == TierTransition.T0_TO_T1_IMMOBILITY

    def test_tier1_to_t0_demotion_on_movement(self):
        """Demotion on movement detection with contaminated PPG."""
        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("forearm_001", acc_sampling_rate=100)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        motion_gate = MotionGateConfig(
            sqi_min=MOTION_GATE_SQI_MIN,
            map_max=MOTION_GATE_MAP_MAX,
            required_consecutive_clean=MOTION_GATE_REQUIRED_CONSECUTIVE,
        )

        controller = AcquisitionController(
            motion_gate=motion_gate,
            power_budget=PowerBudgetManager(),
            clock_fn=time.time,
        )
        controller.state_machine.promote_to_tier1("night_window_start")
        assert controller.state_machine.is_tier1()

        controller.update_motion_quality(ppg_sqi=0.3, motion_artifact_prob=0.8)
        controller.update_motion_quality(ppg_sqi=0.2, motion_artifact_prob=0.9)

        controller.update_imu(
            accel_magnitude=2.5,
            ppg_sqi=0.2,
            motion_artifact_prob=0.9,
        )

        assert controller.state_machine.is_tier0()
        demotion_event = controller.state_machine.transition_history[-1]
        assert demotion_event.transition == TierTransition.T1_TO_T0_MOVEMENT
        assert "movement" in demotion_event.reason.lower()

    def test_tier1_demotion_on_power_budget_exceeded(self):
        """Tier 1 times out when power budget exceeded."""
        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("forearm_001", acc_sampling_rate=100)

        power_budget = PowerBudgetManager(
            hub_battery_mah=DEFAULT_HUB_BATTERY_MAH,
            target_lifetime_h=DEFAULT_TARGET_LIFETIME_H,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=1.0,
        )

        motion_gate = MotionGateConfig()

        clock_state = {"now": time.time()}

        def custom_clock():
            return clock_state["now"]

        controller = AcquisitionController(
            power_budget=power_budget,
            motion_gate=motion_gate,
            clock_fn=custom_clock,
        )

        controller.state_machine.promote_to_tier1("night_window_start")
        assert controller.state_machine.is_tier1()

        clock_state["now"] += 2 * 3600
        controller.tick()

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

        base_time = 1000.0
        for i in range(3):
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=base_time + i * 60.0,
                pod_timestamps={"head_001": base_time + i * 60.0 + 0.005},
            )
            clock_sync.drift_estimator.add_marker(marker)

        clock_sync.update_drift_estimates()

        status_t0 = clock_sync.get_sync_status(tier=Tier.T0)
        assert status_t0["pods"]["head_001"]["within_tolerance"]
        assert status_t0["pods"]["head_001"]["tolerance_ms"] == SYNC_TIER0_TOLERANCE_MS

        for i in range(3):
            marker = SyncMarker(
                sequence=i + 3,
                hub_timestamp=base_time + 200.0 + i * 10.0,
                pod_timestamps={"head_001": base_time + 200.0 + i * 10.0 + 0.005},
            )
            clock_sync.drift_estimator.add_marker(marker)

        clock_sync.update_drift_estimates()

        status_t1 = clock_sync.get_sync_status(tier=Tier.T1)
        assert not status_t1["pods"]["head_001"]["within_tolerance"]
        assert status_t1["pods"]["head_001"]["tolerance_ms"] == SYNC_TIER1_TOLERANCE_MS

    def test_tier1_sync_passes_at_sub_ms(self):
        """Tier 1 sync passes with <1ms residual drift."""
        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        base_time = 1000.0
        for i in range(5):
            marker = clock_sync.broadcast_sync(base_time + i * 10.0)
            marker.pod_timestamps["head_001"] = base_time + i * 10.0 + 0.0008

        clock_sync.update_drift_estimates()

        status = clock_sync.get_sync_status(tier=Tier.T1)
        assert status["pods"]["head_001"]["within_tolerance"]
        assert abs(status["pods"]["head_001"]["offset_ms"]) < 1.0

    def test_quantify_residual_drift_tier1_budget(self):
        """quantify_residual_drift validates against Tier 1 budget (1ms)."""
        hub_ts = np.linspace(0, 10, 5000)
        pod_ts = hub_ts + 0.0008

        result = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T1, config=SyncConfig())

        assert result["within_1ms_pct"] == 100.0
        assert result["tolerance_ms"] == SYNC_TIER1_TOLERANCE_MS
        assert result["tier_evaluated"] == "T1"

    def test_quantify_residual_drift_tier0_budget(self):
        """quantify_residual_drift validates against Tier 0 budget (10ms)."""
        hub_ts = np.linspace(0, 60, 6400)
        pod_ts = hub_ts + 0.008

        result = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T0, config=SyncConfig())

        assert result["within_10ms_pct"] == 100.0
        assert result["tolerance_ms"] == SYNC_TIER0_TOLERANCE_MS
        assert result["tier_evaluated"] == "T0"


class TestMotionGateGating:
    """Test motion gate prevents promotion on contaminated signal (Architecture.md §74)."""

    def test_motion_gate_blocks_promotion_on_low_sqi(self):
        """Low PPG SQI blocks Tier 1 promotion even with immobility."""
        motion_gate = MotionGateConfig(
            sqi_min=MOTION_GATE_SQI_MIN,
            map_max=MOTION_GATE_MAP_MAX,
            required_consecutive_clean=MOTION_GATE_REQUIRED_CONSECUTIVE,
        )

        controller = AcquisitionController(
            motion_gate=motion_gate,
            power_budget=PowerBudgetManager(),
            clock_fn=time.time,
        )

        controller.update_motion_quality(ppg_sqi=0.3, motion_artifact_prob=0.8)
        controller.update_motion_quality(ppg_sqi=0.2, motion_artifact_prob=0.9)

        controller.update_imu(
            accel_magnitude=0.01,
            ppg_sqi=0.3,
            motion_artifact_prob=0.8,
        )

        assert controller.state_machine.is_tier0()

    def test_motion_gate_allows_promotion_on_clean_signal(self):
        """Clean PPG allows Tier 1 promotion with immobility."""
        motion_gate = MotionGateConfig(
            sqi_min=MOTION_GATE_SQI_MIN,
            map_max=MOTION_GATE_MAP_MAX,
            required_consecutive_clean=MOTION_GATE_REQUIRED_CONSECUTIVE,
        )

        immobility_detector = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=1.0,
            magnitude_threshold=0.05,
            min_immobility_min=0.02,
        )

        controller = AcquisitionController(
            immobility_detector=immobility_detector,
            motion_gate=motion_gate,
            power_budget=PowerBudgetManager(),
            clock_fn=time.time,
        )

        controller.update_motion_quality(ppg_sqi=0.8, motion_artifact_prob=0.1)
        controller.update_motion_quality(ppg_sqi=0.9, motion_artifact_prob=0.05)

        for _ in range(250):
            controller.update_imu(
                accel_magnitude=0.01,
                ppg_sqi=0.85,
                motion_artifact_prob=0.08,
            )

        assert controller.state_machine.is_tier1()


class TestEEGQualityGateForTier1:
    """Test EEG signal quality validation for Tier 1 sessions."""

    def test_tier1_eeg_quality_thresholds(self):
        """Tier 1 EEG quality: flatness ≤0.3, alpha_ratio ≥0.3."""
        thresholds = QualityThresholds.for_tier(QualityTier.T1)
        assert thresholds.spectral_flatness_max == TIER1_SPECTRAL_FLATNESS_MAX
        assert thresholds.alpha_ratio_min == TIER1_ALPHA_RATIO_MIN

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
        assert thresholds.spectral_flatness_max == TIER0_SPECTRAL_FLATNESS_MAX
        assert thresholds.alpha_ratio_min == TIER0_ALPHA_RATIO_MIN


class TestFullPromotionCycleSimulation:
    """End-to-end simulation of Tier 0 → 1 → 0 cycle."""

    def test_complete_cycle_t0_t1_t0(self):
        """Simulate full night cycle: T0 (evening) → T1 (sleep) → T0 (morning)."""
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

        assert controller.state_machine.is_tier0()

        controller.state_machine.promote_to_tier1("night_window_start")
        assert controller.state_machine.is_tier1()
        assert (
            controller.state_machine.transition_history[-1].transition
            == TierTransition.T0_TO_T1_NIGHT_WINDOW
        )

        base = 1000.0
        for i in range(6):
            marker = clock_sync.broadcast_sync(base + i * 10.0)
            marker.pod_timestamps["head_001"] = base + i * 10.0 + 0.0005

        clock_sync.update_drift_estimates()

        status = clock_sync.get_sync_status(tier=Tier.T1)
        assert status["pods"]["head_001"]["within_tolerance"]

        controller.state_machine.demote_to_tier0("night_window_end")
        assert controller.state_machine.is_tier0()
        assert (
            controller.state_machine.transition_history[-1].transition
            == TierTransition.T1_TO_T0_WINDOW_END
        )

        status_t0 = clock_sync.get_sync_status(tier=Tier.T0)
        assert status_t0["pods"]["head_001"]["tolerance_ms"] == SYNC_TIER0_TOLERANCE_MS


class TestNightWindowScheduler:
    """Test NightWindowScheduler triggers Tier 1 at correct times."""

    def test_night_window_schedule(self):
        """Night window correctly identifies sleep period."""
        scheduler = NightWindowScheduler(
            SleepWindowConfig(
                start_hour=23,
                end_hour=7,
                timezone="UTC",
            )
        )

        evening = 1767218400.0
        night = 1767232800.0
        morning = 1767348000.0

        assert not scheduler.in_sleep_window(evening)
        assert scheduler.in_sleep_window(night)
        assert not scheduler.in_sleep_window(morning)

    def test_night_window_duration_estimate(self):
        """Window duration estimation matches schedule."""
        scheduler = NightWindowScheduler(
            SleepWindowConfig(
                start_hour=23,
                end_hour=7,
                timezone="UTC",
            )
        )

        midnight = 1767312000.0
        duration = scheduler.estimate_window_duration(midnight)
        assert NIGHT_WINDOW_MIN_HOURS <= duration <= NIGHT_WINDOW_MAX_HOURS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
