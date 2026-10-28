"""The soak gate's truthfulness.

The G1 gate of Roadmap.md is a 6-hour soak judged by
``data/live_validation/gate_report.json``. A soak has exactly one way to lie:
record nothing (the CLI died, the directory was wrong, the writer was the
in-memory spool after all) and still print a green report. This file pins the
antidotes:

* the artifact section names the WRITER and carries its SHA-256, computed
  from the real bytes;
* a rehearsal marks itself ``rehearsal: true`` and names its artifact as the
  deterministic spool walk, so it can never pass as the G1 evidence;
* the v2 rows (mtu, conn interval, drops by reason, samples per stream) carry
  an explicit source — an unfilled number says why, it never impersonates a
  measurement;
* the LabRecorder harness fails loud: a CLI dying at startup, or a completed
  session with no XDF, must raise instead of soaking over nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_entry import Phase1Validator  # noqa: E402
from tests.test_validator_clock_gate import (  # noqa: E402
    CONFIG,
    LSL_NOW,
    SyntheticPodTransport,
    _logger,
)

DURATION_S = 2.0


@pytest.fixture
def validator(tmp_path: Path) -> Phase1Validator:
    """The synthetic pod on the same path the soak uses."""
    LSL_NOW[0] = 1_000.0
    return Phase1Validator(
        CONFIG,
        _logger(),
        transport=SyntheticPodTransport(offset_ms=0.0, duration_s=DURATION_S),
    )


def _run_soak_rehearsal(validator: Phase1Validator, **kwargs):
    """Drive the rehearsal path exactly like the soak does."""
    kwargs.setdefault("lsl_clock_fn", lambda: float(LSL_NOW[0]))
    kwargs["record"] = True
    kwargs["rehearsal"] = True
    kwargs.setdefault("duration_s", DURATION_S)
    return asyncio.run(validator.run(**kwargs))


def _fake_cli(tmp_path: Path, mode: str) -> Path:
    """A cross-platform stand-in LabRecorderCLI.

    ``artifacts`` writes a small XDF-shaped file into the directory the
    ``-r`` argument names, then lingers LONGER than the harness's
    ``wait_ready`` window (a real recorder stays up through a session);
    ``die`` exits immediately (rc 3); ``silent`` lingers without writing
    anything. All of them are killed by the session teardown.
    """
    if os.name == "nt":
        path = tmp_path / "fake_cli.bat"
        if mode == "artifacts":
            body = (
                "@echo off\r\nset REC=\r\n:parse\r\n"
                'if "%~1"=="" goto emit\r\n'
                'if not "%~1"=="-r" shift& goto parse\r\nshift\r\nset REC=%~1\r\n'
                ":emit\r\n"  # noqa: ERA001
                'if "%REC%"=="" exit /b 3\r\n'
                'echo synapse-fake-xdf>"%REC%\\fake_eeg.xdf"\r\n'
                "ping -n 10 127.0.0.1 > nul\r\nexit /b 0\r\n"
            )
        elif mode == "die":
            body = "@exit /b 3\r\n"
        else:  # silent: alive but writes nothing
            body = "@ping -n 10 127.0.0.1 > nul\r\nexit /b 0\r\n"
        path.write_text(body, encoding="utf-8")
        return path
    path = tmp_path / "fake_cli.sh"
    if mode == "artifacts":
        body = (
            "#!/bin/sh\nrec=''\nwhile [ $# -gt 0 ]; do\n"
            '  case "$1" in\n'
            '    -r) rec="$2"; shift 2 ;;\n'
            "    *) shift ;;\n"
            "  esac\n"
            "done\n"
            '[ -z "$rec" ] && exit 3\n'
            'echo synapse-fake-xdf > "$rec/fake_eeg.xdf"\n'
            "sleep 10\n"
        )
    elif mode == "die":
        body = "#!/bin/sh\nexit 3\n"
    else:
        body = "#!/bin/sh\nsleep 10\n"
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


class TestRehearsalTruth:
    """``--soak-rehearsal`` proves the machinery, and never the soak."""

    def test_rehearsal_names_itself_and_its_artifact(self, validator) -> None:
        report = _run_soak_rehearsal(validator)
        assert report.xdf_path, "the rehearsal must still produce its artifact"

        gate = validator._build_gate_report(report)

        assert gate["artifact"]["source"] == "spool (synthetic rehearsal)"
        assert gate["rehearsal"] is True, "a rehearsal must mark itself"
        assert gate["artifact"]["labrecorder_cli"] is None
        assert gate["artifact"]["labrecorder_version"]["source"], (
            "the missing version must say why, not impersonate one"
        )

    def test_artifact_sha256_fingerprints_the_real_bytes(self, validator) -> None:
        report = _run_soak_rehearsal(validator)
        gate = validator._build_gate_report(report)

        path = Path(gate["artifact"]["path"])
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert gate["artifact"]["sha256"] == expected
        assert gate["artifact_sha256"] == expected


class TestGateReportV2:
    """The v2 rows: every number has a source, no silent assumptions."""

    def test_the_v2_rows_exist_and_carry_a_source(self, validator) -> None:
        report = _run_soak_rehearsal(validator)
        gate = validator._build_gate_report(report)

        for row in ("mtu", "conn_interval_ms"):
            assert row in gate, f"v2 row missing: {row}"
            assert gate[row]["source"], f"{row} needs an explicit source"

        assert gate["mtu"]["value"] is None, (
            "the synthetic transport cannot negotiate; None means unknown, never 247"
        )

        for reason in (
            "sequence_gap_frames",
            "pod_reported_samples",
            "decode_rejected",
            "reconnect_restarts",
        ):
            assert reason in gate["drops_by_reason"], f"drop reason missing: {reason}"

        assert gate["frames_rejected"] == 0
        assert gate["samples_by_stream"]["SYNAPSE_ECG_T0"] > 0

    def test_drops_by_reason_breaks_out_the_restarts(self, validator) -> None:
        """A typed restart counts in rows and never as a phantom 65k loss."""
        from synapse24.hardware.raw_codec import RawFormat, RawStream, encode_frame
        from synapse24.hardware.research_client import ResearchStreamDecoder

        decoder = ResearchStreamDecoder()
        values = [100, 200]
        decoder.feed(encode_frame(RawStream.ECG, 5, 12_000, 0, RawFormat.S16, values))
        decoder.feed(encode_frame(RawStream.ECG, 0, 6_000, 0, RawFormat.S16, values))

        assert decoder.restart_summary() == {"ECG": 1}
        assert decoder.drops == []

        report = _run_soak_rehearsal(validator)
        gate = validator._build_gate_report(report)
        assert "reconnect_restarts" in gate["drops_by_reason"]


class TestRecorderTruth:
    """The harness fails loud instead of soaking over nothing."""

    def test_recorder_dying_during_startup_is_refused(self, tmp_path: Path, monkeypatch) -> None:
        """wait_ready must reject a CLI that dies inside the window.

        Built directly (Popen + RecordedSession) so the refused startup is
        asserted on its own error, not on the teardown's no-XDF raise.
        """
        import subprocess

        from synapse24.utils.labrecorder import CLI_ENV_VAR, LabRecorderError, RecordedSession

        fake = _fake_cli(tmp_path, "die")
        monkeypatch.setenv(CLI_ENV_VAR, str(fake))
        process = subprocess.Popen([str(fake)])
        try:
            session = RecordedSession(
                process=process,
                config_path=tmp_path / "cfg.cfg",
                record_dir=tmp_path / "rec",
            )
            with pytest.raises(LabRecorderError) as excinfo:
                session.wait_ready(timeout_s=2.0)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        assert "exited during startup" in str(excinfo.value)
        assert "code 3" in str(excinfo.value)

    def test_completed_recording_without_an_xdf_raises(self, tmp_path: Path, monkeypatch) -> None:
        from synapse24.utils.labrecorder import CLI_ENV_VAR, LabRecorderError, record_session

        monkeypatch.setenv(CLI_ENV_VAR, str(_fake_cli(tmp_path, "silent")))
        with (
            pytest.raises(LabRecorderError) as excinfo,
            record_session(tmp_path / "rec", stream_specs=[]),
        ):
            pass  # the body ran cleanly: the missing file is the failure
        assert "produced no XDF" in str(excinfo.value)

    def test_soak_records_the_writer_and_its_fingerprint(self, tmp_path: Path, monkeypatch) -> None:
        """Point the report at the stand-in recorder's file.

        Full soak path against a fake LabRecorderCLI: one writer, one hash,
        and the report referencing THAT file instead of the spool.
        """
        from synapse24.utils.labrecorder import CLI_ENV_VAR

        monkeypatch.setenv(CLI_ENV_VAR, str(_fake_cli(tmp_path, "artifacts")))
        validator = Phase1Validator(
            CONFIG,
            _logger(),
            transport=SyntheticPodTransport(offset_ms=0.0, duration_s=DURATION_S),
        )
        LSL_NOW[0] = 1_000.0
        report = asyncio.run(
            validator.run(
                duration_s=DURATION_S,
                record=True,
                lsl_clock_fn=lambda: float(LSL_NOW[0]),
            )
        )
        gate = validator._build_gate_report(report)

        assert gate["artifact"]["source"] == "LabRecorderCLI"
        assert gate["artifact"]["labrecorder_cli"]
        assert gate["rehearsal"] is False
        path = Path(gate["artifact"]["path"])
        assert path.exists()
        assert gate["artifact_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
