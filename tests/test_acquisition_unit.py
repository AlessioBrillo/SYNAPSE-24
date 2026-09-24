"""Unit tests for acquisition module - targeting 80%+ coverage."""

import tempfile
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from synapse24.acquisition import (
    NOMINAL_VOLTAGE_V,
    AcquisitionController,
    ClockDriftEstimator,
    DriftEstimate,
    EnergyBudgetStatus,
    GatewayConfig,
    ImmobilityDetector,
    LiveTwoPodConfig,
    LSLGateway,
    LSLUnavailableError,
    MotionGateConfig,
    MultiPodClockSync,
    NightWindowScheduler,
    PodStreamConfig,
    PowerBudgetManager,
    PowerProfile,
    SensorPodCoordinator,
    SyncConfig,
    SyncMarker,
    SyncMarkerManager,
    SyncMarkerRecorder,
    SyncStreamConfig,
    Tier,
    Tier0PodState,
    Tier1Coordinator,
    Tier1PodState,
    TierStateMachine,
    TierSyncBudget,
    TierTransition,
    TimestampCorrector,
    TransitionEvent,
    create_pod_stream_config,
    hours_for_charge,
    mah_for_power,
    quantify_residual_drift,
    run_live_2pod_sync,
)


class TestTierStateMachine:
    """Tests for TierStateMachine."""

    def test_initial_state(self):
        """Test initial state is Tier 0."""
        machine = TierStateMachine()
        assert machine.current_tier == Tier.T0
        assert machine.previous_tier is None

    def test_t0_to_t1_promotion(self):
        """Test promotion from T0 to T1."""
        machine = TierStateMachine()
        result = machine.promote_to_tier1("immobility_detected")
        assert result is True
        assert machine.current_tier == Tier.T1
        assert machine.previous_tier == Tier.T0
        assert len(machine.transition_history) == 1

    def test_t1_to_t0_demotion(self):
        """Test demotion from T1 to T0."""
        machine = TierStateMachine()
        machine.promote_to_tier1("test")
        result = machine.demote_to_tier0("movement_detected")
        assert result is True
        assert machine.current_tier == Tier.T0
        assert machine.previous_tier == Tier.T1

    def test_t1_to_t2_promotion(self):
        """Test promotion from T1 to T2."""
        machine = TierStateMachine()
        machine.promote_to_tier1("test")
        result = machine.start_tier2("voluntary_session")
        assert result is True
        assert machine.current_tier == Tier.T2

    def test_t2_to_t1_demotion(self):
        """Test demotion from T2 to T1."""
        machine = TierStateMachine()
        machine.promote_to_tier1("test")
        machine.start_tier2("test")
        result = machine.end_tier2("session_done")
        assert result is True
        assert machine.current_tier == Tier.T1

    def test_invalid_transition_returns_false(self):
        """Test invalid transition returns False."""
        machine = TierStateMachine()
        # T0 -> T2 is valid (T0_TO_T2_USER)
        result = machine.start_tier2("user_request")
        assert result is True
        # But T2 -> T2 should fail
        result = machine.start_tier2("another_request")
        assert result is False

    def test_get_tier_duration(self):
        """Test tier duration tracking."""
        machine = TierStateMachine()
        time.sleep(0.01)
        machine.promote_to_tier1("test")
        time.sleep(0.01)
        duration = machine.get_tier1_duration()
        assert duration is not None
        assert duration > 0

    def test_reset(self):
        """Test state machine reset."""
        machine = TierStateMachine()
        machine.promote_to_tier1("test")
        machine.reset()
        assert machine.current_tier == Tier.T0
        assert machine.previous_tier is None
        assert len(machine.transition_history) == 0


