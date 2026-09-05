"""Clock synchronization for multi-pod SYNAPSE-24 acquisition.

Implements hardware sync markers + ACC cross-correlation drift correction
per Architecture.md §92 clock drift risk mitigation.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
from scipy.signal import correlate

from synapse24.signal_quality import Tier


@dataclass(frozen=True)
class SyncConfig:
    """Configuration for clock synchronization."""

    # Sync marker interval (seconds)
    sync_interval_s: float = 60.0

    # Maximum acceptable residual drift after correction (ms)
    max_residual_drift_ms: float = 1.0

    # ACC cross-correlation window (seconds)
    acc_corr_window_s: float = 30.0

    # Minimum ACC correlation coefficient for valid drift estimate
    min_acc_correlation: float = 0.7

    # Number of sync markers to keep for drift history
    max_history: int = 100

    # Sampling rates for ACC streams (Hz) per pod
    acc_sampling_rates: dict[str, int] = field(default_factory=dict)


@dataclass
class SyncMarker:
    """A synchronization marker with timestamps from all pods."""

    sequence: int
    hub_timestamp: float  # LSL local_clock() at hub
    pod_timestamps: dict[str, float]  # pod_id -> pod's local_clock() when marker received
    wall_time: float = field(default_factory=time.time)


@dataclass
class DriftEstimate:
    """Clock drift estimate between a pod and hub."""

    pod_id: str
    offset_ms: float  # pod_time - hub_time in milliseconds
    drift_rate_ppm: float  # parts per million (relative frequency error)
    confidence: float  # 0-1, based on ACC correlation
    method: str  # "marker" or "acc_correlation"
    timestamp: float = field(default_factory=time.time)


class SyncMarkerManager:
    """Manages broadcast sync markers via LSL Markers stream.

    All pods and hub subscribe to a shared LSL Markers stream.
    Hub broadcasts SYNC markers at regular intervals.
    Each pod records local_clock() when marker arrives.
    """

    def __init__(self, config: SyncConfig | None = None) -> None:
        self.config = config if config is not None else SyncConfig()
        self._marker_stream = None
        self._sequence = 0
        self._last_broadcast = 0.0
        self._callbacks: dict[str, list[Callable[[SyncMarker, float], None]]] = {}

    def set_marker_stream(self, stream: Any) -> None:
        """Set the LSL marker outlet for broadcasting."""
        self._marker_stream = stream

    def register_callback(self, pod_id: str, callback: Callable[[SyncMarker, float], None]) -> None:
        """Register a callback for when a sync marker is received."""
        if pod_id not in self._callbacks:
            self._callbacks[pod_id] = []
        self._callbacks[pod_id].append(callback)

    def broadcast_sync(self, hub_timestamp: float | None = None) -> SyncMarker:
        """Broadcast a SYNC marker to all subscribers."""
        if hub_timestamp is None:
            hub_timestamp = time.time()

        marker = SyncMarker(
            sequence=self._sequence,
            hub_timestamp=hub_timestamp,
            pod_timestamps={},
        )

        # Broadcast via LSL if stream available
        if self._marker_stream:
            try:
                self._marker_stream.push_sample([f"SYNC_{self._sequence}"], hub_timestamp)
            except Exception:
                pass  # Non-blocking

        # Notify registered callbacks (simulated pod receipt)
        for pod_id, callbacks in self._callbacks.items():
            for cb in callbacks:
                try:
                    # Simulate pod receiving marker at its local clock
                    pod_time = time.time()  # In real implementation, pod's local_clock()
                    cb(marker, pod_time)
                except Exception:
                    pass

        self._sequence += 1
        self._last_broadcast = hub_timestamp
        return marker

    def should_broadcast(self, current_time: float | None = None) -> bool:
        """Check if it's time to broadcast a sync marker."""
        if current_time is None:
            current_time = time.time()
        return current_time - self._last_broadcast >= self.config.sync_interval_s


