"""The G1 soak cell capacity must agree with the band-core firmware default."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import check_contract as cc  # noqa: E402


def test_committed_capacity_matches_bringup() -> None:
    """The committed tree must pass its own battery-capacity check."""
    report = cc.Report()
    cc.check_hub_battery_capacity(cc._load(cc.BRINGUP_YAML), report)
    assert not report.failures, "; ".join(report.failures)


def test_deliberate_capacity_mismatch_fails(tmp_path: Path) -> None:
    """A firmware value that differs from the bringup cell must fail, not pass."""
    defaults = tmp_path / "sdkconfig.defaults.core"
    defaults.write_text("CONFIG_SYNAPSE_HUB_BATTERY_MAH=500\n", encoding="utf-8")
    bringup = cc._load(cc.BRINGUP_YAML)  # 1000 mAh in the committed file

    report = cc.Report()
    cc.check_hub_battery_capacity(bringup, report, defaults_path=defaults)
    assert any("CONFIG_SYNAPSE_HUB_BATTERY_MAH" in f for f in report.failures)
    assert report.checks_run == 1
