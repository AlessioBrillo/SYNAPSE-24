"""Tests for the host half of the per-unit calibration shell."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "factory_calibrate", ROOT / "scripts" / "factory_calibrate.py"
)
assert _spec is not None
assert _spec.loader is not None
fc = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = fc
_spec.loader.exec_module(fc)


def _args(**kwargs: object) -> argparse.Namespace:
    values = {
        "gain": None,
        "gain_json": None,
        "midrail": None,
        "led": None,
        "accel_bias": None,
        "gyro_bias": None,
        "imu_json": None,
        "instrument": None,
        "fw": None,
        "uncertainty": None,
        "noise_uv": None,
        "date": None,
        "operator": None,
        "dry_run": True,
        "dump": False,
        "erase": False,
    }
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_build_commands_translates_all_fields() -> None:
    commands = fc.build_commands(
        _args(
            gain=112300,
            led="1F,20",
            accel_bias=["1", "-2", "3"],
            gyro_bias=["4", "5", "-6"],
            date="2026-11-24",
            operator="A. Brillo",
        )
    )
    assert commands == [
        "cal gain 112300",
        "cal led 1F 20",
        "cal accel_bias 1 -2 3",
        "cal gyro_bias 4 5 -6",
        "cal date 2026-11-24",
        "cal operator A. Brillo",
        "cal save",
        "cal dump",
    ]


def test_calibration_v2_fields_are_written() -> None:
    commands = fc.build_commands(
        _args(
            instrument="GEN-42",
            fw="1.2.3",
            uncertainty=5.0,
            noise_uv=12.5,
        )
    )
    assert commands == [
        "cal instrument GEN-42",
        "cal fw 1.2.3",
        "cal uncertainty 500",
        "cal noise 125",
        "cal save",
        "cal dump",
    ]


def test_imu_json_feeds_the_shell(tmp_path: Path) -> None:
    path = tmp_path / "imu.json"
    path.write_text(json.dumps({"accel_bias_mg": [1, -2, 3], "gyro_bias_mdps": [4, 5, -6]}))
    commands = fc.build_commands(_args(imu_json=str(path)))
    assert commands[:2] == ["cal accel_bias 1 -2 3", "cal gyro_bias 4 5 -6"]


def test_gain_json_is_converted_to_the_kconfig_scale(tmp_path: Path) -> None:
    path = tmp_path / "gain.json"
    path.write_text(json.dumps({"gain_measured": 1123.0, "sdkconfig_ecg_gain": 112300}))
    commands = fc.build_commands(_args(gain_json=str(path)))
    assert "cal gain 112300" in commands


def test_the_serial_is_never_written_from_the_host() -> None:
    commands = fc.build_commands(_args(gain=110000, operator="A. Brillo"))
    assert not any(command.startswith("cal serial") for command in commands)


def test_parse_dump_refuses_an_empty_serial() -> None:
    with pytest.raises(ValueError, match="invalid serial"):
        fc.parse_dump('{"serial":"","ecg_gain_x100":110000}')


def test_fake_transport_round_trip() -> None:
    transport = fc.FakeTransport()
    for command in fc.build_commands(_args(gain=110000)):
        reply = transport.send(command)
        assert not reply.startswith("ERR"), reply
    record = fc.parse_dump(reply)
    assert record["ecg_gain_x100"] == 110000
    assert record["serial"] == "SYN-000000000000"  # the device owns its serial


def test_parse_dump_rejects_a_missing_record() -> None:
    with pytest.raises(ValueError, match="no calibration JSON"):
        fc.parse_dump("OK saved")


def test_the_host_record_uses_the_keys_device_profile_verify_expects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The band dumps `battery_capacity_mah`; the host record stores `battery_mah`."""
    monkeypatch.setattr(fc, "CAL_DIR", tmp_path)
    dump = {
        "serial": "SYN-A1B2C3D4E5F6",
        "ecg_gain_x100": 110000,
        "battery_capacity_mah": 1000,
        "instrument": "GEN-0001",
        "fw_version": "1.2.3",
        "gain_uncertainty_pct_x100": 500,
        "noise_std_uv_x10": 12,
    }
    path = fc.write_record(dump)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["battery_mah"] == 1000
    assert "battery_capacity_mah" not in record
    assert record["cal_instrument"] == "GEN-0001"
    assert record["cal_fw_version"] == "1.2.3"
    assert record["cal_uncertainty_pct_x100"] == 500
    assert record["cal_noise_std_uv_x10"] == 12


def test_a_real_port_dump_emits_json_and_writes_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dump is read-only: bench_suite.py must be able to parse its JSON line."""
    record = {
        "serial": "SYN-A1B2C3D4E5F6",
        "ecg_gain_x100": 110000,
        "cal_state": "nvs",
    }

    class FakePort:
        def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0) -> None:
            self.closed = False

        def send(self, line: str) -> str:
            assert line == "cal dump"
            return json.dumps(record)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(fc, "SerialTransport", FakePort)
    cal_dir = tmp_path / "calibration"
    monkeypatch.setattr(fc, "CAL_DIR", cal_dir)

    assert fc.main(["--port", "COM9", "--dump"]) == 0
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("{")
    ]
    assert any(item["serial"] == record["serial"] for item in lines)
    assert not cal_dir.exists(), "a dump must never write the host record"
