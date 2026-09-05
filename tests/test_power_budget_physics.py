"""Power budget physics regression tests.

Architecture.md §55-62: hub 2000-3000mAh, Tier 0 ~5mW, Tier 1 tens of mW.
Physics: mAh = mW*h / V_nominal (3.7V LiPo). mWh tracked internally.

RED: current implementation uses h*mW/1000*3.7 (understates drain ~73x,
overstates remaining hours ~73x) — non-conservative, invalidates 24h claim.
"""

from __future__ import annotations

import pytest

from synapse24.acquisition.power_budget import NOMINAL_VOLTAGE_V, PowerBudgetManager
from synapse24.signal_quality import Tier


def _isolated_manager(**kwargs) -> PowerBudgetManager:
    """Create manager with wall-clock contribution neutralised for determinism."""
    mgr = PowerBudgetManager(**kwargs)
    # Neutralise in-progress session so get_consumed_mah reflects totals only.
    mgr._tier0_start = None  # noqa: SLF001
    mgr._tier1_start = None  # noqa: SLF001
    mgr._tier2_start = None  # noqa: SLF001
    return mgr


class TestPowerBudgetPhysics:
    """mAh conversion must follow mAh = mWh / V."""

    def test_nominal_voltage_constant(self):
        assert pytest.approx(3.7) == NOMINAL_VOLTAGE_V

    def test_24h_tier0_endurance_math(self):
        """5mW x 24h = 120mWh / 3.7V = ~32.43mAh consumed."""
        mgr = _isolated_manager(hub_battery_mah=3000, reserve_mah=300)
        mgr._tier0_total_h = 24.0  # noqa: SLF001
        expected_mah = 5.0 * 24.0 / NOMINAL_VOLTAGE_V
        assert expected_mah == pytest.approx(32.43, abs=0.1)
        assert mgr.get_consumed_mah() == pytest.approx(expected_mah, rel=0.05)
        # Usable 2700mAh must remain largely available after T0-only day.
        assert mgr.get_remaining_mah() == pytest.approx(2700.0 - expected_mah, rel=0.05)

    def test_mixed_tier_session_budget(self):
        """8h T1@50mW + 16h T0@5mW = 480mWh / 3.7 = ~129.7mAh."""
        mgr = _isolated_manager(hub_battery_mah=3000, reserve_mah=300)
        mgr._tier0_total_h = 16.0  # noqa: SLF001
        mgr._tier1_total_h = 8.0  # noqa: SLF001
        expected_mah = (16.0 * 5.0 + 8.0 * 50.0) / NOMINAL_VOLTAGE_V
        assert expected_mah == pytest.approx(129.73, abs=0.2)
        assert mgr.get_consumed_mah() == pytest.approx(expected_mah, rel=0.05)

    def test_estimated_remaining_hours_physics(self):
        """Full usable 2700mAh at 5mW -> 2700*3.7/5 = 1998h."""
        mgr = _isolated_manager(hub_battery_mah=3000, reserve_mah=300)
        # Force Tier 0 current draw.
        mgr._current_tier = Tier.T0  # noqa: SLF001
        expected_h = 2700.0 * NOMINAL_VOLTAGE_V / 5.0
        assert expected_h == pytest.approx(1998.0, abs=1.0)
        assert mgr.get_estimated_remaining_h() == pytest.approx(expected_h, rel=0.05)

    def test_tier1_max_duration_still_enforced(self):
        mgr = _isolated_manager(tier1_max_h=10.0)
        assert mgr.can_afford_tier1(duration_h=11.0) is False