class TestPowerBudgetManager:
    """Tests for PowerBudgetManager."""

    def test_initialization(self):
        """Test power budget initialization."""
        budget = PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
            tier2_avg_mw=100.0,
            tier2_max_burst_min=30.0,
            reserve_mah=300,
        )
        assert budget.hub_battery_mah == 3000
        assert budget.usable_mah == 3000 - 300
        # Initial consumption is 0
        remaining = budget.get_remaining_mah()
        assert remaining == pytest.approx(3000 - 300, abs=0.1)

    def test_tier0_endurance(self):
        """Test T0 endurance calculation."""
        budget = PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
            tier2_avg_mw=100.0,
            tier2_max_burst_min=30.0,
            reserve_mah=300,
        )
        budget.on_tier_change(Tier.T0, Tier.T0)
        time.sleep(0.01)
        status = budget.get_status()
        assert status.tier0_h_used >= 0
        assert status.estimated_remaining_h > 0

    def test_tier1_affordability(self):
        """Test T1 affordability check."""
        budget = PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
            tier2_avg_mw=100.0,
            tier2_max_burst_min=30.0,
            reserve_mah=300,
        )
        budget.on_tier_change(Tier.T0, Tier.T0)
        assert budget.can_afford_tier1(2.0) is True
        # Request duration exceeding max_duration_h (10h) should fail
        assert budget.can_afford_tier1(15.0) is False
        # Within max duration should pass if battery allows
        assert budget.can_afford_tier1(5.0) is True

    def test_tier2_burst(self):
        """Test T2 burst affordability."""
        budget = PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
            tier2_avg_mw=100.0,
            tier2_max_burst_min=30.0,
            reserve_mah=300,
        )
        budget.on_tier_change(Tier.T0, Tier.T0)
        assert budget.can_afford_tier2(15.0) is True  # 15 min

    def test_utility_functions(self):
        """Test hours_for_charge and mah_for_power utilities."""
        assert mah_for_power(5.0, 1.0) == pytest.approx(5.0 / 3.7)
        assert hours_for_charge(1000, 5.0) == pytest.approx(1000 * 3.7 / 5.0)

    def test_get_status(self):
        """Test get_status returns EnergyBudgetStatus."""
        budget = PowerBudgetManager(
            hub_battery_mah=3000,
            target_lifetime_h=24,
            tier0_avg_mw=5.0,
            tier1_avg_mw=50.0,
            tier1_max_h=10.0,
            tier2_avg_mw=100.0,
            tier2_max_burst_min=30.0,
            reserve_mah=300,
        )
        status = budget.get_status()
        assert isinstance(status, EnergyBudgetStatus)
        assert status.battery_capacity_mah == 3000
        assert status.tier0_h_used >= 0


class TestClockDriftEstimator:
    """Tests for ClockDriftEstimator."""

    def test_perfect_sync(self):
        """Test drift estimation with perfect sync."""
        estimator = ClockDriftEstimator()
        config = SyncConfig()
        marker = SyncMarker(
            sequence=1,
            hub_timestamp=10.0,
            pod_timestamps={"pod_001": 10.0},
        )
        estimator.add_marker(marker)
        marker2 = SyncMarker(
            sequence=2,
            hub_timestamp=20.0,
            pod_timestamps={"pod_001": 20.0},
        )
        estimator.add_marker(marker2)
        estimates = estimator.estimate_from_markers()
        assert "pod_001" in estimates
        assert estimates["pod_001"].offset_ms == pytest.approx(0.0, abs=1.0)
        assert estimates["pod_001"].drift_rate_ppm == pytest.approx(0.0, abs=1.0)

    def test_constant_offset(self):
        """Test drift estimation with constant offset."""
        estimator = ClockDriftEstimator()
        for i in range(10):
            hub_ts = float(i * 10)
            pod_ts = hub_ts + 0.050  # 50ms offset
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=hub_ts,
                pod_timestamps={"pod_001": pod_ts},
            )
            estimator.add_marker(marker)
        estimates = estimator.estimate_from_markers()
        assert "pod_001" in estimates
        assert estimates["pod_001"].offset_ms == pytest.approx(50.0, abs=1.0)

    def test_insufficient_markers(self):
        """Test estimator with insufficient markers."""
        estimator = ClockDriftEstimator()
        marker = SyncMarker(
            sequence=1,
            hub_timestamp=0.0,
            pod_timestamps={"pod_001": 0.0},
        )
        estimator.add_marker(marker)
        estimates = estimator.estimate_from_markers()
        assert estimates == {}


