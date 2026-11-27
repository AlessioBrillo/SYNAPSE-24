"""Contract 2.4 writes are narrow and only committed after readback."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "device_config_write", ROOT / "scripts" / "device_config_write.py"
)
assert _spec is not None
assert _spec.loader is not None
dcw = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dcw
_spec.loader.exec_module(dcw)


def _args(**kwargs: object) -> argparse.Namespace:
    values = {
        "led": None,
        "gain": None,
        "feature_rate": None,
        "battery": None,
        "accel_bias": None,
        "gyro_bias": None,
    }
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_only_whitelisted_fields_are_written() -> None:
    assert dcw.build_payload(_args(led="1F,20")) == {"led_red": 31, "led_ir": 32}
    assert dcw.build_payload(_args(gain=1100)) == {"gain_x100": 110000}
    assert dcw.build_payload(_args(feature_rate=2)) == {"feature_rate_hz": 2}
    assert dcw.build_payload(_args(battery=1000)) == {"battery_mah": 1000}
    combined = dcw.build_payload(_args(led="1F,1F", gain=1100, battery=1000))
    assert set(combined) == {"led_red", "led_ir", "gain_x100", "battery_mah"}
    with pytest.raises(ValueError, match="nothing to write"):
        dcw.build_payload(_args())


def test_readback_refuses_a_wrong_serial_or_an_unapplied_profile() -> None:
    expected = {"ecg_gain_x100": 110000}
    with pytest.raises(ValueError, match="cal_serial"):
        dcw.verify_readback({"cal_serial": "SYN-OTHER"}, expected, "SYN-MINE")
    # A committed profile is pending reboot, but the write still succeeded.
    assert (
        dcw.verify_readback(
            {"cal_serial": "SYN-MINE", "pending_reboot": 1, "ecg_gain_x100": 110000},
            expected,
            "SYN-MINE",
        )["pending_reboot"]
        == 1
    )
    with pytest.raises(ValueError, match="missing from readback"):
        dcw.verify_readback({"cal_serial": "SYN-MINE", "pending_reboot": 0}, expected, "SYN-MINE")


def test_local_record_is_updated_after_a_matching_readback() -> None:
    written: list[tuple[Path, str]] = []
    sink = lambda path, text: written.append((path, text))  # noqa: E731
    path = dcw.update_local_record(
        {
            "cal_serial": "SYN-MINE",
            "ecg_gain_x100": 112300,
            "ppg_led_red": 31,
            "ppg_led_ir": 32,
            "accel_bias_mg": [1, 2, 3],
            "gyro_bias_mdps": [0, 0, 0],
            "battery_mah": 1000,
            "cal_state": "nvs",
            "cal_date": "2026-11-26",
            "cal_operator": "tester",
        },
        "SYN-MINE",
        sink,
    )
    assert len(written) == 1
    assert path.name == "SYN-MINE.json"
    payload = json.loads(written[0][1])
    assert payload["ecg_gain_x100"] == 112300
    assert payload["battery_mah"] == 1000
    assert payload["accel_bias_mg"] == [1, 2, 3]


def test_local_record_persists_the_measurement_provenance() -> None:
    written: list[tuple[Path, str]] = []
    path = dcw.update_local_record(
        {
            "cal_serial": "SYN-MINE",
            "cal_instrument": "GEN-42",
            "cal_fw_version": "1.2.3",
            "cal_uncertainty_pct_x100": 500,
            "cal_noise_std_uv_x10": 125,
        },
        "SYN-MINE",
        lambda p, text: written.append((p, text)),
    )
    assert path.name == "SYN-MINE.json"
    payload = json.loads(written[0][1])
    assert payload["cal_instrument"] == "GEN-42"
    assert payload["cal_fw_version"] == "1.2.3"
    assert payload["cal_uncertainty_pct_x100"] == 500
    assert payload["cal_noise_std_uv_x10"] == 125


def test_a_bad_serial_is_refused_before_a_dry_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert dcw.main(["--address", "AA:BB", "--serial", "SYN-short", "--led", "1F,1F"]) == 1
    assert "invalid serial" in capsys.readouterr().err
    assert (
        dcw.main(
            ["--address", "AA:BB", "--serial", "SYN-A1B2C3D4E5F6", "--dry-run", "--led", "1F,1F"]
        )
        == 0
    )
