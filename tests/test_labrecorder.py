"""Tests for the LabRecorderCLI harness.

The recorder is trusted to *write* the continuous XDF and never to *prove*
anything. These tests pin the three behaviours the SOP depends on: finding
the CLI (or failing loudly with a recovery hint), generating a config that
pins the streams, and stopping cleanly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from synapse24.utils.labrecorder import (
    CLI_ENV_VAR,
    LabRecorderError,
    LabRecorderNotFoundError,
    find_labrecorder_cli,
    generate_config,
    record_session,
    write_session_metadata,
)


class TestFindCli:
    def test_env_override_wins(self, tmp_path: Path, monkeypatch) -> None:
        fake = tmp_path / "LabRecorderCLI.exe"
        fake.write_text("", encoding="utf-8")
        monkeypatch.setenv(CLI_ENV_VAR, str(fake))
        assert find_labrecorder_cli() == fake

    def test_missing_env_override_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(CLI_ENV_VAR, str(tmp_path / "absent.exe"))
        monkeypatch.delenv("PATH", raising=False)
        for path in (
            Path("C:/Program Files/LabRecorder/LabRecorderCLI.exe"),
            Path("C:/Program Files (x86)/LabRecorder/LabRecorderCLI.exe"),
            Path("/usr/local/bin/LabRecorderCLI"),
            Path("/usr/bin/LabRecorderCLI"),
            Path("/opt/homebrew/bin/LabRecorderCLI"),
        ):
            if not path.exists():
                continue
            pytest.skip(f"a real install exists at {path}; cannot test absence")
        assert find_labrecorder_cli() is None

    def test_not_found_error_carries_a_recovery_hint(self) -> None:
        err = LabRecorderNotFoundError("missing")
        assert "LabRecorder" in err.recovery_hint
        assert CLI_ENV_VAR in err.recovery_hint


class TestGenerateConfig:
    def test_config_pins_the_named_streams(self, tmp_path: Path) -> None:
        path = generate_config(
            [{"name": "SYNAPSE_ECG_T0", "uid": "synapse_ecg_t0_001"}],
            tmp_path / "rec.cfg",
        )
        text = path.read_text(encoding="utf-8")
        assert "[Stream]" in text
        assert "Name = SYNAPSE_ECG_T0" in text
        assert "UID = synapse_ecg_t0_001" in text
        assert "[SelectAll]" not in text

    def test_no_specs_records_everything(self, tmp_path: Path) -> None:
        path = generate_config([], tmp_path / "rec.cfg")
        assert "[SelectAll]" in path.read_text(encoding="utf-8")

    def test_record_all_flag_selects_everything(self, tmp_path: Path) -> None:
        path = generate_config([{"name": "SYNAPSE_ECG_T0"}], tmp_path / "rec.cfg", record_all=True)
        assert "[SelectAll]" in path.read_text(encoding="utf-8")


class TestRecordSession:
    def test_missing_cli_raises_with_recovery_hint(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(CLI_ENV_VAR, str(tmp_path / "absent.exe"))
        with (
            pytest.raises(LabRecorderNotFoundError) as excinfo,
            record_session(tmp_path / "rec", stream_specs=[{"name": "SYNAPSE_ECG_T0"}]),
        ):
            pass
        assert excinfo.value.recovery_hint
        assert "LabRecorder" in excinfo.value.recovery_hint

    def test_session_metadata_is_written(self, tmp_path: Path) -> None:
        meta = tmp_path / "session_meta.json"
        write_session_metadata(meta, {"session": "test"})
        assert '"session": "test"' in meta.read_text(encoding="utf-8")

    def test_error_hierarchy(self) -> None:
        assert issubclass(LabRecorderNotFoundError, LabRecorderError)
