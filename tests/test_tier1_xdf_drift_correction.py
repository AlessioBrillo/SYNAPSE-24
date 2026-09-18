"""Test Tier 1 XDF export with drift correction (Architecture.md §92: <1ms residual).

Validates that MultiPodClockSync drift correction is applied before XDF write,
and that round-trip XDF verification confirms <1ms residual drift for Tier 1.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from synapse24.acquisition import (
    MultiPodClockSync,
    SyncConfig,
    SyncMarker,
)
from synapse24.acquisition.clock_sync import (
    TierSyncBudget,
    quantify_residual_drift,
)
from synapse24.signal_quality import Tier
from synapse24.utils import StreamConfig, validate_xdf, verify_xdf_roundtrip


def _assert_float(result: dict[str, Any], key: str, expected: float, tolerance: float = 1e-6) -> None:
    """Assert that a result key has a float value close to expected."""
    val = result[key]
    assert isinstance(val, (int, float)), f"{key} is not numeric: {val}"
    assert abs(float(val) - expected) < tolerance, f"{key}={val}, expected ~{expected}"


class TestTier1XDFDriftCorrection:
    """Test drift-corrected XDF export for Tier 1 acquisition."""

    def setup_method(self) -> None:
        """Create a test configuration with synthetic clock."""
        self.sync_config = SyncConfig()
        self.clock_sync = MultiPodClockSync(self.sync_config)
        self.clock_sync.register_pod("head_001", acc_sampling_rate=100)

        # Controllable clock for deterministic testing
        self._clock_state = {"now": 1000.0}

        def test_clock() -> float:
            return self._clock_state["now"]

        self.clock_sync.marker_manager._clock = test_clock

    def test_synthetic_drift_20ppm_injected_and_corrected(self) -> None:
        """Inject 20ppm drift, run sync markers, apply correction, verify <1ms residual."""
        pod_id = "head_001"

        # Create synthetic timestamps WITH 20ppm drift
        fs = 500
        duration_s = 30.0
        n_samples = int(duration_s * fs)

        # Hub timestamps (ideal clock) - absolute LSL clock domain
        hub_timestamps = np.arange(n_samples) / fs + 1000.0

        # Pod timestamps with 20ppm frequency drift: pod_time = hub_time * (1 + drift)
        # This is consistent with marker timestamps which use absolute time
        drift_rate_ppm = 20.0
        pod_timestamps = hub_timestamps * (1 + drift_rate_ppm * 1e-6)

        # Advance clock to start of acquisition
        self._clock_state["now"] = 1000.0

        # Simulate sync markers every 10s (Tier 1 interval) for 30s duration
        for i in range(4):  # 0s, 10s, 20s, 30s relative to 1000
            marker_time = 1000.0 + i * 10.0
            marker = self.clock_sync.broadcast_sync(marker_time)
            # Pod receives marker with its own clock (which has same frequency drift)
            pod_time = marker_time * (1 + drift_rate_ppm * 1e-6)
            marker.pod_timestamps[pod_id] = pod_time

        # Update drift estimates (marker-based linear regression)
        self.clock_sync.update_drift_estimates()

        # Verify drift estimate is reasonable
        estimates = self.clock_sync.drift_estimator.get_all_estimates()
        assert pod_id in estimates
        est = estimates[pod_id]
        assert abs(est.drift_rate_ppm - drift_rate_ppm) < 5.0  # within 5ppm
        assert est.method in ("marker", "combined")

        # Apply correction to pod timestamps
        corrected_timestamps = self.clock_sync.correct_pod_timestamps(pod_id, pod_timestamps)

        # Verify residual drift < 1ms (Tier 1 budget) - compare corrected vs hub
        result = quantify_residual_drift(corrected_timestamps, hub_timestamps, tier=Tier.T1, config=self.sync_config)
        _assert_float(result, "within_1ms_pct", 100.0)
        _assert_float(result, "max_abs_offset_ms", 0.0)
        assert result["tier_evaluated"] == "T1"
        _assert_float(result, "tolerance_ms", 1.0)

    def test_xdf_export_with_drift_correction(self) -> None:
        """Test XDF export with drift-corrected timestamps passes verification."""
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "test_drift_corrected.xdf"

            # Create synthetic EEG data with known drift
            fs = 500
            duration_s = 10.0
            n_samples = int(duration_s * fs)
            n_channels = 8

            # Hub timestamps (ideal)
            hub_timestamps = np.arange(n_samples) / fs + 1000.0
            # Pod timestamps with 15ppm drift
            drift_rate_ppm = 15.0
            pod_timestamps = hub_timestamps * (1 + drift_rate_ppm * 1e-6)

            # Generate synthetic EEG data
            np.random.seed(42)
            t = np.arange(n_samples) / fs
            eeg_data = np.zeros((n_channels, n_samples), dtype=np.float64)
            for ch in range(n_channels):
                alpha = 10 * np.sin(2 * np.pi * 10 * t)
                noise = np.random.randn(n_samples) * 5
                eeg_data[ch] = alpha + noise

            # Simulate sync markers
            for i in range(int(duration_s / 10) + 1):
                marker_time = 1000.0 + i * 10.0
                marker = self.clock_sync.broadcast_sync(marker_time)
                pod_time = marker_time * (1 + drift_rate_ppm * 1e-6)
                marker.pod_timestamps["head_001"] = pod_time

            self.clock_sync.update_drift_estimates()

            # Apply drift correction
            corrected_timestamps = self.clock_sync.correct_pod_timestamps("head_001", pod_timestamps)

            # Build streams for XDF
            streams = []
            streams.append(
                {
                    "name": "SYNAPSE_EEG",
                    "type": "EEG",
                    "data": eeg_data.T.astype(np.float32),
                    "timestamps": corrected_timestamps,
                    "sampling_rate": float(fs),
                    "channel_format": "float32",
                    "channel_names": [f"CH{i}" for i in range(n_channels)],
                    "channel_units": ["µV"] * n_channels,
                    "source_id": "test_source",
                }
            )

            # Write and verify XDF
            verify_report = verify_xdf_roundtrip(streams, output_path)
            assert verify_report["all_streams_valid"]
            assert verify_report["total_dropped"] == 0

            # Read back and verify residual drift in file
            summary = validate_xdf(output_path)
            eeg_stream = next(s for s in summary["streams"] if s["type"] == "EEG")
            recovered_ts = np.array(eeg_stream.get("time_stamps", []))

            if len(recovered_ts) > 0:
                hub_ts = np.linspace(corrected_timestamps[0], corrected_timestamps[-1], len(recovered_ts))
                drift_result = quantify_residual_drift(
                    recovered_ts, hub_ts, tier=Tier.T1, config=self.sync_config
                )
                _assert_float(drift_result, "within_1ms_pct", 100.0)

    def test_tier0_drift_tolerance_not_applied_to_tier1(self) -> None:
        """Verify Tier 0 tolerance (10ms) is NOT used for Tier 1 data."""
        fs = 500
        n_samples = 5000  # 10s

        # 5ms offset (passes Tier 0, fails Tier 1)
        hub_ts = np.linspace(1000.0, 1010.0, n_samples)
        pod_ts = hub_ts + 0.005  # 5ms offset

        # Tier 0: 10ms tolerance -> should pass
        result_t0 = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T0, config=self.sync_config)
        _assert_float(result_t0, "within_10ms_pct", 100.0)
        _assert_float(result_t0, "tolerance_ms", 10.0)

        # Tier 1: 1ms tolerance -> should FAIL
        result_t1 = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T1, config=self.sync_config)
        _assert_float(result_t1, "within_1ms_pct", 0.0)
        _assert_float(result_t1, "tolerance_ms", 1.0)

    def test_quantify_residual_drift_p99_metric(self) -> None:
        """Test P99 offset metric captures worst-case residual drift."""
        hub_ts = np.linspace(0, 60, 30000)  # 500Hz for 60s
        # Most samples at 0.5ms offset
        pod_ts = hub_ts + 0.0005
        # Add 5% outliers at 2ms (enough to push P99 above 1ms)
        n_outliers = int(len(hub_ts) * 0.05)
        outlier_indices = np.random.choice(len(hub_ts), n_outliers, replace=False)
        pod_ts[outlier_indices] += 0.0015  # additional 1.5ms = 2ms total

        result = quantify_residual_drift(pod_ts, hub_ts, tier=Tier.T1, config=self.sync_config)

        # ~95% within 1ms, P99 should be ~2ms
        _assert_float(result, "within_1ms_pct", 95.0, tolerance=5.0)
        _assert_float(result, "p99_offset_ms", 2.0, tolerance=1.0)
        _assert_float(result, "max_abs_offset_ms", 2.0, tolerance=1.0)


class TestTier1SyncIntegration:
    """Integration tests for Tier 1 sync with ACC cross-correlation."""

    def test_acc_cross_correlation_runs_without_error(self) -> None:
        """ACC cross-correlation runs and produces an estimate."""
        # Create config with proper parameters
        sync_config = SyncConfig(
            tier_budget=TierSyncBudget(
                tier0_max_residual_drift_ms=10.0,
                tier1_max_residual_drift_ms=1.0,
                tier0_sync_interval_s=60.0,
                tier1_sync_interval_s=10.0,
            ),
            acc_corr_window_s=30.0,
            min_acc_correlation=0.3,  # Lower threshold for synthetic test
        )

        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        # Add correlated ACC data with known time shift
        # Use a chirp signal for better cross-correlation peak
        hub_t = np.linspace(0, 30, 3000)
        hub_ts = 1000.0 + hub_t
        # Chirp from 0.5 to 5 Hz
        hub_acc = np.sin(2 * np.pi * (0.5 * hub_t + 0.075 * hub_t**2))

        # Pod signal at pod timestamps (shifted by 2ms)
        pod_t = hub_t + 0.002
        pod_ts = 1000.0 + pod_t
        pod_acc = np.sin(2 * np.pi * (0.5 * pod_t + 0.075 * pod_t**2))

        for i in range(len(hub_t)):
            clock_sync.add_hub_acc(float(hub_acc[i]), float(hub_ts[i]))
            clock_sync.add_pod_acc("head_001", float(pod_acc[i]), float(pod_ts[i]))

        # Update with ACC correlation
        clock_sync.update_drift_estimates()

        # Should have ACC-based estimate
        acc_est = clock_sync.drift_estimator.get_best_estimate("head_001")
        assert acc_est is not None
        assert acc_est.method == "acc_correlation"
        # Just verify it runs and produces a finite offset
        assert np.isfinite(acc_est.offset_ms)
        assert np.isfinite(acc_est.drift_rate_ppm)

    def test_drift_correction_preserves_monotonicity(self) -> None:
        """Drift correction must preserve timestamp monotonicity."""
        fs = 500
        n_samples = 10000
        hub_ts = np.linspace(0, n_samples / fs, n_samples)
        # 50ppm drift
        pod_ts = hub_ts * (1 + 50e-6)

        sync_config = SyncConfig()
        clock_sync = MultiPodClockSync(sync_config)
        clock_sync.register_pod("head_001", acc_sampling_rate=100)

        # Simulate markers
        for i in range(5):
            marker_time = float(i * 5)
            marker = clock_sync.broadcast_sync(marker_time)
            pod_time = marker_time * (1 + 50e-6)
            marker.pod_timestamps["head_001"] = pod_time

        clock_sync.update_drift_estimates()

        corrected = clock_sync.correct_pod_timestamps("head_001", pod_ts)

        # Must be monotonically increasing
        assert np.all(np.diff(corrected) > 0)

        # Must be close to hub timestamps
        result = quantify_residual_drift(corrected, hub_ts, tier=Tier.T1, config=sync_config)
        _assert_float(result, "within_1ms_pct", 100.0)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
