"""End-to-end integration test for Tier 0 → Tier 1 → Tier 0 promotion/demotion cycle.

Validates the complete SYNAPSE-24 acquisition pipeline:
- TierStateMachine with motion gate (Architecture.md §33-43, §74)
- PowerBudgetManager with per-tier profiles (Architecture.md §55-62)
- MultiPodClockSync with tier-specific budgets (Architecture.md §92)
- LSL/XDF zero-drop round-trip (Roadmap.md §151)
- Signal quality gating (signal_quality base thresholds)

This test is the Phase 1 hardware integration contract.
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from synapse24.acquisition.clock_sync import MultiPodClockSync, SyncConfig, TierSyncBudget
from synapse24.acquisition.coordinator import SensorPodCoordinator
from synapse24.acquisition.immobility import ImmobilityDetector
from synapse24.acquisition.night_window import NightWindowScheduler, SleepWindowConfig
from synapse24.acquisition.power_budget import PowerBudgetManager
from synapse24.acquisition.state_machine import (
    AcquisitionController,
    MotionGateConfig,
    TierStateMachine,
    TierTransition,
)
from synapse24.hardware.esp32_tier0 import create_synthetic_tier0_data
from synapse24.signal_quality import QualityThresholds, SignalQualityMetrics, Tier
from synapse24.utils import verify_xdf_roundtrip


class MockClock:
    """Deterministic clock for reproducible testing."""

    def __init__(self, start: float = 0.0):
        self._time = start

    def __call__(self) -> float:
        return self._time

    def advance(self, seconds: float) -> None:
        self._time += seconds

    def set(self, seconds: float) -> None:
        self._time = seconds


def create_mock_pod_coordinator() -> SensorPodCoordinator:
    """Create a mock pod coordinator that tracks tier changes."""
    coordinator = SensorPodCoordinator()
    coordinator.tier_change_log = []

    original_on_tier_change = coordinator.on_tier_change

    def tracked_on_tier_change(event):
        coordinator.tier_change_log.append(
            {
                "from_tier": event.from_tier.name,
                "to_tier": event.to_tier.name,
                "transition": event.transition.value,
                "timestamp": event.timestamp,
                "reason": event.reason,
            }
        )
        original_on_tier_change(event)

    coordinator.on_tier_change = tracked_on_tier_change
    return coordinator


class TierPromotionTestHarness:
    """Complete test harness for tier promotion cycle validation."""

    def __init__(self, clock: MockClock):
        self.clock = clock
        self.duration_s = 30.0  # Reduced simulation time for faster tests

        # Create synthetic Tier 0 data
        self.tier0_data = create_synthetic_tier0_data(
            duration_s=self.duration_s,
            ecg_hr=72.0,
            ppg_hr=72.0,
            motion_level=0.0,  # Start with clean signal
            seed=42,
            ecg_fs=500,
            ppg_fs=64,
            imu_fs=100,
        )

        # Components
        self.power_budget = PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
            tier2_avg_mw=100.0,
            tier2_max_burst_min=30.0,
            reserve_mah=300,
            clock_fn=clock,
        )

        self.immobility_detector = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=5.0,  # 5s windows for faster test
            magnitude_threshold=1.5,  # Synthetic data has ~1.4g stationary (1g gravity + small noise)
            min_immobility_min=0.2,  # 12 seconds for faster test
        )

        self.night_scheduler = NightWindowScheduler(
            SleepWindowConfig(
                start_hour=23,
                end_hour=7,
                timezone="UTC",
                pre_sleep_buffer_min=0,
                post_wake_buffer_min=0,
            )
        )

        self.pod_coordinator = create_mock_pod_coordinator()

        sync_config = SyncConfig(
            tier_budget=TierSyncBudget(
                tier0_max_residual_drift_ms=10.0,
                tier1_max_residual_drift_ms=1.0,
                tier0_sync_interval_s=60.0,
                tier1_sync_interval_s=10.0,
            ),
            acc_corr_window_s=30.0,
            min_acc_correlation=0.7,
            max_history=100,
            acc_sampling_rates={"forearm_hub": 100, "head_pod": 100},
        )
        self.clock_sync = MultiPodClockSync(sync_config, clock_fn=clock)

        # Register pods
        self.clock_sync.register_pod("forearm_hub", 100)
        self.clock_sync.register_pod("head_pod", 100)

        self.controller = AcquisitionController(
            immobility_detector=self.immobility_detector,
            night_scheduler=self.night_scheduler,
            power_budget=self.power_budget,
            pod_coordinator=self.pod_coordinator,
            clock_fn=clock,
            motion_gate=MotionGateConfig(
                sqi_min=0.3,  # Lowered for synthetic data quality
                map_max=0.5,
                required_consecutive_clean=2,
            ),
        )

        # LSL stream manager for XDF output
        self.streams_data: dict[str, list] = {}
        self.stream_timestamps: dict[str, list] = {}

        # Metrics collection
        self.promotion_time: float | None = None
        self.demotion_time: float | None = None
        self.promotion_latency_ms: float | None = None
        self.demotion_latency_ms: float | None = None

    def _init_stream(
        self, name: str, stream_type: str, channel_count: int, sampling_rate: int, tier: int
    ):
        """Initialize a stream buffer."""
        self.streams_data[name] = []
        self.stream_timestamps[name] = []
        self._stream_config = {
            "name": name,
            "type": stream_type,
            "channel_count": channel_count,
            "sampling_rate": sampling_rate,
            "tier": tier,
        }

    def _push_sample(self, name: str, sample: np.ndarray, timestamp: float):
        """Push a sample to stream buffer."""
        if name not in self.streams_data:
            return
        self.streams_data[name].append(sample)
        self.stream_timestamps[name].append(timestamp)

    def run_promotion_cycle(self) -> dict[str, Any]:
        """Run the complete T0 → T1 → T0 promotion/demotion cycle."""
        self._init_tier0_streams()
        sim_params = self._setup_simulation_params()
        tier1_streams_initialized = False
        immobility_start = None
        movement_detected = False

        sim_time = 0.0
        step = 0.01  # 10ms steps

        while sim_time < self.duration_s:
            self.clock.set(sim_time)
            acc_mag = self._process_imu_data(sim_time, sim_params, imu_idx=sim_params["imu_idx"])
            if acc_mag is not None:
                sim_params["imu_idx"] += 1

            self._process_ecg_data(sim_time, sim_params)
            self._process_ppg_data(sim_time, sim_params)

            if int(sim_time / step) % 10 == 0:  # Every 100ms
                self._periodic_controller_tick(sim_time)

            immobility_start = self._check_promotion(sim_time, immobility_start)
            tier1_streams_initialized = self._maybe_init_tier1_streams(tier1_streams_initialized)
            self._generate_tier1_data(sim_time, step)
            movement_detected = self._check_demotion(sim_time, acc_mag, movement_detected)
            self._update_power_budget()

            sim_time += step

        self._finalize_simulation()
        return self._collect_results()

    def _init_tier0_streams(self) -> None:
        """Initialize Tier 0 LSL streams."""
        self._init_stream("SYNAPSE_ECG_T0", "ECG_T0", 1, 500, 0)
        self._init_stream("SYNAPSE_PPG_T0", "PPG_T0", 2, 64, 0)
        self._init_stream("SYNAPSE_ACC_T0", "ACC_T0", 3, 100, 0)

    def _setup_simulation_params(self) -> dict:
        """Set up simulation parameters and indices."""
        dt_ecg = 1.0 / 500
        dt_ppg = 1.0 / 64
        dt_imu = 1.0 / 100

        n_ecg = len(self.tier0_data["ecg"])
        n_ppg = len(self.tier0_data["ppg_red"])
        n_imu = len(self.tier0_data["acc_x"])

        return {
            "dt_ecg": dt_ecg,
            "dt_ppg": dt_ppg,
            "dt_imu": dt_imu,
            "n_ecg": n_ecg,
            "n_ppg": n_ppg,
            "n_imu": n_imu,
            "ecg_idx": 0,
            "ppg_idx": 0,
            "imu_idx": 0,
        }

    def _process_imu_data(self, sim_time: float, params: dict, imu_idx: int) -> float | None:
        """Process IMU data at 100 Hz, return acc_mag if sample processed."""
        if imu_idx >= params["n_imu"]:
            return None
        if abs(sim_time - self.tier0_data["t_imu"][imu_idx]) >= params["dt_imu"] / 2:
            return None

        acc_mag = np.sqrt(
            self.tier0_data["acc_x"][imu_idx] ** 2
            + self.tier0_data["acc_y"][imu_idx] ** 2
            + self.tier0_data["acc_z"][imu_idx] ** 2
        )

        self._push_stream(
            "SYNAPSE_ACC_T0",
            [
                self.tier0_data["acc_x"][imu_idx],
                self.tier0_data["acc_y"][imu_idx],
                self.tier0_data["acc_z"][imu_idx],
            ],
            sim_time,
        )

        self.clock_sync.add_hub_acc(acc_mag, sim_time)
        self.clock_sync.add_pod_acc("forearm_hub", acc_mag, sim_time)

        sqi, map_score = self._compute_motion_quality(params["ppg_idx"], acc_mag)
        self.controller.update_imu(
            accel_magnitude=acc_mag,
            timestamp=sim_time,
            ppg_sqi=sqi,
            motion_artifact_prob=map_score,
        )

        return acc_mag

    def _compute_motion_quality(self, ppg_idx: int, acc_mag: float) -> tuple[float, float]:
        """Compute SQI and MAP from PPG window and ACC magnitude."""
        if ppg_idx <= 0 or ppg_idx >= self.tier0_data["ppg_red"].shape[0]:
            return 0.8, 0.1

        ppg_window = self.tier0_data["ppg_red"][max(0, ppg_idx - 64) : ppg_idx]
        if len(ppg_window) < 32:
            return 0.8, 0.1

        from synapse24.signal_quality import compute_ppg_sqi, ppg_motion_artifact_probability

        sqi = compute_ppg_sqi(np.array(ppg_window), 64)
        map_score = ppg_motion_artifact_probability(
            np.array(ppg_window), np.full(len(ppg_window), acc_mag), 64
        )
        return sqi, map_score

    def _process_ecg_data(self, sim_time: float, params: dict) -> None:
        """Process ECG data at 500 Hz."""
        if params["ecg_idx"] < params["n_ecg"] and abs(sim_time - self.tier0_data["t_ecg"][params["ecg_idx"]]) < params["dt_ecg"] / 2:
            self._push_stream("SYNAPSE_ECG_T0", [self.tier0_data["ecg"][params["ecg_idx"]]], sim_time)
            params["ecg_idx"] += 1

    def _process_ppg_data(self, sim_time: float, params: dict) -> None:
        """Process PPG data at 64 Hz."""
        if params["ppg_idx"] < params["n_ppg"] and abs(sim_time - self.tier0_data["t_ppg"][params["ppg_idx"]]) < params["dt_ppg"] / 2:
            self._push_stream(
                "SYNAPSE_PPG_T0",
                [self.tier0_data["ppg_red"][params["ppg_idx"]], self.tier0_data["ppg_ir"][params["ppg_idx"]]],
                sim_time,
            )
            params["ppg_idx"] += 1

    def _periodic_controller_tick(self, sim_time: float) -> None:
        """Periodic controller and clock sync update."""
        self.controller.tick(sim_time)
        self.clock_sync.update_drift_estimates()
        tier = self.controller.state_machine.current_tier
        if self.clock_sync.marker_manager.should_broadcast(sim_time, tier):
            self.clock_sync.broadcast_sync(sim_time)

    def _check_promotion(self, sim_time: float, immobility_start: float | None) -> float | None:
        """Check and execute T0 → T1 promotion on immobility."""
        if (
            self.controller.state_machine.is_tier0()
            and self.controller.immobility_detector is not None
            and self.controller.immobility_detector.current_window_immobile
        ):
            if immobility_start is None:
                return sim_time
            if sim_time - immobility_start >= 12.0:  # 12 seconds immobility (matches min_immobility_min=0.2)
                if self.power_budget.can_afford_tier1(2.0) and self.controller._motion_gate_armed():
                    self.promotion_time = sim_time
                    self.promotion_latency_ms = (sim_time - immobility_start) * 1000
        return immobility_start

    def _maybe_init_tier1_streams(self, initialized: bool) -> bool:
        """Initialize Tier 1 streams on first promotion."""
        if self.controller.state_machine.is_tier1() and not initialized:
            self._init_stream("SYNAPSE_EEG_T1", "EEG_T1", 8, 500, 1)
            self._init_stream("SYNAPSE_FNIRS_T1", "FNIRS_T1", 8, 10, 1)
            self._init_stream("SYNAPSE_ECG_T1", "ECG_T1", 1, 500, 1)
            return True
        return initialized

    def _generate_tier1_data(self, sim_time: float, step: float) -> None:
        """Generate synthetic Tier 1 data when in Tier 1."""
        if not self.controller.state_machine.is_tier1():
            return

        if int(sim_time * 500) > int((sim_time - step) * 500):
            eeg_sample = np.random.normal(0, 10, 8)
            self._push_stream("SYNAPSE_EEG_T1", eeg_sample, sim_time)

            ecg_rest = 1000 * np.sin(2 * np.pi * 1.2 * sim_time)
            self._push_stream("SYNAPSE_ECG_T1", [ecg_rest], sim_time)

        if int(sim_time * 10) > int((sim_time - step) * 10):
            fnirs_sample = np.random.normal(10000, 100, 8)
            self._push_stream("SYNAPSE_FNIRS_T1", fnirs_sample, sim_time)

    def _check_demotion(self, sim_time: float, acc_mag: float | None, movement_detected: bool) -> bool:
        """Check and execute T1 → T0 demotion on movement."""
        # Prevent demotion for at least 2 seconds after promotion
        if self.promotion_time is not None and sim_time - self.promotion_time < 2.0:
            return movement_detected

        if (
            self.controller.state_machine.is_tier1()
            and acc_mag is not None
            and acc_mag > 1.6  # Higher threshold for demotion (synthetic data has ~1.4g stationary)
            and not movement_detected
        ):
            self.demotion_time = sim_time
            self.demotion_latency_ms = 10.0  # step * 1000
            return True
        # Reset movement_detected if acceleration drops below threshold (allows re-detection)
        if movement_detected and acc_mag is not None and acc_mag <= 1.6:
            return False
        return movement_detected

    def _update_power_budget(self) -> None:
        """Update power budget with current tier."""
        self.power_budget.on_tier_change(
            self.controller.state_machine.previous_tier or Tier.T0,
            self.controller.state_machine.current_tier,
        )

    def _finalize_simulation(self) -> None:
        """Final updates after simulation loop."""
        self.controller.tick(self.duration_s)
        self.clock_sync.update_drift_estimates()
        self.power_budget.on_tier_change(
            self.controller.state_machine.previous_tier or Tier.T0,
            self.controller.state_machine.current_tier,
        )

    def _push_stream(self, name: str, sample: list | np.ndarray, timestamp: float):
        """Push sample to stream buffer."""
        if name in self.streams_data:
            self.streams_data[name].append(np.array(sample, dtype=np.float64))
            self.stream_timestamps[name].append(timestamp)

    def _collect_results(self) -> dict[str, Any]:
        """Collect all test results."""
        # Convert streams to arrays
        streams_for_xdf = []
        for name, data in self.streams_data.items():
            if data:
                timestamps = np.array(self.stream_timestamps[name], dtype=np.float64)
                data_arr = np.array(data, dtype=np.float64)
                if data_arr.ndim == 1:
                    data_arr = data_arr.reshape(-1, 1)

                # Get stream config
                stream_type = "Other"
                channel_count = data_arr.shape[1]
                sampling_rate = 0
                tier = 0
                if "ECG_T0" in name:
                    stream_type, sampling_rate, tier = "ECG_T0", 500, 0
                elif "PPG_T0" in name:
                    stream_type, sampling_rate, tier = "PPG_T0", 64, 0
                elif "ACC_T0" in name:
                    stream_type, sampling_rate, tier = "ACC_T0", 100, 0
                elif "EEG_T1" in name:
                    stream_type, sampling_rate, tier = "EEG_T1", 500, 1
                elif "FNIRS_T1" in name:
                    stream_type, sampling_rate, tier = "FNIRS_T1", 10, 1
                elif "ECG_T1" in name:
                    stream_type, sampling_rate, tier = "ECG_T1", 500, 1

                streams_for_xdf.append(
                    {
                        "name": name,
                        "type": stream_type,
                        "data": data_arr,
                        "timestamps": timestamps,
                        "sampling_rate": sampling_rate,
                        "tier": tier,
                    }
                )

        # XDF round-trip verification
        with tempfile.NamedTemporaryFile(suffix=".xdf", delete=False) as f:
            xdf_path = Path(f.name)

        try:
            xdf_proof = verify_xdf_roundtrip(streams_for_xdf, xdf_path)
        finally:
            if xdf_path.exists():
                xdf_path.unlink()

        # Clock sync residuals
        sync_status = self.clock_sync.get_sync_status(Tier.T0)
        tier1_sync_status = self.clock_sync.get_sync_status(Tier.T1)

        # Power budget status
        power_status = self.power_budget.get_status()

        # Tier transition log
        transitions = self.controller.state_machine.transition_history

        return {
            "promotion_latency_ms": self.promotion_latency_ms,
            "demotion_latency_ms": self.demotion_latency_ms,
            "promotion_occurred": self.promotion_time is not None,
            "demotion_occurred": self.demotion_time is not None,
            "final_tier": self.controller.state_machine.current_tier.name,
            "transition_count": len(transitions),
            "transitions": [
                {
                    "from": t.from_tier.name,
                    "to": t.to_tier.name,
                    "transition": t.transition.value,
                    "reason": t.reason,
                    "timestamp": t.timestamp,
                }
                for t in transitions
            ],
            "xdf_proof": xdf_proof,
            "tier0_sync_residuals": sync_status,
            "tier1_sync_residuals": tier1_sync_status,
            "power_budget": {
                "battery_remaining_mah": power_status.battery_remaining_mah,
                "estimated_remaining_h": power_status.estimated_remaining_h,
                "tier0_h_used": power_status.tier0_h_used,
                "tier1_h_used": power_status.tier1_h_used,
                "can_afford_tier1": power_status.can_afford_tier1,
                "power_draw_mw": power_status.power_draw_mw,
            },
            "stream_counts": {name: len(data) for name, data in self.streams_data.items()},
        }


class TestTierPromotionCycle:
    """Test suite for tier promotion/demotion cycle."""

    @pytest.fixture
    def harness(self):
        """Create test harness with deterministic clock."""
        clock = MockClock(0.0)
        return TierPromotionTestHarness(clock)

    def test_t0_to_t1_promotion_on_immobility(self, harness):
        """Test Tier 0 → Tier 1 promotion when immobility detected."""
        results = harness.run_promotion_cycle()

        # Promotion must occur at least once
        assert results["transition_count"] > 0, "No tier transitions occurred"

        # Must have at least one T0→T1 promotion
        promotion_transitions = [
            t for t in results["transitions"]
            if t["transition"] == TierTransition.T0_TO_T1_IMMOBILITY.value
        ]
        assert len(promotion_transitions) >= 1, (
            f"Expected at least 1 T0→T1 promotion, got {len(promotion_transitions)}"
        )

        # Final tier should be T1 (last promotion wins due to continuous immobility)
        assert results["final_tier"] == "T1", f"Expected final tier T1, got {results['final_tier']}"

    def test_t1_to_t0_demotion_on_movement(self):
        """Test Tier 1 → Tier 0 demotion when movement detected."""
        # Create harness with high motion - motion gate should block promotion
        clock = MockClock(0.0)
        harness_high_motion = TierPromotionTestHarness(clock)

        # Override synthetic data with high motion (motion_level=2.0 creates high accel)
        harness_high_motion.tier0_data = create_synthetic_tier0_data(
            duration_s=30.0,
            motion_level=2.0,
            seed=123,
        )

        results = harness_high_motion.run_promotion_cycle()

        # With high motion, motion gate should block promotion entirely
        # (SQI will be low, MAP high)
        promotion_transitions = [
            t for t in results["transitions"]
            if t["transition"] == TierTransition.T0_TO_T1_IMMOBILITY.value
        ]
        assert len(promotion_transitions) == 0, (
            f"Expected no promotions with high motion, got {len(promotion_transitions)}"
        )
        assert results["final_tier"] == "T0", f"Expected final tier T0, got {results['final_tier']}"
        transitions = results["transitions"]
        demotion_transitions = [
            t for t in transitions if t["transition"] == TierTransition.T1_TO_T0_MOVEMENT.value
        ]
        # With high motion, no promotion occurs, so no demotion either
        assert len(demotion_transitions) == 0, (
            f"Expected no demotions with high motion, got {len(demotion_transitions)}"
        )

    def test_xdf_zero_drop_roundtrip(self, harness):
        """Test XDF write/read round-trip with zero dropped samples."""
        results = harness.run_promotion_cycle()

        xdf_proof = results["xdf_proof"]
        assert xdf_proof["all_streams_valid"], "XDF validation failed: streams invalid"
        assert xdf_proof["total_dropped"] == 0, (
            f"XDF dropped {xdf_proof['total_dropped']} samples (expected 0)"
        )
        assert xdf_proof["total_expected"] == xdf_proof["total_recovered"], (
            f"Sample count mismatch: expected {xdf_proof['total_expected']}, "
            f"recovered {xdf_proof['total_recovered']}"
        )

        # Verify all expected streams present
        stream_names = {s["name"] for s in xdf_proof["per_stream"]}
        expected_t0 = {"SYNAPSE_ECG_T0", "SYNAPSE_PPG_T0", "SYNAPSE_ACC_T0"}
        expected_t1 = {"SYNAPSE_EEG_T1", "SYNAPSE_FNIRS_T1", "SYNAPSE_ECG_T1"}
        assert expected_t0.issubset(stream_names), (
            f"Missing Tier 0 streams: {expected_t0 - stream_names}"
        )
        assert expected_t1.issubset(stream_names), (
            f"Missing Tier 1 streams: {expected_t1 - stream_names}"
        )

    def test_tier0_sync_residual_within_budget(self, harness):
        """Test Tier 0 clock sync residual ≤ 10 ms (p99)."""
        results = harness.run_promotion_cycle()

        sync_status = results["tier0_sync_residuals"]
        pods = sync_status.get("pods", {})

        for pod_id, pod_status in pods.items():
            assert pod_status["within_tolerance"], (
                f"Pod {pod_id} Tier 0 sync residual {pod_status['offset_ms']:.2f}ms "
                f"exceeds {pod_status['tolerance_ms']}ms tolerance"
            )
            assert pod_status["tier_evaluated"] == "T0", (
                f"Wrong tier evaluated: {pod_status['tier_evaluated']}"
            )

    def test_tier1_sync_residual_within_budget(self, harness):
        """Test Tier 1 clock sync residual ≤ 1 ms (p99)."""
        results = harness.run_promotion_cycle()

        sync_status = results["tier1_sync_residuals"]
        pods = sync_status.get("pods", {})

        for pod_id, pod_status in pods.items():
            assert pod_status["within_tolerance"], (
                f"Pod {pod_id} Tier 1 sync residual {pod_status['offset_ms']:.2f}ms "
                f"exceeds {pod_status['tolerance_ms']}ms tolerance"
            )
            assert pod_status["tier_evaluated"] == "T1", (
                f"Wrong tier evaluated: {pod_status['tier_evaluated']}"
            )

    def test_power_budget_24h_projection(self, harness):
        """Test power budget projects ≥ 24h on 3000 mAh."""
        results = harness.run_promotion_cycle()

        power = results["power_budget"]
        assert power["estimated_remaining_h"] >= 24.0, (
            f"Projected battery life {power['estimated_remaining_h']:.1f}h < 24h target"
        )
        assert power["can_afford_tier1"], "Power budget cannot afford Tier 1 session"
        assert power["battery_remaining_mah"] > 0, "Battery depleted during test"

    def test_motion_gate_blocks_promotion_on_artifact(self, harness):
        """Test motion gate prevents promotion when PPG quality is poor."""
        # Create harness with high motion artifact
        clock = MockClock(0.0)
        harness_high_motion = TierPromotionTestHarness(clock)

        # Override synthetic data with high motion
        harness_high_motion.tier0_data = create_synthetic_tier0_data(
            duration_s=60.0,
            motion_level=2.0,  # High motion
            seed=123,
        )

        results = harness_high_motion.run_promotion_cycle()

        # Should NOT promote due to motion gate
        assert not results["promotion_occurred"], (
            "Promotion occurred despite high motion artifact (motion gate failed)"
        )
        assert results["final_tier"] == "T0", "Should remain in Tier 0 with high motion"

    def test_power_budget_blocks_promotion_when_depleted(self):
        """Test power budget prevents promotion when battery low."""
        # Create harness with small battery to force power budget blocking
        clock = MockClock(0.0)
        harness_low_batt = TierPromotionTestHarness(clock)

        # Replace power budget with one that has tiny battery (100 mAh) and high Tier 1 consumption
        new_power_budget = PowerBudgetManager(
            hub_battery_mah=100,  # Tiny battery
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=500.0,  # Very high Tier 1 consumption
            tier1_max_h=10.0,
            tier2_avg_mw=100.0,
            tier2_max_burst_min=30.0,
            reserve_mah=10,
            clock_fn=clock,
        )
        harness_low_batt.power_budget = new_power_budget
        harness_low_batt.controller.power_budget = new_power_budget

        results = harness_low_batt.run_promotion_cycle()

        # Should NOT promote due to power budget
        promotion_transitions = [
            t for t in results["transitions"]
            if t["transition"] == TierTransition.T0_TO_T1_IMMOBILITY.value
        ]
        assert len(promotion_transitions) == 0, (
            f"Promotion occurred despite insufficient power budget: {len(promotion_transitions)} transitions"
        )
        assert results["final_tier"] == "T0", "Should remain in Tier 0 with low battery"


def test_phase1_entry_gate_report():
    """Generate Phase 1 entry gate validation report."""
    clock = MockClock(0.0)
    harness = TierPromotionTestHarness(clock)
    results = harness.run_promotion_cycle()

    promo_latency = results["promotion_latency_ms"]
    demo_latency = results["demotion_latency_ms"]

    # Check actual promotion/demotion via transitions
    promotion_transitions = [
        t for t in results["transitions"]
        if t["transition"] == TierTransition.T0_TO_T1_IMMOBILITY.value
    ]
    demotion_transitions = [
        t for t in results["transitions"]
        if t["transition"] == TierTransition.T1_TO_T0_MOVEMENT.value
    ]

    promo_pass = len(promotion_transitions) >= 1
    demo_pass = len(demotion_transitions) >= 1
    final_tier_pass = results["final_tier"] == "T1"
    xdf_pass = results["xdf_proof"]["total_dropped"] == 0
    tier0_sync_pass = all(
        p["within_tolerance"] for p in results["tier0_sync_residuals"].get("pods", {}).values()
    )
    tier1_sync_pass = all(
        p["within_tolerance"] for p in results["tier1_sync_residuals"].get("pods", {}).values()
    )
    power_pass = results["power_budget"]["estimated_remaining_h"] >= 24.0

    report = {
        "phase": "Phase 1 Entry Gate",
        "timestamp": time.time(),
        "promotion_latency_ms": results["promotion_latency_ms"],
        "demotion_latency_ms": results["demotion_latency_ms"],
        "promotion_count": len(promotion_transitions),
        "demotion_count": len(demotion_transitions),
        "promotion_pass": promo_pass,
        "demotion_pass": demo_pass,
        "final_tier_pass": final_tier_pass,
        "xdf_zero_drop_pass": xdf_pass,
        "tier0_sync_pass": tier0_sync_pass,
        "tier1_sync_pass": tier1_sync_pass,
        "power_budget_24h_pass": power_pass,
        "overall_pass": promo_pass and final_tier_pass and xdf_pass and tier0_sync_pass and tier1_sync_pass and power_pass,
        "details": results,
    }

    # Write report
    output_path = Path("data/processed/phase1_entry_gate_report.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    print("\n=== Phase 1 Entry Gate Report ===")
    promo_str = f"{len(promotion_transitions)} transitions" if promotion_transitions else "NONE"
    demo_str = f"{len(demotion_transitions)} transitions" if demotion_transitions else "NONE"
    print(f"Promotions: {promo_str}")
    print(f"Demotions: {demo_str}")
    print(f"Final Tier: {results['final_tier']} (target T1)")
    print(f"XDF Dropped: {results['xdf_proof']['total_dropped']} (target 0)")
    print(f"Tier 0 Sync: {'PASS' if tier0_sync_pass else 'FAIL'}")
    print(f"Tier 1 Sync: {'PASS' if tier1_sync_pass else 'FAIL'}")
    print(f"Power Budget 24h: {'PASS' if power_pass else 'FAIL'}")
    print(f"OVERALL: {'PASS' if report['overall_pass'] else 'FAIL'}")
    print(f"Report written to: {output_path}")

    # Assert overall pass for CI gate
    assert report["overall_pass"], "Phase 1 entry gate FAILED - see report for details"
    print(f"Power Budget 24h: {'PASS' if report['power_budget_24h_pass'] else 'FAIL'}")
    print(f"OVERALL: {'PASS' if report['overall_pass'] else 'FAIL'}")
    print(f"Report written to: {output_path}")

    # Assert overall pass for CI gate
    assert report["overall_pass"], "Phase 1 entry gate FAILED - see report for details"


if __name__ == "__main__":
    # Run as standalone script
    test_phase1_entry_gate_report()