class ClockDriftEstimator:
    """Estimates clock drift between pods and hub using multiple methods.

    Methods:
    1. Sync markers: Direct timestamp comparison
    2. ACC cross-correlation: Uses shared physical motion as reference
    """

    def __init__(self, config: SyncConfig | None = None) -> None:
        self.config = config if config is not None else SyncConfig()
        self._marker_history: deque[SyncMarker] = deque(maxlen=self.config.max_history)
        self._acc_buffers: dict[str, deque[float]] = {}
        self._acc_timestamps: dict[str, deque[float]] = {}
        self._last_drift_estimates: dict[str, DriftEstimate] = {}

    def add_marker(self, marker: SyncMarker) -> None:
        """Record a sync marker for drift estimation."""
        self._marker_history.append(marker)

    def add_acc_sample(self, pod_id: str, acc_magnitude: float, timestamp: float) -> None:
        """Add ACC sample for cross-correlation drift estimation."""
        if pod_id not in self._acc_buffers:
            self._acc_buffers[pod_id] = deque(
                maxlen=int(
                    self.config.acc_corr_window_s * self.config.acc_sampling_rates.get(pod_id, 100)
                )
            )
            self._acc_timestamps[pod_id] = deque(maxlen=self._acc_buffers[pod_id].maxlen)

        self._acc_buffers[pod_id].append(acc_magnitude)
        self._acc_timestamps[pod_id].append(timestamp)

    def estimate_from_markers(self) -> dict[str, DriftEstimate]:
        """Estimate drift from sync marker timestamps.

        Returns:
            Dict of pod_id -> DriftEstimate
        """
        if len(self._marker_history) < 2:
            return {}

        estimates = {}
        # Use linear regression on marker timestamps
        for pod_id in self._marker_history[0].pod_timestamps:
            hub_times_list: list[float] = []
            pod_times_list: list[float] = []
            for m in self._marker_history:
                if pod_id in m.pod_timestamps:
                    hub_times_list.append(m.hub_timestamp)
                    pod_times_list.append(m.pod_timestamps[pod_id])

            if len(hub_times_list) < 2:
                continue

            hub_times = np.array(hub_times_list)
            pod_times = np.array(pod_times_list)

            # Linear fit: pod_time = a * hub_time + b
            # drift_rate = (a - 1) * 1e6 ppm
            # offset = b * 1000 ms
            A = np.vstack([hub_times, np.ones_like(hub_times)]).T
            a, b = np.linalg.lstsq(A, pod_times, rcond=None)[0]

            drift_rate_ppm = (a - 1.0) * 1_000_000
            offset_ms = b * 1000  # Use intercept as the clock offset at hub_time=0

            estimates[pod_id] = DriftEstimate(
                pod_id=pod_id,
                offset_ms=offset_ms,
                drift_rate_ppm=drift_rate_ppm,
                confidence=min(len(hub_times) / 10.0, 1.0),
                method="marker",
            )

        self._last_drift_estimates.update(estimates)
        return estimates

    def estimate_from_acc_correlation(
        self,
        hub_acc: npt.NDArray[np.float64],
        hub_timestamps: npt.NDArray[np.float64],
        pod_id: str,
    ) -> DriftEstimate | None:
        """Estimate drift using ACC cross-correlation with hub.

        Assumes hub and pod experience same physical motion.
        Cross-correlation of ACC magnitude reveals time offset.
        """
        if pod_id not in self._acc_buffers:
            return None

        pod_acc = np.array(self._acc_buffers[pod_id])
        pod_ts = np.array(self._acc_timestamps[pod_id])

        if len(pod_acc) < 100 or len(hub_acc) < 100:
            return None

        # Resample both to common rate if needed
        hub_fs = 1.0 / np.mean(np.diff(hub_timestamps)) if len(hub_timestamps) > 1 else 100
        pod_fs = self.config.acc_sampling_rates.get(pod_id, 100)

        # Cross-correlation
        corr = correlate(hub_acc, pod_acc, mode="full")
        lags = np.arange(-len(pod_acc) + 1, len(hub_acc))
        max_corr_idx = np.argmax(corr)
        max_corr = corr[max_corr_idx]
        lag_samples = lags[max_corr_idx]

        # Normalized correlation coefficient
        norm_corr = max_corr / (np.sqrt(np.sum(hub_acc**2) * np.sum(pod_acc**2)) + 1e-10)

        if norm_corr < self.config.min_acc_correlation:
            return None

        # Time offset in seconds
        time_offset_s = lag_samples / hub_fs
        offset_ms = time_offset_s * 1000

        # Also estimate drift rate from slope of offset over time
        # (simplified: use marker-based drift rate if available)
        drift_rate_ppm = 0.0
        if pod_id in self._last_drift_estimates:
            drift_rate_ppm = self._last_drift_estimates[pod_id].drift_rate_ppm

        estimate = DriftEstimate(
            pod_id=pod_id,
            offset_ms=offset_ms,
            drift_rate_ppm=drift_rate_ppm,
            confidence=float(norm_corr),
            method="acc_correlation",
        )

        self._last_drift_estimates[pod_id] = estimate
        return estimate

    def get_best_estimate(self, pod_id: str) -> DriftEstimate | None:
        """Get the best available drift estimate for a pod."""
        return self._last_drift_estimates.get(pod_id)

    def get_all_estimates(self) -> dict[str, DriftEstimate]:
        """Get all current drift estimates."""
        return self._last_drift_estimates.copy()


