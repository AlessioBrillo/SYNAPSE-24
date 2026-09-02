"""Night window scheduler for Tier 0→1 transition."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Optional

try:
    import pytz
except ImportError:
    pytz = None


@dataclass
class SleepWindowConfig:
    """Configuration for sleep window."""

    start_hour: int = 22  # 22:00
    start_minute: int = 0
    end_hour: int = 7  # 07:00
    end_minute: int = 0
    timezone: str = "Europe/Rome"
    pre_sleep_buffer_min: int = 30
    post_wake_buffer_min: int = 15


class NightWindowScheduler:
    """Schedules Tier 1 activation during sleep window.

    Architecture.md §37-41: "Attivato da IMU (immobilità >X min) o da finestra oraria (notte)".

    Handles timezone, DST, and buffers around sleep window.
    """

    def __init__(self, config: SleepWindowConfig | None = None) -> None:
        self.config = config or SleepWindowConfig()
        self._tz = self._get_timezone(self.config.timezone)

    def _get_timezone(self, tz_name: str):
        """Get timezone object."""
        if pytz is not None:
            return pytz.timezone(tz_name)
        # Fallback: use datetime.timezone with fixed offset (approximate)
        import datetime

        # This is a rough approximation - pytz is recommended
        return datetime.UTC

    def in_sleep_window(self, timestamp: float | None = None) -> bool:
        """Check if timestamp falls within sleep window (with buffers)."""
        if timestamp is None:
            timestamp = datetime.datetime.now(self._tz).timestamp()

        dt = datetime.datetime.fromtimestamp(timestamp, self._tz)
        time_minutes = dt.hour * 60 + dt.minute

        start_minutes = self.config.start_hour * 60 + self.config.start_minute
        end_minutes = self.config.end_hour * 60 + self.config.end_minute

        # Apply buffers
        buffered_start = (start_minutes - self.config.pre_sleep_buffer_min) % (24 * 60)
        buffered_end = (end_minutes + self.config.post_wake_buffer_min) % (24 * 60)

        if buffered_start < buffered_end:
            # Window doesn't cross midnight
            return buffered_start <= time_minutes <= buffered_end
        # Window crosses midnight
        return time_minutes >= buffered_start or time_minutes <= buffered_end

    def next_window_start(self, timestamp: float | None = None) -> float:
        """Get timestamp of next sleep window start."""
        if timestamp is None:
            timestamp = datetime.datetime.now(self._tz).timestamp()

        dt = datetime.datetime.fromtimestamp(timestamp, self._tz)

        # Today's window start
        today_start = dt.replace(
            hour=self.config.start_hour,
            minute=self.config.start_minute,
            second=0,
            microsecond=0,
        )

        if dt >= today_start:
            # Next window is tomorrow
            next_start = today_start + datetime.timedelta(days=1)
        else:
            next_start = today_start

        return next_start.timestamp()

    def next_window_end(self, timestamp: float | None = None) -> float:
        """Get timestamp of next sleep window end."""
        if timestamp is None:
            timestamp = datetime.datetime.now(self._tz).timestamp()

        dt = datetime.datetime.fromtimestamp(timestamp, self._tz)

        today_end = dt.replace(
            hour=self.config.end_hour,
            minute=self.config.end_minute,
            second=0,
            microsecond=0,
        )

        if dt >= today_end:
            next_end = today_end + datetime.timedelta(days=1)
        else:
            next_end = today_end

        return next_end.timestamp()

    def estimate_window_duration(self, timestamp: float | None = None) -> float:
        """Estimate remaining sleep window duration in hours."""
        if timestamp is None:
            timestamp = datetime.datetime.now(self._tz).timestamp()

        if not self.in_sleep_window(timestamp):
            return 0.0

        end_ts = self.next_window_end(timestamp)
        return (end_ts - timestamp) / 3600

    def time_until_window(self, timestamp: float | None = None) -> float:
        """Time until next sleep window starts (hours)."""
        if timestamp is None:
            timestamp = datetime.datetime.now(self._tz).timestamp()

        if self.in_sleep_window(timestamp):
            return 0.0

        start_ts = self.next_window_start(timestamp)
        return (start_ts - timestamp) / 3600

    def get_status(self, timestamp: float | None = None) -> dict:
        """Get scheduler status."""
        if timestamp is None:
            timestamp = datetime.datetime.now(self._tz).timestamp()

        return {
            "in_sleep_window": self.in_sleep_window(timestamp),
            "timezone": self.config.timezone,
            "window_start": f"{self.config.start_hour:02d}:{self.config.start_minute:02d}",
            "window_end": f"{self.config.end_hour:02d}:{self.config.end_minute:02d}",
            "pre_sleep_buffer_min": self.config.pre_sleep_buffer_min,
            "post_wake_buffer_min": self.config.post_wake_buffer_min,
            "time_until_window_h": self.time_until_window(timestamp),
            "remaining_window_h": self.estimate_window_duration(timestamp),
        }