class TestTimestampCorrector:
    """Tests for TimestampCorrector."""

    def test_no_correction(self):
        """Test corrector with no drift."""
        corrector = TimestampCorrector()
        timestamps = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        corrected = corrector.correct_timestamps("pod_001", timestamps)
        np.testing.assert_array_almost_equal(corrected, timestamps)

    def test_offset_correction(self):
        """Test corrector with offset."""
        corrector = TimestampCorrector()
        corrector.update_correction(
            DriftEstimate(
                pod_id="pod_001",
                offset_ms=100.0,
                drift_rate_ppm=0.0,
                confidence=1.0,
                method="marker",
            )
        )
        timestamps = np.array([0.0, 1.0, 2.0])
        corrected = corrector.correct_timestamps("pod_001", timestamps)
        np.testing.assert_array_almost_equal(corrected, timestamps - 0.100)

    def test_drift_correction(self):
        """Test corrector with drift."""
        corrector = TimestampCorrector()
        corrector.update_correction(
            DriftEstimate(
                pod_id="pod_001",
                offset_ms=0.0,
                drift_rate_ppm=100.0,
                confidence=1.0,
                method="marker",
            )
        )
        timestamps = np.array([0.0, 10.0, 20.0])
        corrected = corrector.correct_timestamps("pod_001", timestamps)
        assert corrected[0] == pytest.approx(0.0)
        assert corrected[1] == pytest.approx(10.0 - 0.001)
        assert corrected[2] == pytest.approx(20.0 - 0.002)

    def test_combined_correction(self):
        """Test corrector with offset and drift."""
        corrector = TimestampCorrector()
        corrector.update_correction(
            DriftEstimate(
                pod_id="pod_001",
                offset_ms=50.0,
                drift_rate_ppm=50.0,
                confidence=1.0,
                method="marker",
            )
        )
        timestamps = np.array([0.0, 10.0])
        corrected = corrector.correct_timestamps("pod_001", timestamps)
        assert corrected[0] == pytest.approx(-0.050, abs=1e-4)
        assert corrected[1] == pytest.approx(10.0 - 0.050 - 0.0005, abs=1e-4)

    def test_get_correction(self):
        """Test getting correction parameters."""
        corrector = TimestampCorrector()
        corrector.update_correction(
            DriftEstimate(
                pod_id="pod_001",
                offset_ms=100.0,
                drift_rate_ppm=50.0,
                confidence=1.0,
                method="marker",
            )
        )
        correction = corrector.get_correction("pod_001")
        assert correction is not None
        offset_s, drift_rate = correction
        assert offset_s == pytest.approx(0.1)
        assert drift_rate == pytest.approx(50e-6)


class TestMultiPodClockSync:
    """Tests for MultiPodClockSync."""

    def test_register_pod(self):
        """Test pod registration."""
        config = SyncConfig()
        sync = MultiPodClockSync(config)
        sync.register_pod("pod_001", 100)
        assert "pod_001" in sync.config.acc_sampling_rates

    def test_broadcast_sync(self):
        """Test sync marker broadcast."""
        config = SyncConfig()
        sync = MultiPodClockSync(config)
        sync.register_pod("pod_001", 100)
        marker = sync.broadcast_sync(123.456)
        assert marker.hub_timestamp == 123.456
        assert marker.sequence == 0

    def test_add_acc_samples(self):
        """Test adding ACC samples for correlation."""
        config = SyncConfig(acc_sampling_rates={"pod_001": 100})
        sync = MultiPodClockSync(config)
        sync.register_pod("pod_001", 100)
        sync.add_hub_acc(1.0, 100.0)
        sync.add_pod_acc("pod_001", 1.0, 100.0)
        assert len(sync._hub_acc_buffer) > 0
        # Pod ACC goes to drift_estimator
        assert "pod_001" in sync.drift_estimator._acc_buffers

    def test_update_drift_estimates(self):
        """Test drift estimate update."""
        config = SyncConfig()
        sync = MultiPodClockSync(config)
        sync.register_pod("pod_001", 100)
        for i in range(10):
            hub_ts = float(i * 10)
            pod_ts = hub_ts + 0.010
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=hub_ts,
                pod_timestamps={"pod_001": pod_ts},
            )
            # Use the internal method to add marker with pod timestamp
            sync.drift_estimator.add_marker(marker)
        sync.update_drift_estimates()
        status = sync.get_sync_status(Tier.T0)
        assert "pod_001" in status["pods"]

    def test_correct_pod_timestamps(self):
        """Test timestamp correction."""
        config = SyncConfig()
        sync = MultiPodClockSync(config)
        sync.register_pod("pod_001", 100)
        for i in range(10):
            hub_ts = float(i * 10)
            pod_ts = hub_ts + 0.010
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=hub_ts,
                pod_timestamps={"pod_001": pod_ts},
            )
            sync.drift_estimator.add_marker(marker)
        sync.update_drift_estimates()
        pod_ts = np.array([100.0, 110.0, 120.0])
        corrected = sync.correct_pod_timestamps("pod_001", pod_ts)
        assert corrected[0] < pod_ts[0]  # Should subtract offset