class TimestampCorrector:
    """Applies clock drift corrections to pod timestamps.

    Uses linear correction: corrected_time = (raw_time - offset) / (1 + drift_rate)
    """

    def __init__(self) -> None:
        self._corrections: dict[str, tuple[float, float]] = {}  # pod_id -> (offset_s, drift_rate)

    def update_correction(self, estimate: DriftEstimate) -> None:
        """Update correction parameters from drift estimate."""
        offset_s = estimate.offset_ms / 1000.0
        drift_rate = estimate.drift_rate_ppm / 1_000_000.0
        self._corrections[estimate.pod_id] = (offset_s, drift_rate)

    def correct_timestamp(self, pod_id: str, raw_timestamp: float) -> float:
        """Apply correction to a single timestamp."""
        if pod_id not in self._corrections:
            return raw_timestamp

        offset_s, drift_rate = self._corrections[pod_id]
        return (raw_timestamp - offset_s) / (1.0 + drift_rate)

    def correct_timestamps(
        self, pod_id: str, raw_timestamps: npt.NDArray[np.float64]
    ) -> npt.NDArray[np.float64]:
        """Apply correction to an array of timestamps."""
        if pod_id not in self._corrections:
            return raw_timestamps

        offset_s, drift_rate = self._corrections[pod_id]
        return (raw_timestamps - offset_s) / (1.0 + drift_rate)

    def get_correction(self, pod_id: str) -> tuple[float, float] | None:
        """Get current correction parameters."""
        return self._corrections.get(pod_id)


