"""Tier duration reset regression (Architecture.md §33-43, §55-62; Guardian P2+P8).

TierStateMachine must clear T1/T2 start times on exit so
AcquisitionController.tick() power-budget demotion operates on
current-segment duration, not stale timestamps.
"""

from __future__ import annotations

from synapse24.acquisition.state_machine import TierStateMachine
from synapse24.signal_quality import Tier


class FakeClock:
    """Deterministic LSL-clock stand-in (Guardian P3: injectable clock)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestTierDurationReset:
    def test_t1_duration_clears_on_demote_to_t0(self) -> None:
        clock = FakeClock()
        sm = TierStateMachine(clock_fn=clock)
        assert sm.promote_to_tier1("immobility_detected")
        clock.advance(120.0)
        assert sm.get_tier1_duration() == 120.0
        assert sm.demote_to_tier0("movement_detected")
        assert sm.get_tier1_duration() is None

    def test_t2_duration_clears_on_end_tier2(self) -> None:
        clock = FakeClock()
        sm = TierStateMachine(clock_fn=clock)
        assert sm.start_tier2("user_calibration")
        clock.advance(60.0)
        assert sm.get_tier2_duration() == 60.0
        assert sm.end_tier2("session_done")
        assert sm.get_tier2_duration() is None
        assert sm.current_tier == Tier.T0

    def test_t1_to_t2_clears_t1_and_arms_t2(self) -> None:
        clock = FakeClock()
        sm = TierStateMachine(clock_fn=clock)
        assert sm.promote_to_tier1("night_window_start")
        clock.advance(30.0)
        assert sm.start_tier2("user_request")
        # Leaving T1 must clear its timer; entering T2 must arm its timer.
        assert sm.get_tier1_duration() is None
        assert sm.get_tier2_duration() == 0.0

    def test_t2_to_t1_clears_t2_and_rearms_t1(self) -> None:
        clock = FakeClock()
        sm = TierStateMachine(clock_fn=clock)
        assert sm.promote_to_tier1("immobility_detected")
        assert sm.start_tier2("user_request")
        clock.advance(45.0)
        assert sm.end_tier2("session_done")
        assert sm.current_tier == Tier.T1
        assert sm.get_tier2_duration() is None
        assert sm.get_tier1_duration() == 0.0
