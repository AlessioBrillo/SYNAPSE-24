"""Tests per l'ancora UTC nell'export FIT (P0-4).

I timestamp XDF vivono nel dominio LSL, non Unix epoch.  L'export deve
mappare LSL→Unix tramite un'ancora (utc0 al primo campione), mai con
``datetime.fromtimestamp(lsl_ts)`` che produrrebbe date del 1970.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from synapse_device.fit import read_fit
from synapse_device.xdf_writer import write_xdf

fitdecode = pytest.importorskip("fitdecode", reason="fitdecode is a dev dependency")


def _write_ecg_xdf(path, lsl_t0: float, duration_s: float = 60.0, fs: float = 100.0):
    n = int(fs * duration_s)
    t = lsl_t0 + np.arange(n) / fs
    beat = np.zeros(n)
    period = 1.0 / 1.2
    # Only the beats inside the window contribute: iterating from the epoch
    # (0 .. t[-1]) is ~2 billion gaussians when lsl_t0 is Unix-scale.
    for index in range(int(np.floor(t[0] / period)), int(np.floor(t[-1] / period)) + 1):
        beat += np.exp(-0.5 * ((t - index * period) / 0.02) ** 2)
    write_xdf(
        path,
        [
            {
                "name": "SYNAPSE_ECG_T0",
                "type": "ECG",
                "channel_count": 1,
                "sampling_rate": fs,
                "channel_format": "float32",
                "channel_names": ["ecg_uv"],
                "channel_units": ["uV"],
                "source_id": "synapse-test",
                "data": beat.reshape(-1, 1),
                "timestamps": t,
            }
        ],
    )


def _data_messages(frames, name: str):
    """Keep the fitdecode data messages by name (definitions have no values)."""
    return [
        frame
        for frame in frames
        if isinstance(frame, fitdecode.FitDataMessage) and frame.name == name
    ]


def test_export_uses_utc_anchor_not_1970(tmp_path):
    """Con un'ancora UTC, i record FIT hanno date coerenti (non 1970)."""
    from synapse24.export import export_session_fit

    lsl_t0 = 1_700_000_000.0  # LSL tempo arbitrario (non Unix epoch)
    utc0 = 1_735_689_600.0  # 2025-01-01T00:00:00Z corrispondente a lsl_t0
    xdf = tmp_path / "session.xdf"
    _write_ecg_xdf(xdf, lsl_t0)

    out = tmp_path / "session.fit"
    summary = export_session_fit(xdf, out, "SYN-000123", window_s=30.0, utc0_s=utc0)
    assert summary.n_records == 2

    with out.open("rb") as handle:
        frames = list(fitdecode.FitReader(handle))

    records = _data_messages(frames, "record")
    assert len(records) == 2

    # Ogni record timestamp deve essere >= utc0 (prima finestra parte a t0).
    for rec in records:
        ts = rec.get_value("timestamp")
        assert isinstance(ts, datetime)  # fitdecode 0.11 decodes FIT epoch → UTC datetime
        assert ts >= datetime.fromtimestamp(utc0, tz=UTC)

    # Il file_id time_created deve essere anch'esso coerente (>= utc0).
    file_ids = _data_messages(frames, "file_id")
    assert len(file_ids) == 1
    tc = file_ids[0].get_value("time_created")
    assert isinstance(tc, datetime)
    assert tc >= datetime.fromtimestamp(utc0, tz=UTC)


def test_export_without_utc0_does_not_produce_1970(tmp_path):
    """Senza ancora UTC si usa time.time() come approssimazione (mai 1970)."""
    from synapse24.export import export_session_fit

    lsl_t0 = 1_700_000_000.0
    xdf = tmp_path / "session.xdf"
    _write_ecg_xdf(xdf, lsl_t0)

    out = tmp_path / "session.fit"
    summary = export_session_fit(xdf, out, "SYN-000123", window_s=30.0)
    assert summary.n_records == 2
    assert len(summary.warnings) == 1
    assert "UTC anchor" in summary.warnings[0]

    with out.open("rb") as handle:
        frames = list(fitdecode.FitReader(handle))
    file_ids = _data_messages(frames, "file_id")
    assert len(file_ids) == 1
    tc = file_ids[0].get_value("time_created")
    assert isinstance(tc, datetime)
    # Deve essere una data recente, non il 1970.
    assert tc.timestamp() > 1_700_000_000