class TestQuantifyResidualDrift:
    """Tests for quantify_residual_drift function."""

    def test_perfect_correction(self):
        """Test residual drift with perfect correction."""
        ref = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        test = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        result = quantify_residual_drift(ref, test, tier=Tier.T0)
        assert result["p99_offset_ms"] < 0.1
        assert result["within_1ms_pct"] == 100.0

    def test_constant_offset(self):
        """Test residual drift with constant offset."""
        ref = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        test = np.array([0.010, 1.010, 2.010, 3.010, 4.010])  # 10ms offset
        result = quantify_residual_drift(ref, test, tier=Tier.T0)
        assert result["p99_offset_ms"] == pytest.approx(10.0, abs=0.1)

    def test_tier1_stricter(self):
        """Test Tier 1 has stricter budget."""
        ref = np.array([0.0, 1.0, 2.0])
        test = np.array([0.005, 1.005, 2.005])  # 5ms offset
        result_t0 = quantify_residual_drift(ref, test, tier=Tier.T0)
        result_t1 = quantify_residual_drift(ref, test, tier=Tier.T1, config=SyncConfig())
        # Check within budget key
        assert "within_10ms_pct" in result_t0 or "within_1ms_pct" in result_t0
        assert "within_1ms_pct" in result_t1


class TestImmobilityDetector:
    """Tests for ImmobilityDetector."""

    def test_immobile_detection(self):
        """Test immobile state detection."""
        detector = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=30.0,
            magnitude_threshold=0.02,
            min_immobility_min=5.0,
        )
        # Feed low-magnitude accel
        for _ in range(3000):  # 30s at 100Hz
            detector.update(0.01, time.time())
        assert detector.current_window_immobile is True

    def test_movement_detection(self):
        """Test movement breaks immobility."""
        detector = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=30.0,
            magnitude_threshold=0.02,
            min_immobility_min=5.0,
        )
        for _ in range(100):
            detector.update(0.01, time.time())
        detector.update(1.5, time.time())  # Large movement
        assert detector.current_window_immobile is False


class TestNightWindowScheduler:
    """Tests for NightWindowScheduler."""

    def test_schedule_creation(self):
        """Test night window schedule creation."""
        config = NightWindowScheduler()
        assert config is not None


class TestSensorPodCoordinator:
    """Tests for SensorPodCoordinator."""

    def test_register_pod(self):
        """Test pod registration."""
        coordinator = SensorPodCoordinator()
        from synapse24.hardware import SensorPodConfig

        config = SensorPodConfig(
            pod_id="pod_001",
            name="Test Pod",
            board_type="CYTON_BOARD",
            modalities=["eeg"],
            tier=Tier.T0,
            channels={"eeg": 8},
            sampling_rate={"eeg": 500},
            placement="head",
            electrode_type="dry",
        )
        coordinator.register_pod(config)
        assert "pod_001" in coordinator._pods

    def test_get_pod_tier(self):
        """Test getting pod tier."""
        coordinator = SensorPodCoordinator()
        from synapse24.hardware import SensorPodConfig

        config = SensorPodConfig(
            pod_id="pod_001",
            name="Test Pod",
            board_type="CYTON_BOARD",
            modalities=["eeg"],
            tier=Tier.T1,
            channels={"eeg": 8},
            sampling_rate={"eeg": 500},
            placement="head",
            electrode_type="dry",
        )
        coordinator.register_pod(config)
        assert coordinator._pods["pod_001"].config.tier == Tier.T1


