# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""CLI surface: discover, info, version, record (synthetic)."""

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


def test_discover(capsys):
    assert main(["discover"]) == 0
    out = capsys.readouterr().out
    assert "SYN-SYNTH-0000" in out


def test_info(capsys):
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    assert "ECG(AD8232)" in out


def test_record_synthetic(tmp_path: Path):
    out = tmp_path / "session.xdf"
    assert main(["record", "--source", "synthetic", "--out", str(out), "--duration", "1"]) == 0
    assert out.is_file()
    assert out.stat().st_size > 0
