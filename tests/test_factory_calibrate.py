"""Tests for the host half of the per-unit calibration shell."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import types
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
        "battery": None,
        "reboot": False,
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
        'cal date "2026-11-24"',
        'cal operator "A. Brillo"',
        "cal save",
        "cal dump",
    ]


def test_multiword_provenance_survives_the_emitted_commands() -> None:
    """The exact bug: a multi-word operator used to reach NVS as just "A."."""
    commands = fc.build_commands(
        _args(
            operator="A. Brillo",
            instrument="Fluke 8846A",
            fw="v2.10 dev",
            date="2026-11-28",
        )
    )
    assert commands == [
        'cal instrument "Fluke 8846A"',
        'cal fw "v2.10 dev"',
        'cal date "2026-11-28"',
        'cal operator "A. Brillo"',
        "cal save",
        "cal dump",
    ]
    transport = fc.FakeTransport()
    for command in commands:
        reply = transport.send(command)
        assert not reply.startswith("ERR"), reply
    record = fc.parse_dump(reply)
    assert record["operator"] == "A. Brillo"
    assert record["instrument"] == "Fluke 8846A"
    assert record["fw_version"] == "v2.10 dev"
    assert record["date"] == "2026-11-28"


@pytest.mark.parametrize("value", ['a "b', "a\nb", "a\rb"])
def test_shell_quote_refuses_values_the_shell_cannot_represent(value: str) -> None:
    with pytest.raises(ValueError, match="cannot contain quotes or newlines"):
        fc.shell_quote(value)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"battery": 50}, "battery must be 100..10000"),
        ({"gain": 2_000_000}, "gain must be 1..1000000"),
        ({"led": "00,1F"}, "LED codes must be 01..FF"),
        ({"accel_bias": ["3000", "0", "0"]}, "--accel-bias values"),
        ({"gyro_bias": ["0", "25000", "0"]}, "--gyro-bias values"),
    ],
)
def test_usb_validation_matches_the_ble_whitelist(kwargs: object, message: str) -> None:
    """A USB shell must not persist a value the radio path would refuse."""
    with pytest.raises(ValueError, match=message):
        fc.build_commands(_args(**kwargs))


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
        'cal instrument "GEN-42"',
        'cal fw "1.2.3"',
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


def test_fake_transport_mirrors_the_real_firmware_text_parser() -> None:
    """An unquoted value is truncated; malformed quotes are refused loudly."""
    transport = fc.FakeTransport()
    assert transport.send("cal operator A. Brillo") == "OK operator"
    assert transport.send('cal instrument "GEN 42"') == "OK instrument"
    assert transport.send('cal operator "A. Brillo') == "ERR bad operator value"
    assert transport.send('cal operator "A." junk') == "ERR bad operator value"
    assert transport.send('cal operator A."Brillo') == "ERR bad operator value"
    record = fc.parse_dump(transport.send("cal dump"))
    assert record["operator"] == "A."  # first token only, like strtok on the band
    assert record["instrument"] == "GEN 42"  # quoted -> whole value


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


def test_text_provenance_is_bounded_by_the_firmware_buffers() -> None:
    """A 17-char fw value is silently truncated by the band, so refuse it here."""
    with pytest.raises(ValueError, match="fw must be at most 16"):
        fc.build_commands(_args(fw="band-bench@abcdef", dry_run=True))
    with pytest.raises(ValueError, match="operator must be at most 16"):
        fc.build_commands(_args(operator="A very long operator name", dry_run=True))
    assert fc.build_commands(_args(fw="v1.0.0", dry_run=True))


def test_provenance_accepts_a_non_empty_numeric_string() -> None:
    """cal_has_provenance() checks a non-empty first byte, not human_plausibility."""
    record = {
        "serial": "SYN-A1B2C3D4E5F6",
        "date": "2026-11-28",
        "operator": "0",
        "instrument": "GEN-42",
        "fw_version": "0",
        "gain_uncertainty_pct_x100": 500,
    }
    assert fc.missing_provenance(record) == []


def test_missing_provenance_mirrors_the_firmware_whitelist() -> None:
    complete = {
        "serial": "SYN-A1B2C3D4E5F6",
        "date": "2026-11-28",
        "operator": "A. Brillo",
        "instrument": "GEN-42",
        "fw_version": "band-bench@abc",
        "gain_uncertainty_pct_x100": 500,
    }
    assert fc.missing_provenance(complete) == []
    assert fc.missing_provenance({}) == list(fc.PROVENANCE_DUMP_KEYS)
    partial = dict(complete, instrument="", gain_uncertainty_pct_x100=0)
    assert fc.missing_provenance(partial) == ["instrument", "gain_uncertainty_pct_x100"]


def _port_with_record(record: dict[str, object]) -> type:
    class Port:
        def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0) -> None:
            self.inner = fc.FakeTransport(record=dict(record))

        def send(self, line: str) -> str:
            return self.inner.send(line)

        def close(self) -> None:
            return None

    return Port


def test_first_write_without_provenance_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A record missing instrument/fw/uncertainty would block every later BLE write."""
    monkeypatch.setattr(
        fc,
        "SerialTransport",
        _port_with_record({"serial": "SYN-A1B2C3D4E5F6", "ecg_gain_x100": 110000}),
    )
    monkeypatch.setattr(fc, "CAL_DIR", tmp_path / "calibration")
    assert (
        fc.main(
            [
                "--port",
                "COM9",
                "--led",
                "1F,1F",
                "--operator",
                "A. Brillo",
                "--uncertainty",
                "5",
            ]
        )
        == 1
    )
    assert "complete provenance" in capsys.readouterr().err
    assert not (tmp_path / "calibration").exists()


