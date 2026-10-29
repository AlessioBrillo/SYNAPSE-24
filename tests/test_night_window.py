"""Night window scheduler: crossing midnight, buffers and boundaries.

The scheduler triggers the Tier 0→1 transition by wall clock, so the
crossing/non-crossing branches and the buffers are pinned here on a fixed
UTC clock instead of on a bench at night.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from synapse24.acquisition import night_window
from synapse24.acquisition.night_window import NightWindowScheduler, SleepWindowConfig


def _utc(hour: int, minute: int) -> float:
    return datetime(2025, 1, 15, hour, minute, tzinfo=UTC).timestamp()


def _utc_next_day(hour: int, minute: int) -> float:
    return datetime(2025, 1, 16, hour, minute, tzinfo=UTC).timestamp()


def _scheduler(**overrides) -> NightWindowScheduler:
    return NightWindowScheduler(SleepWindowConfig(timezone="UTC", **overrides))


class TestCrossingMidnightWindow:
    """The default 22:00–07:00 window crosses midnight."""

    def test_inside_and_outside(self) -> None:
        night = _scheduler()
        assert night.in_sleep_window(_utc(23, 0)) is True
        assert night.in_sleep_window(_utc(3, 0)) is True
        assert night.in_sleep_window(_utc(12, 0)) is False

    def test_buffers_extend_both_edges(self) -> None:
        night = _scheduler()  # 30 min pre-sleep, 15 min post-wake
        assert night.in_sleep_window(_utc(21, 29)) is False
        assert night.in_sleep_window(_utc(21, 30)) is True
        assert night.in_sleep_window(_utc(7, 15)) is True
        assert night.in_sleep_window(_utc(7, 16)) is False

    def test_next_window_start_rolls_to_tomorrow_after_the_start(self) -> None:
        night = _scheduler()
        assert night.next_window_start(_utc(12, 0)) == _utc(22, 0)
        assert night.next_window_start(_utc(23, 0)) == _utc_next_day(22, 0)

    def test_next_window_end_rolls_to_tomorrow_after_the_end(self) -> None:
        night = _scheduler()
        assert night.next_window_end(_utc(3, 0)) == _utc(7, 0)
        assert night.next_window_end(_utc(8, 0)) == _utc_next_day(7, 0)

    def test_duration_and_countdown(self) -> None:
        night = _scheduler()
        assert night.estimate_window_duration(_utc(3, 0)) == pytest.approx(4.0)
        assert night.estimate_window_duration(_utc(12, 0)) == 0.0
        assert night.time_until_window(_utc(23, 0)) == 0.0
        assert night.time_until_window(_utc(12, 0)) == pytest.approx(10.0)

    def test_status_reports_the_window(self) -> None:
        status = _scheduler().get_status(_utc(3, 0))
        assert status["in_sleep_window"] is True
        assert status["timezone"] == "UTC"
        assert status["window_start"] == "22:00"
        assert status["window_end"] == "07:00"
        assert status["remaining_window_h"] == pytest.approx(4.0)
        assert status["time_until_window_h"] == 0.0


class TestNonCrossingWindow:
    """A window that does not cross midnight takes the other branch."""

    def test_inside_and_outside(self) -> None:
        nap = _scheduler(
            start_hour=13,
            start_minute=0,
            end_hour=15,
            end_minute=0,
            pre_sleep_buffer_min=0,
            post_wake_buffer_min=0,
        )
        assert nap.in_sleep_window(_utc(12, 0)) is False
        assert nap.in_sleep_window(_utc(13, 0)) is True
        assert nap.in_sleep_window(_utc(15, 0)) is True
        assert nap.in_sleep_window(_utc(15, 1)) is False


class TestClockFreeCalls:
    """Every public method must work without an explicit timestamp."""

    def test_none_means_now(self) -> None:
        night = _scheduler()
        assert isinstance(night.in_sleep_window(), bool)
        assert isinstance(night.next_window_start(), float)
        assert isinstance(night.next_window_end(), float)
        assert isinstance(night.estimate_window_duration(), float)
        assert isinstance(night.time_until_window(), float)
        assert isinstance(night.get_status(), dict)


def test_without_pytz_the_scheduler_falls_back_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When pytz is unavailable the module must still work (UTC fallback)."""
    monkeypatch.setattr(night_window, "PYTZ_AVAILABLE", False)
    monkeypatch.setattr(night_window, "pytz_module", None)
    night = NightWindowScheduler(SleepWindowConfig(timezone="Europe/Rome"))
    assert night.in_sleep_window(_utc(23, 0)) is True
    assert night.in_sleep_window(_utc(12, 0)) is False
