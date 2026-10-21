"""Third-party conformance of the formats the project writes.

The audit found that the project wrote files that *looked* like FIT and XDF
but that no standard parser could read: the FIT definition headers used the
wrong bit, the file CRC was missing, the developer-data messages carried the
wrong field numbers, and the "XDF" was an HDF5 file with a made-up layout.

These tests close that hole with parsers that are not part of this project:

* ``fitdecode`` reads the FIT files (and checks the CRCs for us);
* ``pyxdf`` reads the XDF files.

A file that only this project can read is not a product format.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]

fitdecode = pytest.importorskip("fitdecode", reason="fitdecode is a dev dependency")
pytest.importorskip("pyxdf", reason="pyxdf is a project dependency")

from synapse_device.fit import (  # noqa: E402
    DEV_FIELDS,
    FitRecord,
    FitWriter,
    read_fit,
)
from synapse_device.xdf_writer import write_xdf  # noqa: E402


def _write_fit(path: Path, n_records: int = 3) -> Path:
    writer = FitWriter()
    writer.write_file_header()
    writer.write_file_id(serial_number=4242, time_created=1_000_000)
    writer.write_developer_data_id()
    writer.write_field_descriptions()
    for index in range(n_records):
        writer.write_record(
            FitRecord(
                timestamp=1_000_000 + index,
                heart_rate=60 + index,
                dev={
                    "synapse_stress": 1,
                    "synapse_sqi": 200,
                    "synapse_map": 20,
                    "synapse_ecg_quality": 250,
                    "synapse_sleep_stage": 255,
                    "synapse_rmssd": 37,
                    "synapse_sdnn": 42,
                    "synapse_lf_hf": 150,
                },
            )
        )
    writer.finalize(path)
    return path


# ---------------------------------------------------------------------------
# FIT
# ---------------------------------------------------------------------------


def _fit_data_frames(frames: list) -> list:
    """Data messages that carry a record (timestamp + developer values)."""
    out = []
    for frame in frames:
        if not isinstance(frame, fitdecode.records.FitDataMessage):
            continue
        if _value(frame, "timestamp") is None:
            continue
        out.append(frame)
    return out


def _value(frame, name: str):
    """Value of a field of a FIT message, or None when it is absent."""
    try:
        return frame.get_value(name)
    except (KeyError, AttributeError):
        return None


def test_fit_file_is_read_by_a_standard_parser(tmp_path: Path) -> None:
    """Fitdecode must read the file, its messages and its developer fields."""
    path = _write_fit(tmp_path / "session.fit")

    with path.open("rb") as handle:
        frames = list(fitdecode.FitReader(handle))

    header = frames[0]
    assert header.header_size == 14
    assert path.read_bytes()[8:12] == b".FIT"

    records = _fit_data_frames(frames)
    assert len(records) == 3, "the reader found no record messages"

    first = records[0]
    assert _value(first, "heart_rate") == 60
    assert _value(first, "synapse_rmssd") == 37
    assert _value(first, "synapse_sdnn") == 42
    assert _value(first, "synapse_lf_hf") == 150
    assert _value(first, "synapse_sqi") == 200


def test_fit_crc_is_valid_for_a_standard_parser(tmp_path: Path) -> None:
    """The header CRC and the file CRC must match what a parser expects."""
    path = _write_fit(tmp_path / "session.fit")

    with path.open("rb") as handle:
        frames = list(fitdecode.FitReader(handle))

    crc_frames = [f for f in frames if isinstance(f, fitdecode.records.FitCRC)]
    assert crc_frames, "the file has no file CRC"
    assert crc_frames[0].matched is True, "the file CRC does not match its content"

    header = frames[0]
    assert header.crc_matched is True, "the header CRC does not match its content"


def test_fit_developer_fields_are_described(tmp_path: Path) -> None:
    """Every developer field must be described, or a parser cannot name it."""
    path = _write_fit(tmp_path / "session.fit")

    with path.open("rb") as handle:
        frames = list(fitdecode.FitReader(handle))

    described = {
        _value(frame, "field_name")
        for frame in frames
        if isinstance(frame, fitdecode.records.FitDataMessage)
        and _value(frame, "field_name") is not None
    }
    missing = {field.name for field in DEV_FIELDS} - described
    assert not missing, f"developer fields without a description: {sorted(missing)}"


def test_fit_round_trips_through_our_own_reader(tmp_path: Path) -> None:
    """The canonical reader must agree with the canonical writer."""
    path = _write_fit(tmp_path / "session.fit")
    parsed = read_fit(path)

    assert parsed.n_records == 3
    first = parsed.records[0]
    assert first["timestamp"] == 1_000_000
    assert first["heart_rate"] == 60
    assert first["dev"]["synapse_rmssd"] == 37
    assert first["dev"]["synapse_lf_hf"] == 150


def test_a_corrupted_fit_is_rejected(tmp_path: Path) -> None:
    """A flipped byte must break the CRC: the check is not decorative."""
    path = _write_fit(tmp_path / "session.fit")
    data = bytearray(path.read_bytes())
    data[40] ^= 0xFF
    broken = tmp_path / "broken.fit"
    broken.write_bytes(bytes(data))

    with broken.open("rb") as handle:
        frames = list(fitdecode.FitReader(handle))
    crc_frames = [f for f in frames if isinstance(f, fitdecode.records.FitCRC)]
    assert crc_frames, "the corrupted file has no file CRC"
    assert crc_frames[0].matched is False


# ---------------------------------------------------------------------------
# XDF
# ---------------------------------------------------------------------------


def _write_xdf(path: Path, n_samples: int = 500, fs: float = 50.0) -> Path:
    t = np.arange(n_samples) / fs
    signal = np.sin(2 * np.pi * 1.2 * t).reshape(-1, 1)
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
                "data": signal,
                "timestamps": t,
            }
        ],
    )
    return path


def test_xdf_file_is_read_by_a_standard_parser(tmp_path: Path) -> None:
    """Pyxdf must read the file back with the same samples and timestamps."""
    import pyxdf

    path = _write_xdf(tmp_path / "session.xdf")
    streams, _header = pyxdf.load_xdf(str(path))

    assert len(streams) == 1
    stream = streams[0]
    assert stream["info"]["name"][0] == "SYNAPSE_ECG_T0"
    assert stream["info"]["type"][0] == "ECG"
    assert len(stream["time_stamps"]) == 500
    np.testing.assert_allclose(
        stream["time_series"].ravel(),
        np.sin(2 * np.pi * 1.2 * np.arange(500) / 50.0),
        rtol=1e-4,
        atol=1e-4,
    )


def test_xdf_timestamps_are_monotonic_and_regular(tmp_path: Path) -> None:
    """The samples of a stream are spaced by the stream period."""
    import pyxdf

    path = _write_xdf(tmp_path / "session.xdf", n_samples=250, fs=50.0)
    streams, _ = pyxdf.load_xdf(str(path))
    stamps = np.asarray(streams[0]["time_stamps"], dtype=float)

    assert np.all(np.diff(stamps) > 0)
    np.testing.assert_allclose(np.diff(stamps), 1 / 50.0, rtol=1e-3)


# ---------------------------------------------------------------------------
# Export path
# ---------------------------------------------------------------------------


def test_export_produces_a_file_a_standard_parser_reads(tmp_path: Path) -> None:
    """XDF -> FIT through the canonical export, validated by fitdecode."""
    from synapse24.export import export_session_fit

    fs = 100.0
    n = int(fs * 60)
    t = np.arange(n) / fs
    # A synthetic ECG: narrow QRS-like spikes at 72 bpm on a flat baseline.
    period = 1.0 / 1.2
    beat = np.zeros(n)
    for index in range(int(t[-1] / period) + 1):
        beat += np.exp(-0.5 * ((t - index * period) / 0.02) ** 2)
    write_xdf(
        tmp_path / "session.xdf",
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

    out = tmp_path / "session.fit"
    summary = export_session_fit(tmp_path / "session.xdf", out, "SYN-000123", window_s=30.0)
    assert summary.n_records == 2
    assert "ECG" in summary.primary_stream

    with out.open("rb") as handle:
        frames = list(fitdecode.FitReader(handle))
    records = _fit_data_frames(frames)
    assert len(records) == 2
    # The HRV of a 1.2 Hz signal is 500 ms: the export must carry it.
    assert _value(records[0], "synapse_rmssd") is not None
    assert _value(records[0], "heart_rate") == pytest.approx(72, abs=6)


def test_export_refuses_to_analyse_the_wrong_stream(tmp_path: Path) -> None:
    """A session without ECG/PPG must fail loudly, not analyse the IMU."""
    from synapse24.export import ExportError, export_session_fit

    fs = 50.0
    n = int(fs * 10)
    t = np.arange(n) / fs
    write_xdf(
        tmp_path / "imu_only.xdf",
        [
            {
                "name": "SYNAPSE_ACC_T0",
                "type": "IMU",
                "channel_count": 3,
                "sampling_rate": fs,
                "channel_format": "float32",
                "channel_names": ["x", "y", "z"],
                "channel_units": ["mg", "mg", "mg"],
                "source_id": "synapse-test",
                "data": np.zeros((n, 3)),
                "timestamps": t,
            }
        ],
    )

    with pytest.raises(ExportError, match="no analysable stream"):
        export_session_fit(tmp_path / "imu_only.xdf", tmp_path / "out.fit")


# ---------------------------------------------------------------------------
# SDK CLI
# ---------------------------------------------------------------------------


def test_sdk_cli_records_a_file_pyxdf_can_read(tmp_path: Path) -> None:
    """``synapse record`` must leave a real XDF behind."""
    import pyxdf

    out = tmp_path / "synthetic.xdf"
    proc = subprocess.run(  # noqa: S603 - fixed interpreter and module
        [
            sys.executable,
            "-m",
            "synapse_device.cli",
            "record",
            "--out",
            str(out),
            "--duration",
            "1",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        env={**_env_with_sdk(), "PYTHONPATH": str(ROOT / "sdk" / "python")},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.is_file()

    streams, _ = pyxdf.load_xdf(str(out))
    assert streams, "the recorded XDF has no streams"


def test_sdk_cli_moved_commands_point_at_the_host_pipeline(tmp_path: Path) -> None:
    """The commands that moved must say where they went."""
    proc = subprocess.run(  # noqa: S603 - fixed interpreter and module
        [sys.executable, "-m", "synapse_device.cli", "export", "a.xdf", "b.fit"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
        env={**_env_with_sdk(), "PYTHONPATH": str(ROOT / "sdk" / "python")},
    )
    assert proc.returncode == 2
    assert "synapse-export" in proc.stderr


def _env_with_sdk() -> dict[str, str]:
    import os

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(ROOT / "sdk" / "python"))
    return env
