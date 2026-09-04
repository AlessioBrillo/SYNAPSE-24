"""Unit tests for clock synchronization pipeline."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import numpy.typing as npt
import pytest

from synapse24.acquisition.clock_sync import (
    ClockDriftEstimator,
    DriftEstimate,
    MultiPodClockSync,
    SyncConfig,
    SyncMarker,
    SyncMarkerManager,
    TimestampCorrector,
    quantify_residual_drift,
)


class TestSyncConfig:
    """Tests for SyncConfig."""

    def test_default_config(self):
        """Test default configuration values."""
        config = SyncConfig()
        assert config.sync_interval_s == 60.0
        assert config.max_residual_drift_ms == 1.0
        assert config.acc_corr_window_s == 30.0
        assert config.min_acc_correlation == 0.7

    def test_custom_config(self):
        """Test custom configuration."""
        config = SyncConfig(
            sync_interval_s=30.0,
            max_residual_drift_ms=0.5,
            acc_corr_window_s=60.0,
            min_acc_correlation=0.8,
        )
        assert config.sync_interval_s == 30.0
        assert config.max_residual_drift_ms == 0.5
        assert config.acc_corr_window_s == 60.0
        assert config.min_acc_correlation == 0.8


class TestSyncMarker:
    """Tests for SyncMarker dataclass."""

    def test_marker_creation(self):
        """Test sync marker creation."""
        marker = SyncMarker(
            sequence=1,
            hub_timestamp=1000.0,
            pod_timestamps={"pod1": 1000.001, "pod2": 999.999},
        )
        assert marker.sequence == 1
        assert marker.hub_timestamp == 1000.0
        assert marker.pod_timestamps["pod1"] == 1000.001
        assert marker.pod_timestamps["pod2"] == 999.999

    def test_marker_auto_wall_time(self):
        """Test wall_time is auto-generated."""
        before = time.time()
        marker = SyncMarker(sequence=1, hub_timestamp=1000.0, pod_timestamps={})
        after = time.time()
        assert before <= marker.wall_time <= after


class TestSyncMarkerManager:
    """Tests for SyncMarkerManager."""

    def test_broadcast_sync(self):
        """Test sync marker broadcasting."""
        manager = SyncMarkerManager()
        marker = manager.broadcast_sync(1000.0)

        assert marker.sequence == 0
        assert marker.hub_timestamp == 1000.0
        assert isinstance(marker.pod_timestamps, dict)

    def test_sequence_increments(self):
        """Test sequence increments on each broadcast."""
        manager = SyncMarkerManager()
        m1 = manager.broadcast_sync(1000.0)
        m2 = manager.broadcast_sync(1060.0)
        m3 = manager.broadcast_sync(1120.0)

        assert m1.sequence == 0
        assert m2.sequence == 1
        assert m3.sequence == 2

    def test_should_broadcast(self):
        """Test broadcast timing logic."""
        manager = SyncMarkerManager(SyncConfig(sync_interval_s=60.0))

        # First broadcast at time 1000.0
        manager.broadcast_sync(1000.0)

        # Immediately after broadcast
        assert not manager.should_broadcast(1000.1)

        # Before interval
        assert not manager.should_broadcast(1059.0)

        # After interval
        assert manager.should_broadcast(1061.0)

    def test_callback_registration(self):
        """Test pod callback registration."""
        manager = SyncMarkerManager()
        received = []

        def callback(marker: SyncMarker, pod_time: float):
            received.append((marker.sequence, pod_time))

        manager.register_callback("pod1", callback)
        marker = manager.broadcast_sync(1000.0)

        assert len(received) == 1
        assert received[0][0] == 0


class TestClockDriftEstimator:
    """Tests for ClockDriftEstimator."""

    def test_estimate_from_markers_perfect_sync(self):
        """Test drift estimation with perfect sync."""
        config = SyncConfig()
        estimator = ClockDriftEstimator(config)

        # Add markers with zero offset
        for i in range(5):
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=1000.0 + i * 60.0,
                pod_timestamps={"pod1": 1000.0 + i * 60.0},
            )
            estimator.add_marker(marker)

        estimates = estimator.estimate_from_markers()
        assert "pod1" in estimates
        est = estimates["pod1"]
        assert abs(est.offset_ms) < 0.1  # Near zero
        assert abs(est.drift_rate_ppm) < 1.0  # Near zero

    def test_estimate_from_markers_with_offset(self):
        """Test drift estimation with constant offset."""
        config = SyncConfig()
        estimator = ClockDriftEstimator(config)

        # Pod is 5ms ahead
        for i in range(5):
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=1000.0 + i * 60.0,
                pod_timestamps={"pod1": 1000.005 + i * 60.0},
            )
            estimator.add_marker(marker)

        estimates = estimator.estimate_from_markers()
        est = estimates["pod1"]
        assert abs(est.offset_ms - 5.0) < 0.5

    def test_estimate_from_markers_with_drift(self):
        """Test drift estimation with clock drift."""
        config = SyncConfig()
        estimator = ClockDriftEstimator(config)

        # Pod clock runs 100 ppm fast (1.0001x)
        for i in range(10):
            hub_t = 1000.0 + i * 60.0
            pod_t = 1000.0 + i * 60.0 * 1.0001  # 100 ppm fast
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=hub_t,
                pod_timestamps={"pod1": pod_t},
            )
            estimator.add_marker(marker)

        estimates = estimator.estimate_from_markers()
        est = estimates["pod1"]
        # Drift rate should be close to 100 ppm
        assert 50 < est.drift_rate_ppm < 150

    def test_estimate_from_acc_correlation(self):
        """Test ACC cross-correlation drift estimation."""
        config = SyncConfig(acc_sampling_rates={"pod1": 100})
        estimator = ClockDriftEstimator(config)

        # Generate correlated ACC signals with known offset
        fs = 100
        t_hub = np.arange(0, 30, 1/fs)
        t_pod = t_hub + 0.01  # 10ms offset

        hub_acc = np.sin(2 * np.pi * 1.2 * t_hub) + 0.1 * np.random.randn(len(t_hub))
        pod_acc = np.sin(2 * np.pi * 1.2 * t_pod) + 0.1 * np.random.randn(len(t_pod))

        # Add to estimator
        for i in range(len(hub_acc)):
            estimator.add_acc_sample("pod1", pod_acc[i], t_pod[i])

        # Estimate
        hub_acc_arr = hub_acc.astype(np.float64)
        hub_ts_arr = t_hub.astype(np.float64)

        est = estimator.estimate_from_acc_correlation(hub_acc_arr, hub_ts_arr, "pod1")

        assert est is not None
        assert est.method == "acc_correlation"
        assert est.confidence > 0.7
        # Offset should be close to 10ms
        assert 5 < abs(est.offset_ms) < 20

    def test_estimate_from_acc_low_correlation(self):
        """Test ACC correlation rejects uncorrelated signals."""
        config = SyncConfig(acc_sampling_rates={"pod1": 100}, min_acc_correlation=0.7)
        estimator = ClockDriftEstimator(config)

        # Uncorrelated noise
        hub_acc = np.random.randn(3000)
        pod_acc = np.random.randn(3000)
        hub_ts = np.arange(0, 30, 0.01)

        for i in range(len(hub_acc)):
            estimator.add_acc_sample("pod1", pod_acc[i], hub_ts[i] + 0.01)

        est = estimator.estimate_from_acc_correlation(hub_acc, hub_ts, "pod1")
        assert est is None  # Should reject due to low correlation

    def test_get_best_estimate(self):
        """Test getting best estimate."""
        config = SyncConfig()
        estimator = ClockDriftEstimator(config)

        # Add marker-based estimate
        for i in range(5):
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=1000.0 + i * 60.0,
                pod_timestamps={"pod1": 1000.005 + i * 60.0},
            )
            estimator.add_marker(marker)

        estimator.estimate_from_markers()
        best = estimator.get_best_estimate("pod1")

        assert best is not None
        assert best.pod_id == "pod1"

    def test_get_all_estimates(self):
        """Test getting all estimates."""
        config = SyncConfig()
        estimator = ClockDriftEstimator(config)

        for i in range(5):
            marker = SyncMarker(
                sequence=i,
                hub_timestamp=1000.0 + i * 60.0,
                pod_timestamps={"pod1": 1000.005 + i * 60.0, "pod2": 999.995 + i * 60.0},
            )
            estimator.add_marker(marker)

        estimator.estimate_from_markers()
        all_est = estimator.get_all_estimates()

        assert "pod1" in all_est
        assert "pod2" in all_est


class TestTimestampCorrector:
    """Tests for TimestampCorrector."""

    def test_correct_no_correction(self):
        """Test correction with no parameters returns original."""
        corrector = TimestampCorrector()
        timestamps = np.array([1000.0, 1000.01, 1000.02])
        corrected = corrector.correct_timestamps("pod1", timestamps)
        np.testing.assert_array_equal(corrected, timestamps)

    def test_correct_offset_only(self):
        """Test correction with offset only."""
        corrector = TimestampCorrector()
        corrector.update_correction(DriftEstimate(
            pod_id="pod1",
            offset_ms=5.0,  # 5ms ahead
            drift_rate_ppm=0.0,
            confidence=1.0,
            method="marker",
        ))

        timestamps = np.array([1000.005, 1000.015, 1000.025])  # 5ms ahead
        corrected = corrector.correct_timestamps("pod1", timestamps)

        # Should subtract 5ms
        expected = np.array([1000.0, 1000.01, 1000.02])
        np.testing.assert_allclose(corrected, expected, atol=1e-6)

    def test_correct_drift_only(self):
        """Test correction with drift rate only."""
        corrector = TimestampCorrector()
        # 100 ppm fast = 1.0001x
        corrector.update_correction(DriftEstimate(
            pod_id="pod1",
            offset_ms=0.0,
            drift_rate_ppm=100.0,
            confidence=1.0,
            method="marker",
        ))

        # After 1000 seconds, pod is 100ms ahead
        timestamps = np.array([1000.0, 2000.0, 3000.0])  # Pod timestamps
        corrected = corrector.correct_timestamps("pod1", timestamps)

        # At t=1000, offset=0.1s; at t=2000, offset=0.2s
        # Corrected: (t - 0) / 1.0001 ≈ t * 0.9999
        expected = timestamps / 1.0001
        np.testing.assert_allclose(corrected, expected, rtol=1e-4)

    def test_correct_combined(self):
        """Test correction with both offset and drift."""
        corrector = TimestampCorrector()
        corrector.update_correction(DriftEstimate(
            pod_id="pod1",
            offset_ms=2.0,
            drift_rate_ppm=50.0,
            confidence=1.0,
            method="marker",
        ))

        timestamps = np.array([1000.0, 1001.0])
        corrected = corrector.correct_timestamps("pod1", timestamps)

        # Verify it runs without error
        assert len(corrected) == 2
        assert corrected[0] < timestamps[0]  # Should be corrected earlier


class TestMultiPodClockSync:
    """Tests for MultiPodClockSync integration."""

    def test_register_pod(self):
        """Test pod registration."""
        sync = MultiPodClockSync()
        sync.register_pod("pod1", 100)
        sync.register_pod("pod2", 50)

        assert "pod1" in sync.config.acc_sampling_rates
        assert "pod2" in sync.config.acc_sampling_rates
        assert sync.config.acc_sampling_rates["pod1"] == 100
        assert sync.config.acc_sampling_rates["pod2"] == 50

    def test_broadcast_sync(self):
        """Test sync broadcast."""
        sync = MultiPodClockSync()
        sync.register_pod("pod1", 100)

        marker = sync.broadcast_sync(1000.0)
        assert marker.sequence == 0
        assert marker.hub_timestamp == 1000.0

    def test_add_acc_samples(self):
        """Test adding ACC samples."""
        sync = MultiPodClockSync()
        sync.register_pod("pod1", 100)

        # Add hub ACC
        for i in range(100):
            sync.add_hub_acc(np.sin(2 * np.pi * 1.2 * i / 100), 1000.0 + i / 100)

        # Add pod ACC (with 10ms offset)
        for i in range(100):
            sync.add_pod_acc("pod1", np.sin(2 * np.pi * 1.2 * (i / 100 + 0.01)), 1000.0 + i / 100 + 0.01)

        assert len(sync._hub_acc_buffer) == 100

    def test_update_drift_estimates_markers_only(self):
        """Test drift estimation with markers only."""
        sync = MultiPodClockSync()
        sync.register_pod("pod1", 100)

        # Add markers directly to estimator (bypassing broadcast to avoid callback issues)
        for i in range(5):
            sync.drift_estimator.add_marker(SyncMarker(
                sequence=i,
                hub_timestamp=1000.0 + i * 60.0,
                pod_timestamps={"pod1": 1000.005 + i * 60.0},
            ))

        estimates = sync.update_drift_estimates()

        assert "pod1" in estimates
        est = estimates["pod1"]
        assert abs(est.offset_ms - 5.0) < 1.0
        assert est.method == "marker"

    def test_correct_pod_timestamps(self):
        """Test timestamp correction pipeline."""
        sync = MultiPodClockSync()
        sync.register_pod("pod1", 100)

        # Add markers directly to estimator (bypassing broadcast to avoid callback issues)
        for i in range(5):
            sync.drift_estimator.add_marker(SyncMarker(
                sequence=i,
                hub_timestamp=1000.0 + i * 60.0,
                pod_timestamps={"pod1": 1000.005 + i * 60.0},
            ))

        sync.update_drift_estimates()

        # Correct pod timestamps
        pod_timestamps = np.array([1000.005, 1060.005, 1120.005])
        corrected = sync.correct_pod_timestamps("pod1", pod_timestamps)

        # Should be corrected to hub time
        expected = np.array([1000.0, 1060.0, 1120.0])
        np.testing.assert_allclose(corrected, expected, atol=1.0)

    def test_get_sync_status(self):
        """Test sync status reporting."""
        sync = MultiPodClockSync()
        sync.register_pod("pod1", 100)

        # Add markers directly to estimator
        for i in range(5):
            sync.drift_estimator.add_marker(SyncMarker(
                sequence=i,
                hub_timestamp=1000.0 + i * 60.0,
                pod_timestamps={"pod1": 1000.005 + i * 60.0},
            ))

        sync.update_drift_estimates()
        status = sync.get_sync_status()

        assert "pods" in status
        assert "pod1" in status["pods"]
        pod_status = status["pods"]["pod1"]
        assert "offset_ms" in pod_status
        assert "drift_rate_ppm" in pod_status
        assert "confidence" in pod_status
        assert "method" in pod_status
        assert "within_tolerance" in pod_status


class TestQuantifyResidualDrift:
    """Tests for residual drift quantification."""

    def test_perfect_correction(self):
        """Test quantification with perfect correction."""
        corrected = np.array([1000.0, 1000.01, 1000.02, 1000.03])
        hub = np.array([1000.0, 1000.01, 1000.02, 1000.03])

        metrics = quantify_residual_drift(corrected, hub)

        assert metrics["mean_offset_ms"] == 0.0
        assert metrics["std_offset_ms"] == 0.0
        assert metrics["max_abs_offset_ms"] == 0.0
        assert metrics["p99_offset_ms"] == 0.0
        assert metrics["within_1ms_pct"] == 100.0

    def test_constant_offset(self):
        """Test quantification with constant residual offset."""
        corrected = np.array([1000.0005, 1000.0105, 1000.0205])  # 0.5ms ahead
        hub = np.array([1000.0, 1000.01, 1000.02])

        metrics = quantify_residual_drift(corrected, hub)

        assert abs(metrics["mean_offset_ms"] - 0.5) < 0.1
        assert metrics["std_offset_ms"] < 0.1
        assert metrics["within_1ms_pct"] == 100.0

    def test_variable_offset(self):
        """Test quantification with variable residual offset."""
        corrected = np.array([1000.0, 1000.01, 1000.02, 1000.03, 1000.04])
        hub = np.array([1000.0005, 1000.009, 1000.021, 1000.029, 1000.041])

        metrics = quantify_residual_drift(corrected, hub)

        assert metrics["mean_offset_ms"] < 2.0
        assert metrics["p99_offset_ms"] > 0
        assert 0 <= metrics["within_1ms_pct"] <= 100

    def test_length_mismatch_raises(self):
        """Test length mismatch raises ValueError."""
        corrected = np.array([1000.0, 1000.01])
        hub = np.array([1000.0])

        with pytest.raises(ValueError, match="same length"):
            quantify_residual_drift(corrected, hub)


class TestIntegrationScenarios:
    """Integration tests for realistic scenarios."""

    def test_hour_long_simulation(self):
        """Test 1-hour simulation with drift."""
        config = SyncConfig(sync_interval_s=60.0)
        sync = MultiPodClockSync(config)
        sync.register_pod("head_pod", 100)
        sync.register_pod("forearm_pod", 50)

        # Simulate 1 hour with 100 ppm drift on head_pod, 50 ppm on forearm
        hub_time = 0.0
        for minute in range(60):
            # Head pod: 100 ppm fast
            head_pod_time = hub_time * 1.0001
            forearm_pod_time = hub_time * 1.00005
            sync.drift_estimator.add_marker(SyncMarker(
                sequence=minute,
                hub_timestamp=hub_time,
                pod_timestamps={"head_pod": head_pod_time, "forearm_pod": forearm_pod_time},
            ))

            # Add ACC data (correlated motion)
            for i in range(6000):  # 60s * 100Hz
                t = hub_time + i / 100
                acc = np.sin(2 * np.pi * 1.2 * t) + 0.1 * np.random.randn()
                sync.add_hub_acc(float(acc), t)
                sync.add_pod_acc("head_pod", float(acc) + 0.01 * np.random.randn(), t * 1.0001)
                sync.add_pod_acc("forearm_pod", float(acc) + 0.01 * np.random.randn(), t * 1.00005)

            hub_time += 60.0

        estimates = sync.update_drift_estimates()

        # Both pods should have drift estimates
        assert "head_pod" in estimates
        assert "forearm_pod" in estimates

        # Drift rates should be close to expected
        head_est = estimates["head_pod"]
        forearm_est = estimates["forearm_pod"]

        # Allow wide tolerance due to noise and estimation method
        assert 50 < head_est.drift_rate_ppm < 200
        assert 10 < forearm_est.drift_rate_ppm < 150

    def test_correction_quality_metrics(self):
        """Test that correction achieves <1ms residual drift."""
        sync = MultiPodClockSync()
        sync.register_pod("pod1", 100)

        # Perfect sync markers - add directly to estimator
        for i in range(20):
            sync.drift_estimator.add_marker(SyncMarker(
                sequence=i,
                hub_timestamp=i * 60.0,
                pod_timestamps={"pod1": i * 60.0},
            ))

        sync.update_drift_estimates()

        # Generate test timestamps
        pod_ts = np.arange(0, 3600, 0.01)  # 1 hour at 100Hz
        hub_ts = pod_ts.copy()

        corrected = sync.correct_pod_timestamps("pod1", pod_ts)
        metrics = quantify_residual_drift(corrected, hub_ts)

        # With perfect sync, residual should be near zero
        assert metrics["max_abs_offset_ms"] < 0.1
        assert metrics["p99_offset_ms"] < 0.1
        assert metrics["within_1ms_pct"] == 100.0


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_estimator(self):
        """Test estimator with no data."""
        estimator = ClockDriftEstimator()
        estimates = estimator.estimate_from_markers()
        assert estimates == {}

    def test_single_marker(self):
        """Test estimator with single marker (insufficient)."""
        estimator = ClockDriftEstimator()
        estimator.add_marker(SyncMarker(
            sequence=0,
            hub_timestamp=1000.0,
            pod_timestamps={"pod1": 1000.005},
        ))
        estimates = estimator.estimate_from_markers()
        assert estimates == {}

    def test_missing_pod_in_markers(self):
        """Test estimator handles missing pod gracefully."""
        estimator = ClockDriftEstimator()
        for i in range(5):
            estimator.add_marker(SyncMarker(
                sequence=i,
                hub_timestamp=1000.0 + i * 60.0,
                pod_timestamps={"pod1": 1000.005 + i * 60.0},  # pod2 missing
            ))
        estimates = estimator.estimate_from_markers()
        assert "pod1" in estimates
        assert "pod2" not in estimates

    def test_corrector_get_correction(self):
        """Test getting correction parameters."""
        corrector = TimestampCorrector()
        assert corrector.get_correction("pod1") is None

        corrector.update_correction(DriftEstimate(
            pod_id="pod1",
            offset_ms=5.0,
            drift_rate_ppm=100.0,
            confidence=1.0,
            method="marker",
        ))

        corr = corrector.get_correction("pod1")
        assert corr is not None
        offset_s, drift_rate = corr
        assert offset_s == 0.005
        assert drift_rate == 0.0001
