"""The unit of a stream is what its file declares, not what its type suggests.

Two writers put ECG in an XDF: the live LSL path (millivolts) and ``synapse record`` (microvolts).
A report that labels by stream type was wrong for one of them by a factor of 1000.
"""

from __future__ import annotations

import numpy as np
import pytest
from synapse_device.xdf_writer import write_xdf

from synapse24.report.report import _unit_for, build_session_report
from synapse24.sessions.catalog import index_xdf


def _write(path, stream_type: str, units: list[str], channels: int = 1) -> None:
    n = 500
    write_xdf(
        path,
        [
            {
                "name": f"S_{stream_type}",
                "type": stream_type,
                "channel_count": channels,
                "sampling_rate": 100.0,
                "channel_format": "float32",
                "channel_names": [f"c{i}" for i in range(channels)],
                "channel_units": units,
                "source_id": "units-test",
                "data": np.zeros((n, channels)),
                "timestamps": 1_735_689_600.0 + np.arange(n) / 100.0,
            }
        ],
    )


@pytest.mark.parametrize(
    ("stream_type", "units", "channels"),
    [("ECG", ["uV"], 1), ("ECG", ["mV"], 1), ("IMU", ["mg"] * 3, 3), ("IMU", ["dps"] * 3, 3)],
)
def test_the_report_carries_the_unit_the_file_declares(
    tmp_path, stream_type: str, units: list[str], channels: int
) -> None:
    path = tmp_path / "s.xdf"
    _write(path, stream_type, units, channels)

    record = index_xdf(path)
    assert record.streams[0].channel_units == units

    report = build_session_report(record)
    assert report["streams"][0]["unit"] == units[0]


def test_a_stream_that_declares_nothing_falls_back_to_the_type_guess(tmp_path) -> None:
    path = tmp_path / "s.xdf"
    _write(path, "ECG", [""])
    record = index_xdf(path)
    assert record.streams[0].channel_units == [""]
    assert build_session_report(record)["streams"][0]["unit"] == "mV"  # the legacy guess


def test_mixed_units_are_all_reported_not_the_first() -> None:
    assert _unit_for("POWER", ["mV", "mA", "mW", "percent"]) == "mA,mV,mW,percent"


def test_the_sdk_reader_returns_the_declared_unit(tmp_path) -> None:
    from synapse_device.xdf import read_xdf

    path = tmp_path / "s.xdf"
    _write(path, "ECG", ["uV"])
    assert read_xdf(path).streams[0].units == "uV"


def test_the_live_lsl_outlets_declare_the_units_they_push() -> None:
    """The tier-0 outlets used to declare nothing (and the gyroscope was labelled deg/s)."""
    import inspect

    from synapse24.hardware import esp32_tier0

    source = inspect.getsource(esp32_tier0.ESP32Tier0Firmware.setup_lsl_streams)
    assert '_declare_units(ecg_info, ["mV"])' in source
    assert '"rad/s"' in source
    assert '"deg/s"' not in source
