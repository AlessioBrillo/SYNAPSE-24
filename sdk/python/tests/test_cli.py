# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""CLI surface: discover, info, version, record (synthetic), inspect."""

from __future__ import annotations

from pathlib import Path

import pytest

from synapse_device.cli import main


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "synapse" in out


def test_discover_reports_the_pods_it_finds(capsys, monkeypatch: pytest.MonkeyPatch):
    """The scan must report the Synapse pods it finds, and only those."""
    bleak = pytest.importorskip("bleak")

    class _Device:
        def __init__(self, address: str, name: str) -> None:
            self.address = address
            self.name = name

    class _Scanner:
        @staticmethod
        def discover(timeout: float = 5.0):
            return {
                "aa:bb": _Device("aa:bb:cc:dd:ee:ff", "SYNAPSE-BAND"),
                "11:22": _Device("11:22:33:44:55:66", "Some Other Device"),
            }

    monkeypatch.setattr(bleak, "BleakScanner", _Scanner)
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "SYNAPSE-BAND" in out
    assert "Some Other Device" not in out


def test_info(capsys):
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert "ECG(AD8232)" in out


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
