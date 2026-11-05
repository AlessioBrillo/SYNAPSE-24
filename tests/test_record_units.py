"""`synapse record` writes the units it declares.

The XDF said ``dps`` and ``uT`` for the gyroscope and the magnetometer while writing the wire integers
(mrad/s and 0.01 uT): a gyroscope 17.45 times and a magnetometer 100 times off, in a file that looks
perfectly fine.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from synapse_device import record  # noqa: E402
from synapse_device.codec import RawFormat, RawStream, decode_frame, encode_frame  # noqa: E402

GATT_V2 = ROOT / "contracts" / "gatt_v2.json"


def _payload(stream: RawStream, values: list[int]) -> dict[str, object]:
    fmt = RawFormat.S32 if stream is RawStream.PPG else RawFormat.S16
    header, decoded = decode_frame(encode_frame(stream, 0, 1_000_000, 0, fmt, values))
    payload, _rates = record._xdf_payload(
        [(header, decoded, 1_000_000)], [stream], "synapse-device-test", 1_000.0
    )
    return payload[0]


def test_the_scale_table_is_the_contracts() -> None:
    streams = {
        s["name"]: s for s in json.loads(GATT_V2.read_text("utf-8"))["stream_metadata"]["streams"]
    }
    for stream, factor in record.DISPLAY_PER_WIRE.items():
        assert factor == pytest.approx(streams[stream.name.lower()]["display_per_wire"]), stream


def test_a_gyroscope_reading_is_written_in_degrees_per_second() -> None:
    # 1 rad/s on the wire is 1000 mrad/s
    stream = _payload(RawStream.GYRO, [1000, 0, -1000])
    assert stream["channel_units"] == ["dps", "dps", "dps"]
    data = np.asarray(stream["data"])
    assert data[0, 0] == pytest.approx(180.0 / math.pi)  # 57.2958 deg/s
    assert data[0, 2] == pytest.approx(-180.0 / math.pi)


def test_a_magnetometer_reading_is_written_in_microtesla() -> None:
    # 0.01 uT per count on the wire: 4000 counts are 40 uT
    stream = _payload(RawStream.MAG, [3000, -1200, 4000])
    assert stream["channel_units"] == ["uT", "uT", "uT"]
    data = np.asarray(stream["data"])
    assert data[0].tolist() == pytest.approx([30.0, -12.0, 40.0])


@pytest.mark.parametrize(
    ("stream", "values", "unit"),
    [
        (RawStream.ECG, [1000], "uV"),
        (RawStream.ACC, [10, -20, 1000], "mg"),
        (RawStream.PPG, [100_000, 120_000], "au"),
    ],
)
def test_streams_whose_wire_unit_is_the_display_unit_are_not_scaled(
    stream: RawStream, values: list[int], unit: str
) -> None:
    result = _payload(stream, values)
    assert result["channel_units"][0] == unit  # type: ignore[index]
    assert np.asarray(result["data"])[0].tolist() == pytest.approx([float(v) for v in values])
