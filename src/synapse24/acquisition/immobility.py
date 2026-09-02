"""IMU-based immobility detector for Tier 0→1 transition."""

from __future__ import annotations

from collections import deque

import numpy as np

from synapse24.signal_quality import Tier


class ImmobilityDetector:
    """Detects sustained immobility from accelerometer data.

    Used to trigger Tier 0 → Tier 1 transition when user is at rest.
    Based on Architecture.md §37-41: "Attivato da IMU (immobilità >X min)".

    Algorithm:
    - Sliding window of accelerometer magnitude
    - Window is "immobile" if magnitude < threshold for entire window
    - Requires N consecutive immobile windows = min_immobility_min minutes
    """

    def __init__(
        self,
        accel_sampling_rate: int = 100,
        window_duration_s: float = 30.0,
        magnitude_threshold: float = 0.02,  # g-force
        min_immobility_min: float = 5.0,  # minutes of continuous immobility
    ) -> None:
        self.accel_sampling_rate = accel_sampling_rate
        self.window_duration_s = window_duration_s
        self.magnitude_threshold = magnitude_threshold
        self.min_immobility_min = min_immobility_min

        self._window_samples = int(window_duration_s * accel_sampling_rate)
        self._required_windows = int(min_immobility_min * 60 / window_duration_s)

        # Buffer for current window
        self._window_buffer: deque[float] = deque(maxlen=self._window_samples)
        # Track consecutive immobile windows
        self._consecutive_immobile = 0
        self._total_samples = 0

    def update(self, accel_magnitude: float, timestamp: float | None = None) -> bool:
        """Update detector with new accelerometer magnitude sample.

        Args:
            accel_magnitude: Accelerometer magnitude in g-force
            timestamp: Optional timestamp (unused, for future sync)

        Returns:
            True if immobility threshold reached (trigger Tier 1)
        """
        self._window_buffer.append(accel_magnitude)
        self._total_samples += 1

        # Only check when window is full
        if len(self._window_buffer) < self._window_samples:
            return False

        # Check if current window is immobile
        window_max = max(self._window_buffer)
        is_immobile = window_max < self.magnitude_threshold

        if is_immobile:
            self._consecutive_immobile += 1
        else:
            self._consecutive_immobile = 0

        # Check if we've reached required consecutive immobile windows
        if self._consecutive_immobile >= self._required_windows:
            self._consecutive_immobile = 0  # Reset after trigger
            return True

        return False

    def reset(self) -> None:
        """Reset detector state."""
        self._window_buffer.clear()
        self._consecutive_immobile = 0
        self._total_samples = 0

    @property
    def current_window_immobile(self) -> bool:
        """Check if current window is immobile."""
        if len(self._window_buffer) < self._window_samples:
            return False
        return max(self._window_buffer) < self.magnitude_threshold

    @property
    def progress_ratio(self) -> float:
        """Get progress toward immobility trigger [0, 1]."""
        if self._required_windows == 0:
            return 1.0
        return min(self._consecutive_immobile / self._required_windows, 1.0)

    def get_status(self) -> dict:
        return {
            "consecutive_immobile_windows": self._consecutive_immobile,
            "required_windows": self._required_windows,
            "current_window_immobile": self.current_window_immobile,
            "progress_ratio": self.progress_ratio,
            "threshold_g": self.magnitude_threshold,
            "window_duration_s": self.window_duration_s,
            "min_immobility_min": self.min_immobility_min,
        }


class AdaptiveImmobilityDetector(ImmobilityDetector):
    """Adaptive immobility detector that adjusts threshold based on context.

    Uses baseline noise estimation to adapt to sensor placement differences.
    """

    def __init__(self, *args, adaptation_window_s: float = 300.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.adaptation_window_s = adaptation_window_s
        self._baseline_buffer: deque[float] = deque(maxlen=int(adaptation_window_s))
        self._adapted_threshold: float | None = None

    def update(self, accel_magnitude: float, timestamp: float | None = None) -> bool:
        # Update baseline estimation
        self._baseline_buffer.append(accel_magnitude)

        # Periodically adapt threshold
        if len(self._baseline_buffer) >= self._baseline_buffer.maxlen:
            self._adapt_threshold()

        return super().update(accel_magnitude, timestamp)

    def _adapt_threshold(self) -> None:
        """Adapt threshold based on observed baseline noise."""
        if len(self._baseline_buffer) < 100:
            return

        # Use 95th percentile of baseline as noise floor
        baseline_array = np.array(self._baseline_buffer)
        noise_floor = np.percentile(baseline_array, 95)

        # Set threshold to noise_floor + margin
        self._adapted_threshold = noise_floor + 0.01  # 0.01g margin
        self.magnitude_threshold = max(self._adapted_threshold, 0.015)  # Floor at 0.015g

    def get_status(self) -> dict:
        status = super().get_status()
        status["adapted_threshold"] = self._adapted_threshold
        status["baseline_samples"] = len(self._baseline_buffer)
        return status
