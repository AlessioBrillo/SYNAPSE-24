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


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"led_red": 31}, "written together"),
        ({"led_red": 0, "led_ir": 31}, "0 keeps the compiled default"),
        ({"led_red": 31, "led_ir": 256}, "out of range"),
    ],
)
def test_led_values_the_firmware_cannot_write_are_refused_on_the_host(
    tmp_path: Path, payload: dict[str, int], message: str
) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        dp.load_profile(path)


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


def test_the_configured_image_is_the_one_that_allows_configuration() -> None:
    """A manifest naming band-core would make every configure/write fail."""
    import yaml  # noqa: PLC0415 - local to this assertion

    hw = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    bring = yaml.safe_load((ROOT / "config" / "hardware_bringup.yaml").read_text(encoding="utf-8"))
    variant = hw["pods"][0]["build_variant"]
    assert variant == bring["bringup"]["firmware"]["build_variant"] == "band-bench"
    defaults_file = variant.removeprefix("band-")
    defaults = (ROOT / "firmware" / "esp32_band" / f"sdkconfig.defaults.{defaults_file}").read_text(
        encoding="utf-8"
    )
    assert "CONFIG_SYNAPSE_FACTORY_MODE=y" in defaults


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


SERIAL = "SYN-A1B2C3D4E5F6"


def _readback(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        **dp.contract_fields(),
        "cal_serial": SERIAL,
        "pending_reboot": 0,
        "ecg_gain_x100": 110000,
        "ppg_led_red": 31,
        "ppg_led_ir": 31,
        "accel_bias_mg": [0, 0, 0],
        "gyro_bias_mdps": [0, 0, 0],
        "battery_mah": 1000,
        "cal_instrument": "GEN-42",
        "cal_fw_version": "1.2.3",
        "cal_uncertainty_pct_x100": 500,
        "cal_noise_std_uv_x10": 125,
    }
    payload.update(overrides)
    return payload


def _record() -> dict[str, object]:
    return {
        "serial": SERIAL,
        "ecg_gain_x100": 110000,
        "ppg_led_red": 31,
        "ppg_led_ir": 31,
        "accel_bias_mg": [0, 0, 0],
        "gyro_bias_mdps": [0, 0, 0],
        "battery_mah": 1000,
        "cal_instrument": "GEN-42",
        "cal_fw_version": "1.2.3",
        "cal_uncertainty_pct_x100": 500,
        "cal_noise_std_uv_x10": 125,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gain_x100", 0),
        ("gain_x100", 1_000_001),
        ("led_red", 256),
        ("led_ir", -1),
        ("battery_mah", 99),
        ("battery_mah", 10_001),
        ("accel_bias_mg", [0, 0, 2001]),
        ("gyro_bias_mdps", [0, 0, 20_001]),
        ("accel_bias_mg", [0, 0, 40000]),
        ("feature_rate_hz", 0),
        ("feature_rate_hz", 6),
    ],
)
def test_out_of_range_profile_fields_are_refused(tmp_path: Path, field: str, value: object) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({field: value}), encoding="utf-8")
    with pytest.raises(ValueError, match="out of range|must be"):
        dp.load_profile(path)


def test_invalid_serials_are_refused_before_any_device_work(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert dp.main(["verify", "SYN-short", "--address", "AA:BB"]) == 1
    assert "invalid serial" in capsys.readouterr().err


def test_configure_does_not_commit_the_host_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(dp.EXAMPLE), encoding="utf-8")

    async def fake_write(_address: str, _payload: dict[str, object]) -> dict[str, object]:
        return _readback()

    committed: list[str] = []

    def no_commit(*_args: object) -> Path:
        committed.append("called")
        return Path("x.json")

    monkeypatch.setattr(dp.dcw, "_write", fake_write)
    monkeypatch.setattr(dp.dcw, "update_local_record", no_commit)

    assert dp.main(["configure", SERIAL, "--address", "AA:BB", "--profile", str(profile)]) == 0
    output = capsys.readouterr().out
    assert "verify" in output
    assert committed == []


def test_verify_commits_only_a_complete_host_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_read(_address: str) -> dict[str, object]:
        return _readback()

    monkeypatch.setattr(dp.dcw, "_read", fake_read)
    monkeypatch.setattr(
        dp,
        "contract_fields",
        lambda: {
            "ecg_hz": 500,
            "ppg_hz": 50,
            "imu_hz": 50,
            "ecg_adc_ch": 0,
            "i2c_sda": 8,
            "i2c_scl": 9,
            "battery_mah": 1000,
        },
    )
    monkeypatch.setattr(dp, "ROOT", tmp_path)
    monkeypatch.setattr(dp.dcw, "ROOT", tmp_path)

    assert dp.main(["verify", SERIAL, "--address", "AA:BB"]) == 1
    assert "missing" in capsys.readouterr().out
    cal_dir = tmp_path / "data" / "calibration"
    cal_dir.mkdir(parents=True)
    (cal_dir / f"{SERIAL}.json").write_text(json.dumps(_record()), encoding="utf-8")

    assert dp.main(["verify", SERIAL, "--address", "AA:BB"]) == 0
    assert "committed" in capsys.readouterr().out
    persisted = json.loads((cal_dir / f"{SERIAL}.json").read_text(encoding="utf-8"))
    assert persisted["cal_instrument"] == "GEN-42"
    assert persisted["cal_fw_version"] == "1.2.3"
    assert persisted["cal_uncertainty_pct_x100"] == 500
    assert persisted["cal_noise_std_uv_x10"] == 125

    # A stale provenance value must fail the verify, not be silently ignored.
    stale = _record()
    stale["cal_fw_version"] = "9.9.9"
    (cal_dir / f"{SERIAL}.json").write_text(json.dumps(stale), encoding="utf-8")
    assert dp.main(["verify", SERIAL, "--address", "AA:BB"]) == 1
    assert "cal_fw_version" in capsys.readouterr().out