def test_first_write_with_full_provenance_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        fc,
        "SerialTransport",
        _port_with_record({"serial": "SYN-A1B2C3D4E5F6", "ecg_gain_x100": 110000}),
    )
    monkeypatch.setattr(fc, "CAL_DIR", tmp_path / "calibration")
    monkeypatch.setattr(fc, "ROOT", tmp_path)
    assert (
        fc.main(
            [
                "--port",
                "COM9",
                "--led",
                "1F,1F",
                "--operator",
                "A. Brillo",
                "--instrument",
                "GEN-42",
                "--fw",
                "band-bench@abc",
                "--uncertainty",
                "5",
                "--battery",
                "1000",
            ]
        )
        == 0
    )
    record = json.loads((tmp_path / "calibration" / "SYN-A1B2C3D4E5F6.json").read_text())
    assert record["cal_instrument"] == "GEN-42"
    assert record["cal_uncertainty_pct_x100"] == 500


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


class _FakeConn:
    """Minimal pyserial stand-in: answers `cal dump` only when told to."""

    answers_dump = False

    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 2.0) -> None:
        self.port = port
        self.line = ""
        self.closed = False
        self.dumped = 0

    def write(self, data: bytes) -> None:
        self.line = data.decode("ascii").strip()

    def readline(self) -> bytes:
        if self.line == "cal dump" and type(self).answers_dump:
            self.dumped += 1
            return b'{"serial":"SYN-A1B2C3D4E5F6"}\r\n'
        return b"OK reboot\r\n"

    def close(self) -> None:
        self.closed = True


def _install_fake_serial(monkeypatch: pytest.MonkeyPatch, conn_type: type[_FakeConn]) -> None:
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=conn_type))


def test_reboot_polls_until_the_booted_shell_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LateBoot(_FakeConn):
        answers_dump = True

    _install_fake_serial(monkeypatch, LateBoot)
    transport = fc.SerialTransport("COM9")
    transport.reboot(timeout=1.0)
    assert transport._conn.dumped == 1
    transport.close()


def test_reboot_fails_loudly_when_the_band_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_serial(monkeypatch, _FakeConn)
    transport = fc.SerialTransport("COM9")
    with pytest.raises(RuntimeError, match="did not answer `cal dump` within"):
        transport.reboot(timeout=0.05)