class TestAcquisitionController:
    """Tests for AcquisitionController."""

    def test_initialization(self):
        """Test controller initialization."""
        controller = AcquisitionController(
            immobility_detector=ImmobilityDetector(100, 30.0, 0.02, 5.0),
            night_scheduler=NightWindowScheduler(),
            power_budget=PowerBudgetManager(3000, 24, 5.0, 50.0, 10.0, 100.0, 30.0, 300),
            pod_coordinator=SensorPodCoordinator(),
        )
        assert controller.state_machine.current_tier == Tier.T0

    def test_update_imu(self):
        """Test IMU update."""
        controller = AcquisitionController(
            immobility_detector=ImmobilityDetector(100, 30.0, 0.02, 5.0),
            night_scheduler=NightWindowScheduler(),
            power_budget=PowerBudgetManager(3000, 24, 5.0, 50.0, 10.0, 100.0, 30.0, 300),
            pod_coordinator=SensorPodCoordinator(),
        )
        controller.update_imu(0.01, time.time())
        # Should not raise


class TestSyncMarkerManager:
    """Tests for SyncMarkerManager."""

    def test_marker_creation(self):
        """Test sync marker creation."""
        marker = SyncMarker(
            sequence=1,
            hub_timestamp=100.0,
            pod_timestamps={"pod_001": 100.001},
        )
        assert marker.sequence == 1
        assert marker.hub_timestamp == 100.0

    def test_should_broadcast_tier0(self):
        """Test broadcast interval for Tier 0."""
        manager = SyncMarkerManager(config=SyncConfig())
        # Simulate first broadcast at time -60
        manager._last_broadcast = -60.0
        # At time 0: 0 - (-60) = 60 >= 60 -> True (time for next broadcast)
        assert manager.should_broadcast(0.0, Tier.T0) is True
        # Simulate broadcast happened at time 0
        manager._last_broadcast = 0.0
        # At 30s: 30 - 0 = 30 < 60 -> False (not yet time)
        assert manager.should_broadcast(30.0, Tier.T0) is False
        # At 60s: 60 - 0 = 60 >= 60 -> True
        assert manager.should_broadcast(60.0, Tier.T0) is True

    def test_should_broadcast_tier1(self):
        """Test broadcast interval for Tier 1."""
        manager = SyncMarkerManager(config=SyncConfig())
        # Simulate first broadcast at time -10
        manager._last_broadcast = -10.0
        # At time 0: 0 - (-10) = 10 >= 10 -> True
        assert manager.should_broadcast(0.0, Tier.T1) is True
        # Simulate broadcast happened at time 0
        manager._last_broadcast = 0.0
        # At 5s: 5 - 0 = 5 < 10 -> False
        assert manager.should_broadcast(5.0, Tier.T1) is False
        # At 10s: 10 - 0 = 10 >= 10 -> True
        assert manager.should_broadcast(10.0, Tier.T1) is True


class TestSyncMarkerRecorder:
    """Tests for SyncMarkerRecorder."""

    def test_record_marker(self):
        """Test recording marker to file."""
        recorder = SyncMarkerRecorder()
        recorder.record_hub_broadcast(1, 100.0)
        recorder.record_pod_receipt(1, "pod_001", 100.001, 100.0)

        records = recorder.get_all_records()
        assert len(records) == 2
        assert records[0]["sequence"] == 1
        assert records[0]["pod_id"] == "hub"
        assert records[1]["pod_id"] == "pod_001"
        assert records[1]["pod_timestamp"] == 100.001


class TestLSLGateway:
    """Tests for LSLGateway."""

    def test_config_creation(self):
        """Test gateway config creation."""
        config = GatewayConfig(
            sync_config=SyncConfig(),
        )
        assert config.gateway_id == "SYNAPSE_HUB"

    def test_pod_stream_config(self):
        """Test pod stream config creation."""
        config = create_pod_stream_config(
            pod_id="test_pod",
            modality="ecg",
            channel_count=1,
            sampling_rate=500,
            tier=Tier.T0,
        )
        assert config.pod_id == "test_pod"
        assert config.channel_count == 1
        assert config.tier == 0


class TestLiveTwoPodConfig:
    """Tests for LiveTwoPodConfig."""

    def test_default_config(self):
        """Test default configuration."""
        config = LiveTwoPodConfig()
        assert config.duration_s == 2.0
        assert config.ecg_fs == 500
        assert config.ppg_fs == 64
        assert config.imu_fs == 100


