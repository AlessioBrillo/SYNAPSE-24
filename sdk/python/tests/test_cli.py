# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""CLI surface: discover, info (static + live probe), record, inspect.

The ``info`` pins carry the honesty contract: without a pod the command
prints only static SDK facts and NEVER a battery reading; with a pod it
probes the POWER characteristic and reports exactly what arrived -- a probe
that learns nothing fails instead of printing placeholders.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import synapse_device.cli as cli
from synapse_device.cli import main
from synapse_device.codec import RawFormat, RawStream, encode_frame
from synapse_device.transport import CHAR_ECG, CHAR_POWER, TransportError


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "synapse" in out


def test_discover_reports_the_pods_it_finds(capsys, monkeypatch: pytest.MonkeyPatch):
    """The scan must report the Synapse pods it finds, and only those."""
    pytest.importorskip("bleak")

    class _Device:
        def __init__(self, address: str, name: str) -> None:
            self.address = address
            self.name = name

    class _Scanner:
        @staticmethod
        def discover(timeout: float = 5.0):
            return {
                "aa:bb": _Device("aa:bb:cc:dd:ee:ff", "Synapse Pod"),
                "zz:zz": _Device("zz:zz:zz:zz:zz:zz", "Some Other Device"),
            }

    import bleak  # noqa: PLC0415

    monkeypatch.setattr(bleak, "BleakScanner", _Scanner)
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "aa:bb:cc:dd:ee:ff" in out
    assert "Some Other Device" not in out


def test_info_without_address_prints_only_static_facts(capsys):
    """No pod, no probe: static facts, and NEVER an invented battery value."""
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert "no pod probed" in out
    assert "MTU: target 247" in out
    assert "Battery" not in out
    assert "battery: NOT probed here" in out


class _FakePodTransport:
    """Fake pod for the CLI seams: a POWER reply, a frame schedule, or both."""

    def __init__(
        self,
        *,
        granted_mtu: int | None = 247,
        power_frame: bytes | None = None,
        schedule: tuple[tuple[float, str, bytes], ...] = (),
        connect_error: str | None = None,
    ) -> None:
        self.granted_mtu = granted_mtu
        self.power_frame = power_frame
        self.schedule = list(schedule)
        self.connect_error = connect_error
        self.connected = False
        self.mtu_requests: list[int] = []
        self.unsubscribed: list[str] = []
        self._tasks: list[asyncio.Task] = []

    async def connect(self, address: str) -> None:
        if self.connect_error:
            raise TransportError(self.connect_error)
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        tasks = self._tasks
        self._tasks = []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def subscribe(self, char_uuid: str, handler) -> None:
        if self.power_frame is not None and char_uuid == CHAR_POWER:
            handler(char_uuid, self.power_frame)
        plays = [entry for entry in self.schedule if entry[1] == char_uuid]
        self._tasks.append(asyncio.create_task(self._play(char_uuid, handler, plays)))

    async def unsubscribe(self, char_uuid: str) -> None:
        self.unsubscribed.append(char_uuid)

    async def request_mtu(self, mtu: int) -> int | None:
        self.mtu_requests.append(mtu)
        return self.granted_mtu

    async def write(self, char_uuid: str, data: bytes, response: bool = True) -> None:
        raise AssertionError(f"the probe/record path must not write to {char_uuid}")

    async def read(self, char_uuid: str) -> bytes:
        raise AssertionError(f"the probe/record path must not read {char_uuid}")

    async def _play(self, char_uuid: str, handler, plays) -> None:
        import time  # noqa: PLC0415

        started = time.monotonic()
        for delay, _uuid, frame in plays:
            await asyncio.sleep(max(delay - (time.monotonic() - started), 0.0))
            handler(char_uuid, frame)


def _power_frame() -> bytes:
    """One real POWER frame (S32, 4 channels: mV, mA, mW, %) via the codec."""
    return encode_frame(RawStream.POWER, 0, 1_000_000, 0, RawFormat.S32, [[3720, 12, 44, 87]])


