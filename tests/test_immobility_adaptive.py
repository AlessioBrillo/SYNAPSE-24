"""Adaptive immobility detector, statuses and reset semantics.

The adaptive detector adjusts its threshold from the observed baseline; the
base detector's counters/reset are pinned here too, so a promotion cycle
cannot silently stop reporting progress.
"""

from __future__ import annotations

import pytest

from synapse24.acquisition.immobility import AdaptiveImmobilityDetector, ImmobilityDetector


def _small(**overrides) -> dict:
    """A tiny window/requirement so the test feeds tens of samples, not 30k."""
    params = {
        "accel_sampling_rate": 10,
        "window_duration_s": 1.0,
        "magnitude_threshold": 0.05,
        "min_immobility_min": 0.05,  # 3 windows of 1 s
    }
    params.update(overrides)
    return params


def _feed(detector, n: int, value: float) -> bool:
    triggered = False
    for _ in range(n):
        triggered = detector.update(value, None) or triggered
    return triggered


class TestBaseDetectorStatus:
    """Counter, progress and reset of the non-adaptive detector."""

    def test_immobility_triggers_and_reports_progress(self) -> None:
        det = ImmobilityDetector(**_small())
        assert det.progress_ratio == 0.0
        assert _feed(det, 30, 1.0) is True  # 3 full quiet windows
        status = det.get_status()
        assert status["consecutive_immobile_windows"] == 3
        assert status["required_windows"] == 3
        assert status["progress_ratio"] == 1.0
        assert status["current_window_immobile"] is True
        assert status["threshold_metric"] == "dyn_acc_g_std"

    def test_movement_resets_the_streak(self) -> None:
        det = ImmobilityDetector(**_small())
        _feed(det, 10, 1.0)  # one quiet window, below the 3 required
        det.update(3.0, None)  # a movement spike: std jumps, counter resets
        assert det.get_status()["consecutive_immobile_windows"] == 0
        assert det.progress_ratio == 0.0

    def test_reset_clears_the_window(self) -> None:
        det = ImmobilityDetector(**_small())
        _feed(det, 30, 1.0)
        det.reset()
        status = det.get_status()
        assert status["consecutive_immobile_windows"] == 0
        assert status["current_window_immobile"] is False
        assert det.progress_ratio == 0.0

    def test_zero_required_windows_is_immediately_done(self) -> None:
        det = ImmobilityDetector(**_small(min_immobility_min=0.0))
        assert det.get_status()["required_windows"] == 0
        assert det.progress_ratio == 1.0


class TestAdaptiveDetector:
    """Threshold adaptation from the baseline window."""

    def test_adapts_the_threshold_from_a_quiet_baseline(self) -> None:
        det = AdaptiveImmobilityDetector(
            **_small(magnitude_threshold=0.02, adaptation_window_s=200.0)
        )
        # The adaptation kicks in once the 200-sample baseline is full; a
        # zero baseline yields the 0.015 g floor.
        _feed(det, 200, 0.0)
        status = det.get_status()
        assert status["baseline_samples"] == 200
        assert status["adapted_threshold"] == pytest.approx(0.01)  # percentile + margin
        assert det.magnitude_threshold == pytest.approx(0.015)  # floored at 0.015 g

    def test_does_not_adapt_before_the_baseline_is_full(self) -> None:
        det = AdaptiveImmobilityDetector(
            **_small(magnitude_threshold=0.02, adaptation_window_s=400.0)
        )
        _feed(det, 100, 1.0)
        status = det.get_status()
        assert status["baseline_samples"] == 100
        assert status["adapted_threshold"] is None
        assert det.magnitude_threshold == pytest.approx(0.02)
