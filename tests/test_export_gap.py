"""The FIT export places heart beats where they happened, not where their sample index says.

A lost frame removes samples; the stream timestamps keep the real time. HRV computed from sample
indices read the interval across the hole as shorter than it was, so one lost frame in a window
produced a large fake RMSSD and a wrong heart rate in the file Averyn shows.
"""

from __future__ import annotations

import numpy as np
import pytest
from synapse_device.xdf_writer import write_xdf

fitdecode = pytest.importorskip("fitdecode", reason="fitdecode is a dev dependency")

FS = 100.0
RR_S = 0.8  # 75 bpm, perfectly regular: any RMSSD is an artefact
T0 = 1_735_689_600.0  # 2025-01-01T00:00:00Z, wall clock


def _write(path, remove: tuple[float, float] | None) -> None:
    n = int(FS * 90)
    t = T0 + np.arange(n) / FS
    beat = np.zeros(n)
    for k in range(int(90 / RR_S) + 1):
        beat += np.exp(-0.5 * ((t - T0 - 0.4 - k * RR_S) / 0.02) ** 2)
    keep = np.ones(n, dtype=bool)
    if remove is not None:
        # a hole between two beats (beats sit at 0.4 + k * 0.8): no beat is lost, time is
        keep = ~((t - T0 >= remove[0]) & (t - T0 < remove[1]))
    write_xdf(
        path,
        [
            {
                "name": "SYNAPSE_ECG_T0",
                "type": "ECG",
                "channel_count": 1,
                "sampling_rate": FS,
                "channel_format": "float32",
                "channel_names": ["ecg_uv"],
                "channel_units": ["uV"],
                "source_id": "synapse-test",
                "data": beat[keep].reshape(-1, 1),
                "timestamps": t[keep],
            }
        ],
    )


def _records(path):
    with path.open("rb") as handle:
        frames = list(fitdecode.FitReader(handle))
    out = []
    for frame in frames:
        if isinstance(frame, fitdecode.records.FitDataMessage) and frame.name == "record":
            out.append(frame)
    return out


def _field(frame, name: str):
    try:
        return frame.get_value(name)
    except (KeyError, AttributeError):
        return None


def _export(tmp_path, remove):
    from synapse24.export import export_session_fit

    xdf = tmp_path / "s.xdf"
    out = tmp_path / "s.fit"
    _write(xdf, remove)
    export_session_fit(xdf, out, "SYN-000001", window_s=30.0)
    return _records(out)


def test_a_hole_between_two_beats_does_not_change_the_heart_rate_or_invent_variability(
    tmp_path,
) -> None:
    clean = _export(tmp_path, None)
    # 0.4 s of samples gone between the beats at 12.4 s and 13.2 s (13.6 s is the next one's
    # window edge; the hole [12.6, 13.0) holds no beat)
    gap_dir = tmp_path / "gap"
    gap_dir.mkdir()
    holed = _export(gap_dir, (12.6, 13.0))

    assert len(clean) == len(holed)
    for a, b in zip(clean, holed, strict=True):
        assert _field(b, "heart_rate") == _field(a, "heart_rate") == 75
        # perfectly regular beats: whatever RMSSD the file carries is the hole's doing
        assert _field(b, "synapse_rmssd") == _field(a, "synapse_rmssd")


def test_the_gap_free_baseline_is_regular(tmp_path) -> None:
    clean = _export(tmp_path, None)
    assert all((_field(r, "synapse_rmssd") or 0) <= 5 for r in clean)