class MultiPodClockSync:
    """High-level coordinator for multi-pod clock synchronization.

    Integrates:
    - SyncMarkerManager: Broadcast/receive sync markers
    - ClockDriftEstimator: Estimate drift from markers + ACC correlation
    - TimestampCorrector: Apply corrections to pod data
    """

    def __init__(self, config: SyncConfig | None = None) -> None:
        self.config = config if config is not None else SyncConfig()
        self.marker_manager = SyncMarkerManager(self.config)
        self.drift_estimator = ClockDriftEstimator(self.config)
        self.corrector = TimestampCorrector()

        # Hub ACC buffer for cross-correlation
        self._hub_acc_buffer: deque[float] = deque(maxlen=int(self.config.acc_corr_window_s * 100))
        self._hub_acc_timestamps: deque[float] = deque(
            maxlen=int(self.config.acc_corr_window_s * 100)
        )

    def register_pod(self, pod_id: str, acc_sampling_rate: int = 100) -> None:
        """Register a pod for synchronization."""
        self.config.acc_sampling_rates[pod_id] = acc_sampling_rate
        # Register callback for marker receipt with pod_id bound
        self.marker_manager.register_callback(
            pod_id, lambda m, ts: self._on_pod_marker_received(m, ts, pod_id)
        )

    def _on_pod_marker_received(
        self, marker: SyncMarker, pod_timestamp: float, pod_id: str
    ) -> None:
        """Callback when pod receives a sync marker."""
        marker.pod_timestamps[pod_id] = pod_timestamp

    def broadcast_sync(self, hub_timestamp: float | None = None) -> SyncMarker:
        """Broadcast sync marker from hub."""
        marker = self.marker_manager.broadcast_sync(hub_timestamp)
        self.drift_estimator.add_marker(marker)
        return marker

    def add_hub_acc(self, acc_magnitude: float, timestamp: float) -> None:
        """Add hub ACC sample for cross-correlation reference."""
        self._hub_acc_buffer.append(acc_magnitude)
        self._hub_acc_timestamps.append(timestamp)

    def add_pod_acc(self, pod_id: str, acc_magnitude: float, timestamp: float) -> None:
        """Add pod ACC sample for cross-correlation."""
        self.drift_estimator.add_acc_sample(pod_id, acc_magnitude, timestamp)

    def update_drift_estimates(self) -> dict[str, DriftEstimate]:
        """Update all drift estimates and corrections."""
        estimates = {}

        # Method 1: Marker-based (provides both offset and drift rate)
        marker_estimates = self.drift_estimator.estimate_from_markers()
        estimates.update(marker_estimates)

        # Method 2: ACC cross-correlation (if hub ACC available)
        if len(self._hub_acc_buffer) > 100:
            hub_acc = np.array(self._hub_acc_buffer)
            hub_ts = np.array(self._hub_acc_timestamps)
            for pod_id in self.config.acc_sampling_rates:
                acc_est = self.drift_estimator.estimate_from_acc_correlation(
                    hub_acc, hub_ts, pod_id
                )
                if acc_est:
                    if pod_id in estimates:
                        # Combine: use ACC offset (more precise) + marker drift rate
                        combined = DriftEstimate(
                            pod_id=pod_id,
                            offset_ms=acc_est.offset_ms,
                            drift_rate_ppm=estimates[pod_id].drift_rate_ppm,
                            confidence=max(acc_est.confidence, estimates[pod_id].confidence),
                            method="combined",
                        )
                        estimates[pod_id] = combined
                    else:
                        # No marker estimate available, use ACC with 0 drift rate
                        estimates[pod_id] = acc_est

        # Update correctors
        for pod_id, estimate in estimates.items():
            self.corrector.update_correction(estimate)

        return estimates

    def correct_pod_timestamps(
        self, pod_id: str, raw_timestamps: npt.NDArray[np.float64]
    ) -> npt.NDArray[np.float64]:
        """Correct pod timestamps to hub clock domain."""
        return self.corrector.correct_timestamps(pod_id, raw_timestamps)

    def get_sync_status(self) -> dict[str, Any]:
        """Get synchronization status for all pods."""
        estimates = self.drift_estimator.get_all_estimates()
        return {
            "pods": {
                pod_id: {
                    "offset_ms": est.offset_ms,
                    "drift_rate_ppm": est.drift_rate_ppm,
                    "confidence": est.confidence,
                    "method": est.method,
                    "within_tolerance": abs(est.offset_ms) <= self.config.max_residual_drift_ms,
                }
                for pod_id, est in estimates.items()
            },
            "marker_history_len": len(self.drift_estimator._marker_history),
            "hub_acc_buffer_len": len(self._hub_acc_buffer),
        }


def quantify_residual_drift(
    corrected_pod_timestamps: npt.NDArray[np.float64],
    hub_timestamps: npt.NDArray[np.float64],
) -> dict[str, float]:
    """Quantify residual drift after correction.

    Computes statistics of timestamp differences between corrected pod
    timestamps and corresponding hub timestamps (assuming same sample count).
    """
    if len(corrected_pod_timestamps) != len(hub_timestamps):
        raise ValueError("Timestamp arrays must have same length")

    diffs_ms = (corrected_pod_timestamps - hub_timestamps) * 1000

    return {
        "mean_offset_ms": float(np.mean(diffs_ms)),
        "std_offset_ms": float(np.std(diffs_ms)),
        "max_abs_offset_ms": float(np.max(np.abs(diffs_ms))),
        "p99_offset_ms": float(np.percentile(np.abs(diffs_ms), 99)),
        "within_1ms_pct": float(np.mean(np.abs(diffs_ms) <= 1.0) * 100),
    }
