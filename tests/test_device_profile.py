"""The single profile tool must refuse unknowns and read back every field."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import device_profile as dp  # noqa: E402


def test_the_template_contains_only_unit_level_fields() -> None:
    assert set(dp.EXAMPLE) == {
        "gain_x100",
        "led_red",
        "led_ir",
        "accel_bias_mg",
        "gyro_bias_mdps",
        "battery_mah",
    }
    assert "sample_rate" not in dp.EXAMPLE
    assert "i2c_sda" not in dp.EXAMPLE


def test_unknown_profile_fields_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"i2c_sda": 8, "gain_x100": 110000}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown profile fields"):
        dp.load_profile(path)


def test_a_complete_profile_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(dp.EXAMPLE), encoding="utf-8")
    assert dp.load_profile(path) == dp.EXAMPLE


def test_diff_catches_missing_wrong_and_string_fields() -> None:
    readback = {
        "cal_serial": "SYN-OTHER",
        "ecg_gain_x100": 90000,
        "accel_bias_mg": [0, 0, 1],
    }
    problems = dp.diff_fields(
        readback,
        {
            "cal_serial": "SYN-MINE",
            "ecg_gain_x100": 110000,
            "accel_bias_mg": [0, 0, 0],
            "battery_mah": 1000,
        },
    )
    assert any("cal_serial" in p for p in problems)
    assert any("ecg_gain_x100" in p for p in problems)
    assert any("accel_bias_mg" in p for p in problems)
    assert any("battery_mah: missing" in p for p in problems)


def test_contract_fields_come_from_the_manifest() -> None:
    fields = dp.contract_fields()
    assert fields["ecg_hz"] == 500
    assert fields["i2c_sda"] == 8
    assert fields["battery_mah"] == 1000


def test_example_and_dry_run_do_not_touch_a_device(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert dp.main(["example"]) == 0
    assert "gain_x100" in capsys.readouterr().out
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"gain_x100": 110000}), encoding="utf-8")
    assert (
        dp.main(
            [
                "configure",
                "SYN-A1B2C3D4E5F6",
                "--address",
                "AA:BB",
                "--profile",
                str(path),
                "--dry-run",
            ]
        )
        == 0
    )
    assert "gain_x100" in capsys.readouterr().out
