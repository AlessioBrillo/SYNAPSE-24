"""The bench suite writes evidence for failures too."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bench_suite as bs  # noqa: E402


def test_factory_checks_the_serial_from_the_device(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bs, "EVIDENCE", tmp_path)
    dump = json.dumps({"serial": "SYN-A1B2C3D4E5F6", "ecg_gain_x100": 110000})
    result = bs.factory(
        "COM9",
        lambda _cmd: (0, f"some log\n{dump}\n"),
        dis_reader=lambda _target: "SYN-A1B2C3D4E5F6",
    )
    assert result["pass"] is True
    assert result["observations"]["serial"] == "SYN-A1B2C3D4E5F6"
    assert any(p.suffix == ".json" for p in tmp_path.glob("T02_*.json"))


def test_a_bad_serial_fails_still_writes_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bs, "EVIDENCE", tmp_path)
    result = bs.factory(
        "COM9",
        lambda _cmd: (0, '{"serial":""}'),
        dis_reader=lambda _target: "SYN-A1B2C3D4E5F6",
    )
    assert result["pass"] is False
    evidence = list(tmp_path.glob("T02_*.json"))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text())["pass"] is False


def test_incoming_collects_both_preflight_commands(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(bs, "EVIDENCE", tmp_path)
    result = bs.incoming(lambda cmd: (0, " ".join(cmd)), "verifier")
    assert result["pass"] is True
    assert set(result["checks"]) == {"T01-toolchain", "T01-contract"}