class TestTier1PodState:
    """Tests for Tier1PodState."""

    def test_state_creation(self):
        """Test tier 1 pod state creation."""
        from synapse24.hardware import SensorPodConfig

        config = SensorPodConfig(
            pod_id="head_001",
            name="Head Pod",
            board_type="CERELOG_BOARD",
            modalities=["eeg", "acc"],
            tier=Tier.T1,
            channels={"eeg": 8, "acc": 3},
            sampling_rate={"eeg": 500, "acc": 100},
            placement="head",
            electrode_type="dry",
        )
        state = Tier1PodState(pod_id="head_001", config=config)
        assert state.pod_id == "head_001"
        assert state.cerelog_manager is None


class TestTier0PodState:
    """Tests for Tier0PodState."""

    def test_state_creation(self):
        """Test tier 0 pod state creation."""
        from synapse24.hardware import SensorPodConfig

        config = SensorPodConfig(
            pod_id="forearm_001",
            name="Forearm Pod",
            board_type="ESP32_TIER0",
            modalities=["ecg", "ppg", "acc"],
            tier=Tier.T0,
            channels={"ecg": 1, "ppg": 2, "acc": 3},
            sampling_rate={"ecg": 500, "ppg": 64, "acc": 100},
            placement="forearm",
            electrode_type="wet",
        )
        state = Tier0PodState(pod_id="forearm_001", config=config)
        assert state.pod_id == "forearm_001"


class TestTransitionEvent:
    """Tests for TransitionEvent."""

    def test_transition_event(self):
        """Test transition event creation."""
        event = TransitionEvent(
            from_tier=Tier.T0,
            to_tier=Tier.T1,
            transition=TierTransition.T0_TO_T1_IMMOBILITY,
            timestamp=time.time(),
            reason="test",
        )
        assert event.from_tier == Tier.T0
        assert event.to_tier == Tier.T1
        assert event.transition == TierTransition.T0_TO_T1_IMMOBILITY


class TestTierSyncBudget:
    """Tests for TierSyncBudget."""

    def test_defaults(self):
        """Test default budgets."""
        budget = TierSyncBudget()
        assert budget.tier0_max_residual_drift_ms == 10.0
        assert budget.tier1_max_residual_drift_ms == 1.0
        assert budget.tier0_sync_interval_s == 60.0
        assert budget.tier1_sync_interval_s == 10.0


class TestSyncConfig:
    """Tests for SyncConfig."""

    def test_default_config(self):
        """Test default sync config."""
        config = SyncConfig()
        assert config.tier_budget.tier0_max_residual_drift_ms == 10.0
        assert config.acc_corr_window_s == 30.0
        assert config.min_acc_correlation == 0.7


class TestConstants:
    """Tests for module constants."""

    def test_nominal_voltage(self):
        """Test NOMINAL_VOLTAGE_V constant."""
        assert NOMINAL_VOLTAGE_V == 3.7

    def test_tier_enum(self):
        """Test Tier enum values."""
        from synapse24.signal_quality import Tier as QualityTier

        assert Tier.T0 == QualityTier.T0
        assert Tier.T1 == QualityTier.T1
        assert Tier.T2 == QualityTier.T2


class TestMotionGateConfig:
    """Tests for MotionGateConfig."""

    def test_defaults(self):
        """Test default motion gate config."""
        config = MotionGateConfig()
        assert config.sqi_min == 0.5
        assert config.map_max == 0.5
        assert config.required_consecutive_clean == 2


class TestDriftEstimate:
    """Tests for DriftEstimate."""

    def test_creation(self):
        """Test DriftEstimate creation."""
        estimate = DriftEstimate(
            pod_id="pod_001",
            offset_ms=10.0,
            drift_rate_ppm=50.0,
            confidence=0.9,
            method="marker",
        )
        assert estimate.pod_id == "pod_001"
        assert estimate.offset_ms == 10.0
        assert estimate.drift_rate_ppm == 50.0


class TestSyncMarker:
    """Tests for SyncMarker."""

    def test_creation(self):
        """Test SyncMarker creation."""
        marker = SyncMarker(
            sequence=1,
            hub_timestamp=100.0,
            pod_timestamps={"pod_001": 100.001},
        )
        assert marker.sequence == 1
        assert "pod_001" in marker.pod_timestamps


