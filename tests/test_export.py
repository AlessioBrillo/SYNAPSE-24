"""Exporting a real session must not need a manual --utc0-s flag.

The anchor (UTC epoch at a known LSL instant) is carried by the gate report /
session metadata or by the XDF stream's own metadata. The CLI flag stays as an
override.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

fitdecode = pytest.importorskip("fitdecode", reason="fitdecode is a dev dependency")

from synapse24.export import ExportError, TimeAnchor, export_session_fit  # noqa: E402
from synapse24.utils import StreamConfig, write_xdf  # noqa: E402

UTC0 = 1_735_689_600.0  # 2025-01-01T00:00:00Z
LSL_T0 = 12_345.0


def _write_ecg_xdf(path: Path, lsl_t0: float, metadata: dict | None = None) -> None:
    fs = 100.0
    duration = 60.0
    n = int(fs * duration)
    t = lsl_t0 + np.arange(n) / fs
    beat = np.zeros(n)
    period = 1.0 / 1.2
    for index in range(int(np.floor(t[0] / period)), int(np.floor(t[-1] / period)) + 1):
        beat += np.exp(-0.5 * ((t - index * period) / 0.02) ** 2)
    write_xdf(
        path,
        [
            {
                "data": beat.reshape(-1, 1),
                "timestamps": t,
                "info": StreamConfig(
                    name="SYNAPSE_ECG_T0",
                    stream_type="ECG",
                    channel_count=1,
                    sampling_rate=fs,
                    channel_names=["ECG"],
                    channel_units=["mV"],
                    source_id="synapse24_ecg_t0_SYN-000123",
                    metadata=metadata or {},
                ),
            }
        ],
    )


def _first_record_time(path: Path) -> datetime:
    with path.open("rb") as handle:
        for frame in fitdecode.FitReader(handle):
            if isinstance(frame, fitdecode.FitDataMessage) and frame.name == "record":
                value = frame.get_value("timestamp")
                assert isinstance(value, datetime)
                return value
    raise AssertionError("no record in FIT file")


def test_time_anchor_reads_nested_gate_report() -> None:
    anchor = TimeAnchor.from_mapping({"result": {"clock": {"utc0_s": UTC0, "lsl_t0_s": LSL_T0}}})
    assert anchor == TimeAnchor(utc0_s=UTC0, lsl_t0_s=LSL_T0)


def test_legacy_lsl0_s_report_is_still_exportable() -> None:
    """Gate reports are append-only: a session written before the rename must export."""
    anchor = TimeAnchor.from_mapping({"utc0_s": UTC0, "lsl0_s": LSL_T0})
    assert anchor == TimeAnchor(utc0_s=UTC0, lsl_t0_s=LSL_T0)


def test_legacy_lsl0_s_anchor_dates_the_fit(tmp_path: Path) -> None:
    xdf = tmp_path / "legacy.xdf"
    out = tmp_path / "legacy.fit"
    first_sample_lsl = LSL_T0 + 5.0
    _write_ecg_xdf(xdf, first_sample_lsl)

    export_session_fit(
        xdf,
        out,
        "SYN-000123",
        window_s=30.0,
        session_metadata={"utc0_s": UTC0, "lsl0_s": LSL_T0},
    )
    assert _first_record_time(out).timestamp() == pytest.approx(UTC0 + 5.0, abs=2.0)


def test_session_metadata_anchor_dates_the_fit_without_a_flag(tmp_path: Path) -> None:
    xdf = tmp_path / "session.xdf"
    out = tmp_path / "session.fit"
    _write_ecg_xdf(xdf, LSL_T0)

    summary = export_session_fit(
        xdf,
        out,
        "SYN-000123",
        window_s=30.0,
        session_metadata={"utc0_s": UTC0, "lsl_t0_s": LSL_T0},
    )

    assert summary.n_records == 2
    assert any("session metadata" in w for w in summary.warnings)
    assert _first_record_time(out) >= datetime.fromtimestamp(UTC0, tz=UTC)


def test_recorded_lsl_anchor_is_used_not_the_file_first_sample(tmp_path: Path) -> None:
    """A session that started before the first ECG sample keeps its true date."""
    xdf = tmp_path / "session.xdf"
    out = tmp_path / "session.fit"
    first_sample_lsl = LSL_T0 + 5.0
    _write_ecg_xdf(xdf, first_sample_lsl)

    export_session_fit(
        xdf,
        out,
        "SYN-000123",
        window_s=30.0,
        session_metadata={"utc0_s": UTC0, "lsl_t0_s": LSL_T0},
    )
    first = _first_record_time(out).timestamp()
    assert first == pytest.approx(UTC0 + 5.0, abs=2.0)


def test_session_metadata_can_be_a_json_path(tmp_path: Path) -> None:
    xdf = tmp_path / "session.xdf"
    out = tmp_path / "session.fit"
    report = tmp_path / "gate_report.json"
    report.write_text(json.dumps({"clock": {"utc0_s": UTC0, "lsl_t0_s": LSL_T0}}))
    _write_ecg_xdf(xdf, LSL_T0)

    summary = export_session_fit(xdf, out, "SYN-000123", window_s=30.0, session_metadata=report)
    assert summary.n_records == 2


def test_explicit_utc0_flag_overrides_the_recorded_anchor(tmp_path: Path) -> None:
    xdf = tmp_path / "session.xdf"
    out = tmp_path / "session.fit"
    _write_ecg_xdf(xdf, LSL_T0)
    override = UTC0 + 86_400.0

    export_session_fit(
        xdf,
        out,
        "SYN-000123",
        window_s=30.0,
        utc0_s=override,
        session_metadata={"utc0_s": UTC0, "lsl_t0_s": LSL_T0},
    )
    assert _first_record_time(out).timestamp() >= override


def test_self_describing_xdf_carries_its_own_anchor(tmp_path: Path) -> None:
    xdf = tmp_path / "session.xdf"
    out = tmp_path / "session.fit"
    _write_ecg_xdf(xdf, LSL_T0, metadata={"utc0_s": str(UTC0), "lsl_t0_s": str(LSL_T0)})

    summary = export_session_fit(xdf, out, "SYN-000123", window_s=30.0)

    assert summary.n_records == 2
    assert any("XDF stream metadata" in w for w in summary.warnings)


def test_cli_accepts_session_metadata_and_explicit_override(tmp_path: Path) -> None:
    from synapse24.export import main

    xdf = tmp_path / "session.xdf"
    out = tmp_path / "session.fit"
    _write_ecg_xdf(xdf, LSL_T0)
    report = tmp_path / "gate.json"
    report.write_text(json.dumps({"utc0_s": UTC0, "lsl_t0_s": LSL_T0}))

    assert main([str(xdf), str(out), "--session-metadata", str(report)]) == 0
    assert out.is_file()

    override_out = tmp_path / "override.fit"
    assert (
        main(
            [
                str(xdf),
                str(override_out),
                "--session-metadata",
                str(report),
                "--utc0-s",
                str(UTC0 + 86_400.0),
            ]
        )
        == 0
    )
    assert _first_record_time(override_out).timestamp() >= UTC0 + 86_400.0


def test_local_clock_without_any_anchor_is_still_refused(tmp_path: Path) -> None:
    xdf = tmp_path / "session.xdf"
    out = tmp_path / "session.fit"
    _write_ecg_xdf(xdf, LSL_T0)

    with pytest.raises(ExportError, match="local LSL clock"):
        export_session_fit(xdf, out, "SYN-000123", window_s=30.0)
    assert not out.exists()