def test_info_probe_reports_the_power_channels(capsys, monkeypatch: pytest.MonkeyPatch):
    """A live pod answers with real numbers and the granted MTU, as asked."""
    fake = _FakePodTransport(power_frame=_power_frame())
    monkeypatch.setattr(cli, "_build_transport", lambda _address: fake)
    assert main(["info", "--address", "AA:BB:CC:DD:EE:FF"]) == 0
    out = capsys.readouterr().out
    for needle in (
        "voltage_mv = 3720",
        "current_ma = 12",
        "power_mw = 44",
        "battery_pct = 87",
        "MTU granted: 247",
    ):
        assert needle in out, f"missing from the probe output: {needle!r}\n{out}"
    assert fake.mtu_requests == [cli.MTU_TARGET]
    assert CHAR_POWER in fake.unsubscribed  # the probe leaves the link clean


def test_info_probe_without_a_frame_times_out(capsys, monkeypatch: pytest.MonkeyPatch):
    """No POWER frame in time: exit 1, the probe says what it did NOT get."""
    fake = _FakePodTransport()  # nothing ever delivered
    monkeypatch.setattr(cli, "_build_transport", lambda _address: fake)
    monkeypatch.setattr(cli, "_POWER_PROBE_TIMEOUT_S", 0.05)
    assert main(["info", "--address", "AA:BB:CC:DD:EE:FF"]) == 1
    err = capsys.readouterr().err
    assert "no POWER frame" in err
    assert "Battery" not in capsys.readouterr().out + err


def test_info_probe_connect_failure_is_a_clean_error(capsys, monkeypatch: pytest.MonkeyPatch):
    """A pod that cannot be reached fails with the transport's own words."""
    fake = _FakePodTransport(connect_error="no pod answering at AA:BB")
    monkeypatch.setattr(cli, "_build_transport", lambda _address: fake)
    assert main(["info", "--address", "AA:BB:CC:DD:EE:FF"]) == 1
    assert "no pod answering at AA:BB" in capsys.readouterr().err


def test_record_ble_source_runs_through_the_recorder(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
):
    """``--source <addr>`` is the real recorder: MTU + measured rate summary."""
    pytest.importorskip("pyxdf")

    schedule = (
        (0.05, CHAR_ECG, encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S16, [[7]])),
        (0.10, CHAR_ECG, encode_frame(RawStream.ECG, 1, 10_000, 0, RawFormat.S16, [[8]])),
    )
    fake = _FakePodTransport(schedule=schedule, granted_mtu=123)
    monkeypatch.setattr(cli, "_build_transport", lambda _address: fake)
    out = tmp_path / "ble.xdf"
    code = main(["record", "--source", "AA:BB:CC:DD:EE:FF", "--out", str(out), "--duration", "0.4"])
    assert code == 0
    printed = capsys.readouterr().out
    assert "MTU granted: 123" in printed
    assert "ECG: 2 frames" in printed
    assert "100.000 Hz measured" in printed  # from the frames' own stamps
    assert out.is_file() and out.stat().st_size > 0
    assert fake.unsubscribed  # the capture unsubscribes what it subscribed


def test_record_synthetic(tmp_path: Path):
    out = tmp_path / "session.xdf"
    assert main(["record", "--source", "synthetic", "--out", str(out), "--duration", "1"]) == 0
    assert out.is_file()
    assert out.stat().st_size > 0


def test_inspect_reads_the_recorded_session(tmp_path: Path, capsys):
    """``inspect`` must read back what ``record`` wrote."""
    out = tmp_path / "session.xdf"
    assert main(["record", "--source", "synthetic", "--out", str(out), "--duration", "1"]) == 0
    capsys.readouterr()

    assert main(["inspect", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "XDF" in printed
    assert "streams" in printed


@pytest.mark.parametrize("command", ["export", "index", "report", "analyze"])
def test_moved_commands_point_at_the_host_pipeline(command: str, capsys):
    """The commands that moved must say where they went, not fail silently."""
    assert main([command]) == 2
    err = capsys.readouterr().err
    assert "moved to the host pipeline" in err
    assert "synapse" in err or "worker" in err
