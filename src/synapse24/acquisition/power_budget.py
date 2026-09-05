"""Power budget manager for SYNAPSE-24 energy constraints."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Final, Optional

from synapse24.signal_quality import Tier

NOMINAL_VOLTAGE_V: Final[float] = 3.7
"""Nominal LiPo cell voltage for mWh <-> mAh conversion (Architecture.md §55-62)."""


def mah_for_power(mw: float, hours: float) -> float:
    """Convert constant power draw over time to charge consumed.

    mWh = mW * h; mAh = mWh / V_nominal.
    """
    return float(mw * hours / NOMINAL_VOLTAGE_V)


def hours_for_charge(mah: float, mw: float) -> float:
    """Convert remaining charge to hours at constant power draw."""
    if mw <= 0:
        return float("inf")
    return float(mah * NOMINAL_VOLTAGE_V / mw)


@dataclass
class PowerProfile:
    """Power consumption profile for a tier."""

    tier: Tier
    avg_mw: float  # Average power in milliwatts
    peak_mw: float  # Peak power in milliwatts
    max_duration_h: float | None = None  # Max continuous hours


@dataclass
class EnergyBudgetStatus:
    """Current energy budget status."""

    battery_remaining_mah: float
    battery_capacity_mah: float
    estimated_remaining_h: float
    tier0_h_used: float
    tier1_h_used: float
    tier2_h_used: float
    can_afford_tier1: bool
    can_afford_tier2: bool
    power_draw_mw: float


class PowerBudgetManager:
    """Manages energy budget for SYNAPSE-24 tiered acquisition.

    Architecture.md §55-62: Energy budget is the real constraint.
    - Tier 0 continuous: ~µW–low mW (earbud-like)
    - Tier 1 sessions: tens of mW (hub-powered)
    - Tier 2 bursts: higher power, short duration

    Enforces hard limits to ensure 24h target lifetime.
    """

    def __init__(
        self,
        hub_battery_mah: float = 3000,
        target_lifetime_h: float = 24,
        tier0_avg_mw: float = 5.0,
        tier1_avg_mw: float = 50.0,
        tier1_max_h: float = 10.0,
        tier2_avg_mw: float = 100.0,
        tier2_max_burst_min: float = 30.0,
        reserve_mah: float = 300,  # 10% reserve
    ) -> None:
        self.hub_battery_mah = hub_battery_mah
        self.target_lifetime_h = target_lifetime_h
        self.reserve_mah = reserve_mah
        self.usable_mah = hub_battery_mah - reserve_mah

        # Power profiles
        self.profiles = {
            Tier.T0: PowerProfile(Tier.T0, tier0_avg_mw, tier0_avg_mw * 2),
            Tier.T1: PowerProfile(
                Tier.T1, tier1_avg_mw, tier1_avg_mw * 2, max_duration_h=tier1_max_h
            ),
            Tier.T2: PowerProfile(
                Tier.T2, tier2_avg_mw, tier2_avg_mw * 2, max_duration_h=tier2_max_burst_min / 60
            ),
        }

        # Usage tracking
        self._tier0_start: float | None = None
        self._tier1_start: float | None = None
        self._tier2_start: float | None = None
        self._tier0_total_h = 0.0
        self._tier1_total_h = 0.0
        self._tier2_total_h = 0.0
        self._current_tier = Tier.T0

        # Start Tier 0 timer
        self._tier0_start = time.time()

    def on_tier_change(self, from_tier: Tier, to_tier: Tier) -> None:
        """Call when tier changes to update timers."""
        now = time.time()

        # Stop previous tier timer
        if self._current_tier == Tier.T0 and self._tier0_start:
            self._tier0_total_h += (now - self._tier0_start) / 3600
            self._tier0_start = None
        elif self._current_tier == Tier.T1 and self._tier1_start:
            self._tier1_total_h += (now - self._tier1_start) / 3600
            self._tier1_start = None
        elif self._current_tier == Tier.T2 and self._tier2_start:
            self._tier2_total_h += (now - self._tier2_start) / 3600
            self._tier2_start = None

        # Start new tier timer
        if to_tier == Tier.T0:
            self._tier0_start = now
        elif to_tier == Tier.T1:
            self._tier1_start = now
        elif to_tier == Tier.T2:
            self._tier2_start = now

        self._current_tier = to_tier

    def get_current_consumption_mw(self) -> float:
        """Get current power draw in mW."""
        return self.profiles[self._current_tier].avg_mw

    def get_consumed_mah(self) -> float:
        """Get total consumed mAh so far (mAh = mW*h / 3.7V)."""
        now = time.time()
        consumed = 0.0

        # Completed sessions
        consumed += mah_for_power(self.profiles[Tier.T0].avg_mw, self._tier0_total_h)
        consumed += mah_for_power(self.profiles[Tier.T1].avg_mw, self._tier1_total_h)
        consumed += mah_for_power(self.profiles[Tier.T2].avg_mw, self._tier2_total_h)

        # Current session
        if self._current_tier == Tier.T0 and self._tier0_start:
            h = (now - self._tier0_start) / 3600
            consumed += mah_for_power(self.profiles[Tier.T0].avg_mw, h)
        elif self._current_tier == Tier.T1 and self._tier1_start:
            h = (now - self._tier1_start) / 3600
            consumed += mah_for_power(self.profiles[Tier.T1].avg_mw, h)
        elif self._current_tier == Tier.T2 and self._tier2_start:
            h = (now - self._tier2_start) / 3600
            consumed += mah_for_power(self.profiles[Tier.T2].avg_mw, h)

        return consumed

    def get_remaining_mah(self) -> float:
        """Get remaining mAh."""
        return max(self.usable_mah - self.get_consumed_mah(), 0.0)

    def get_estimated_remaining_h(self) -> float:
        """Estimate remaining hours at current consumption rate."""
        remaining_mah = self.get_remaining_mah()
        if remaining_mah <= 0:
            return 0.0
        current_mw = self.get_current_consumption_mw()
        return hours_for_charge(remaining_mah, current_mw)

    def can_afford_tier1(self, duration_h: float) -> bool:
        """Check if we can afford a Tier 1 session of given duration."""
        max_duration = self.profiles[Tier.T1].max_duration_h
        if max_duration is not None and duration_h > max_duration:
            return False

        # Projected consumption
        projected_mah = self.get_consumed_mah()
        projected_mah += mah_for_power(self.profiles[Tier.T1].avg_mw, duration_h)

        # Must leave enough for remaining Tier 0 time
        remaining_target_h = self.target_lifetime_h - self._get_total_elapsed_h()
        tier0_needed_mah = mah_for_power(self.profiles[Tier.T0].avg_mw, remaining_target_h)

        return (projected_mah + tier0_needed_mah) <= self.usable_mah

    def can_afford_tier2(self, duration_min: float) -> bool:
        """Check if we can afford a Tier 2 burst."""
        duration_h = duration_min / 60
        max_duration = self.profiles[Tier.T2].max_duration_h
        if max_duration is not None and duration_h > max_duration:
            return False

        projected_mah = self.get_consumed_mah()
        projected_mah += mah_for_power(self.profiles[Tier.T2].avg_mw, duration_h)

        remaining_target_h = self.target_lifetime_h - self._get_total_elapsed_h()
        tier0_needed_mah = mah_for_power(self.profiles[Tier.T0].avg_mw, remaining_target_h)

        return (projected_mah + tier0_needed_mah) <= self.usable_mah

    def _get_total_elapsed_h(self) -> float:
        """Get total elapsed hours since start."""
        now = time.time()
        elapsed = self._tier0_total_h + self._tier1_total_h + self._tier2_total_h
        if self._current_tier == Tier.T0 and self._tier0_start:
            elapsed += (now - self._tier0_start) / 3600
        elif self._current_tier == Tier.T1 and self._tier1_start:
            elapsed += (now - self._tier1_start) / 3600
        elif self._current_tier == Tier.T2 and self._tier2_start:
            elapsed += (now - self._tier2_start) / 3600
        return elapsed

    def record_tier1_session(self, actual_duration_h: float) -> None:
        """Record a completed Tier 1 session."""
        self._tier1_total_h += actual_duration_h

    def record_tier2_session(self, actual_duration_min: float) -> None:
        """Record a completed Tier 2 session."""
        self._tier2_total_h += actual_duration_min / 60

    def get_status(self) -> EnergyBudgetStatus:
        """Get current budget status."""
        remaining_mah = self.get_remaining_mah()
        return EnergyBudgetStatus(
            battery_remaining_mah=remaining_mah,
            battery_capacity_mah=self.hub_battery_mah,
            estimated_remaining_h=self.get_estimated_remaining_h(),
            tier0_h_used=self._tier0_total_h,
            tier1_h_used=self._tier1_total_h,
            tier2_h_used=self._tier2_total_h,
            can_afford_tier1=self.can_afford_tier1(1.0),
            can_afford_tier2=self.can_afford_tier2(10.0),
            power_draw_mw=self.get_current_consumption_mw(),
        )

    def get_tier_status(self, tier: Tier) -> dict[str, Any]:
        """Get detailed status for a specific tier."""
        profile = self.profiles[tier]
        used_h = {
            Tier.T0: self._tier0_total_h,
            Tier.T1: self._tier1_total_h,
            Tier.T2: self._tier2_total_h,
        }[tier]

        max_h = profile.max_duration_h or float("inf")

        return {
            "tier": tier.name,
            "avg_mw": profile.avg_mw,
            "peak_mw": profile.peak_mw,
            "used_h": used_h,
            "max_continuous_h": max_h,
            "remaining_h": max(0, max_h - used_h) if max_h != float("inf") else float("inf"),
        }