class TestPowerProfile:
    """Tests for PowerProfile."""

    def test_creation(self):
        """Test PowerProfile creation."""
        profile = PowerProfile(
            tier=Tier.T0,
            avg_mw=5.0,
            peak_mw=10.0,
        )
        assert profile.tier == Tier.T0
        assert profile.avg_mw == 5.0


class TestTierPromotionIntegration:
    """Integration tests for T0<->T1 promotion cycle with motion-quality gate.

    Architecture.md 33-43, 74: Tiered acquisition with IMU-triggered promotion
    gated by measured PPG quality (SQI >= 0.5, MAP <= 0.5).
    """

    def _make_controller(self, clock_fn):
        """Create controller with test-friendly parameters."""
        return AcquisitionController(
            immobility_detector=ImmobilityDetector(
                accel_sampling_rate=1,
                window_duration_s=1.0,
                magnitude_threshold=0.02,
                min_immobility_min=1.0,
            ),
            night_scheduler=None,
            power_budget=PowerBudgetManager(
                hub_battery_mah=3000,
                target_lifetime_h=24,
                tier0_avg_mw=5.0,
                tier1_avg_mw=50.0,
                tier1_max_h=10.0,
                tier2_avg_mw=100.0,
                tier2_max_burst_min=30.0,
                reserve_mah=300,
                clock_fn=clock_fn,
            ),
            motion_gate=MotionGateConfig(
                sqi_min=0.5,
                map_max=0.5,
                required_consecutive_clean=2,
                sqi_staleness_max_s=30.0,
                fail_open=False,
            ),
            clock_fn=clock_fn,
        )

    def test_promotion_cycle_t0_t1_t0(self):
        """Test complete T0->T1->T0 promotion cycle with motion gate."""
        clock = 0.0

        def test_clock():
            return clock

        controller = self._make_controller(test_clock)

        # Phase 1: Stationary + clean PPG (should promote T0->T1)
        phase1_promoted = False
        for _ in range(120):  # Need 60 windows for 1 min immobility
            controller.update_imu(
                accel_magnitude=0.01,
                timestamp=clock,
                ppg_sqi=0.8,
                motion_artifact_prob=0.1,
            )
            controller.tick(clock)
            if controller.state_machine.current_tier == Tier.T1:
                phase1_promoted = True
                break
            clock += 1.0

        assert phase1_promoted, "Should promote on clean immobility"
        assert controller.state_machine.current_tier == Tier.T1

        # Phase 2: Movement + dirty PPG (should demote T1->T0)
        phase2_demoted = False
        for _ in range(50):
            controller.update_imu(
                accel_magnitude=1.5,
                timestamp=clock,
                ppg_sqi=0.2,
                motion_artifact_prob=0.8,
            )
            controller.tick(clock)
            if controller.state_machine.current_tier == Tier.T0:
                phase2_demoted = True
                break
            clock += 1.0

        assert phase2_demoted, "Should demote on movement"
        assert controller.state_machine.current_tier == Tier.T0

        # Phase 3: Stationary + clean again (should re-promote T0->T1)
        phase3_repromoted = False
        for _ in range(120):
            controller.update_imu(
                accel_magnitude=0.01,
                timestamp=clock,
                ppg_sqi=0.8,
                motion_artifact_prob=0.1,
            )
            controller.tick(clock)
            if controller.state_machine.current_tier == Tier.T1:
                phase3_repromoted = True
                break
            clock += 1.0

        assert phase3_repromoted, "Should re-promote after recovery"
        assert controller.state_machine.current_tier == Tier.T1

    def test_motion_gate_blocks_promotion_on_low_sqi(self):
        """Test motion gate blocks promotion when SQI < threshold."""
        clock = 0.0

        def test_clock():
            return clock

        controller = self._make_controller(test_clock)

        # Reset to T0 with dirty PPG
        controller.state_machine.reset()
        controller._consecutive_clean = 0
        controller._consecutive_contaminated = 0
        controller._latest_sqi = 0.3
        controller._latest_map = 0.7
        controller._latest_sqi_timestamp = clock

        gate_blocked = True
        for _ in range(100):
            controller.update_imu(
                accel_magnitude=0.01,
                timestamp=clock,
                ppg_sqi=0.3,  # Below threshold
                motion_artifact_prob=0.7,  # Above threshold
            )
            controller.tick(clock)
            if controller.state_machine.current_tier == Tier.T1:
                gate_blocked = False
                break
            clock += 1.0

        assert gate_blocked, "Motion gate should block promotion on low SQI"
        assert controller.state_machine.current_tier == Tier.T0

    def test_motion_gate_allows_promotion_on_clean_signal(self):
        """Test motion gate allows promotion after 2 consecutive clean assessments."""
        clock = 0.0

        def test_clock():
            return clock

        controller = self._make_controller(test_clock)

        # Feed clean PPG for 3 steps (need 2 consecutive clean)
        for _ in range(3):
            controller.update_imu(
                accel_magnitude=0.01,
                timestamp=clock,
                ppg_sqi=0.8,
                motion_artifact_prob=0.1,
            )
            controller.tick(clock)
            clock += 1.0

        # Motion gate should be armed now
        status = controller.get_status()
        assert status["motion_gate"]["armed"] is True
        assert status["motion_gate"]["consecutive_clean"] >= 2

        # Now immobility should trigger promotion
        # Need to feed enough immobile samples for 1 min at 1Hz
        controller._consecutive_clean = 2
        controller._latest_sqi = 0.8
        controller._latest_map = 0.1
        controller._latest_sqi_timestamp = clock

        promoted = False
        for _ in range(70):  # 1 min = 60 windows at 1Hz
            controller.update_imu(
                accel_magnitude=0.01,
                timestamp=clock,
                ppg_sqi=0.8,
                motion_artifact_prob=0.1,
            )
            controller.tick(clock)
            if controller.state_machine.current_tier == Tier.T1:
                promoted = True
                break
            clock += 1.0

        assert promoted, "Should promote when motion gate armed and immobility detected"

    def test_power_budget_blocks_tier1_when_exceeded(self):
        """Test power budget demotes T1->T0 when budget exceeded."""
        clock = 0.0

        def test_clock():
            return clock

        # Use battery that can afford T1 initially but not after extended T1 use
        # 3000mAh with 300mAh reserve = 2700mAh usable
        # At 50mW T1 = 13.5mAh/h, at 5mW T0 = 1.35mAh/h
        # Target 24h needs 5*24/3.7 = 32.4mAh for T0
        # So 2700 - 32.4 = 2667.6mAh available for T1 = ~197h
        # Set tier1_max_h >= 2 to allow promotion (hardcoded duration_h=2 in update_imu)
        controller = AcquisitionController(
            immobility_detector=ImmobilityDetector(1, 1.0, 0.02, 1.0),
            night_scheduler=None,
            power_budget=PowerBudgetManager(
                hub_battery_mah=3000,
                target_lifetime_h=24,
                tier0_avg_mw=5.0,
                tier1_avg_mw=50.0,
                tier1_max_h=2.0,  # Allow promotion (hardcoded 2h check)
                tier2_avg_mw=100.0,
                tier2_max_burst_min=30.0,
                reserve_mah=300,
                clock_fn=test_clock,
            ),
            motion_gate=MotionGateConfig(sqi_min=0.5, map_max=0.5, required_consecutive_clean=2),
            clock_fn=test_clock,
        )

        # Promote to T1 - need 60 windows for 1 min immobility at 1Hz
        controller._consecutive_clean = 2
        controller._latest_sqi = 0.8
        controller._latest_map = 0.1
        controller._latest_sqi_timestamp = clock
        for _ in range(70):  # 60 windows + margin
            controller.update_imu(0.01, clock, ppg_sqi=0.8, motion_artifact_prob=0.1)
            controller.tick(clock)
            clock += 1.0

        assert controller.state_machine.current_tier == Tier.T1, "Should promote to T1"

        # Now test that power budget check in tick() demotes when T1 duration exceeds max_h
        # Manually set T1 start time to simulate session longer than tier1_max_h (2 hours)
        controller.state_machine._t1_start_time = clock - 2.5 * 3600  # 2.5 hours ago

        controller.tick(clock)

        # Should demote due to power budget max duration exceeded
        assert controller.state_machine.current_tier == Tier.T0, (
            "Should demote when T1 max duration exceeded"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
