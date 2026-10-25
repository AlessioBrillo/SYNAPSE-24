"""Tests for the gravity-aware immobility detector.

The raw accelerometer magnitude includes the static gravity component (~1 g
at rest).  The detector must therefore judge immobility on the *dynamic*
acceleration (std of the magnitude over a window), not on the raw magnitude:
a still user yields magnitude ~1 g but low std, while a moving user yields
high std.  This matches ``imu_processor`` firmware semantics
(``dyn_acc_g < 0.05 g``).
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from synapse24.acquisition.immobility import ImmobilityDetector


def _feed(detector: ImmobilityDetector, n: int, value_fn) -> None:
    for _ in range(n):
        detector.update(value_fn(), time.time())


class TestGravityImmobility:
    """Immobilità con gravità inclusa nel segnale raw."""

    def test_still_one_g_is_immobile(self):
        """Magnitudine ~1 g costante (gravità, nessun movimento) → immobile."""
        det = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=30.0,
            magnitude_threshold=0.05,
            min_immobility_min=0.01,  # 1 window suffices
        )
        _feed(det, 3000, lambda: 1.0)  # 30 s di 1 g costante
        assert det.current_window_immobile is True

    def test_still_one_g_with_small_noise_is_immobile(self):
        """Magnitudine 1 g con rumore piccolo (std < soglia) → immobile."""
        rng = np.random.default_rng(0)
        det = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=30.0,
            magnitude_threshold=0.05,
            min_immobility_min=0.01,
        )
        _feed(det, 3000, lambda: float(1.0 + 0.005 * rng.standard_normal()))
        assert det.current_window_immobile is True

    def test_moving_high_std_is_not_immobile(self):
        """Magnitudine con variazione ampia (movimento reale) → non immobile."""
        rng = np.random.default_rng(1)
        det = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=30.0,
            magnitude_threshold=0.05,
            min_immobility_min=0.01,
        )
        _feed(det, 3000, lambda: float(1.0 + 0.5 * rng.standard_normal()))
        assert det.current_window_immobile is False

    def test_constant_high_magnitude_is_still_immobile(self):
        """1.5 g costante (es. piano inclinato offset) ha std ~0 → immobile.

        Lo strumento rileva il *movimento* (varianza), non l'offset statico.
        """
        det = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=30.0,
            magnitude_threshold=0.05,
            min_immobility_min=0.01,
        )
        _feed(det, 3000, lambda: 1.5)
        assert det.current_window_immobile is True

    def test_backward_compat_zero_magnitude(self):
        """Valori vicini a zero (fixture esistenti) continuano a rilevare immobilità."""
        det = ImmobilityDetector(
            accel_sampling_rate=100,
            window_duration_s=30.0,
            magnitude_threshold=0.02,
            min_immobility_min=0.01,
        )
        _feed(det, 3000, lambda: 0.01)
        assert det.current_window_immobile is True
