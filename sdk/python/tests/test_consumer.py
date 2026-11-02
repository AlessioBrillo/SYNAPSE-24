# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Consumer contract v2 codec: decode, sentinels, metadata and control writes.

The firmware encoder and ``contracts/gatt_v2.json`` are cross-checked in
``tests/test_consumer_v2_parity.py``; these tests pin the codec's own behaviour
so the SDK suite covers it on its own.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path

import pytest

from synapse_device import consumer

VECTORS = Path(__file__).resolve().parents[3] / "contracts" / "vectors" / "consumer_v2.json"


def _vector(name: str) -> dict[str, object]:
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    return next(case for case in doc["feature"] if case["name"] == name)


def test_a_golden_vector_decodes_to_its_values() -> None:
    case = _vector("typical_resting")
    decoded = consumer.decode_feature(bytes.fromhex(str(case["hex"])))
    values = case["values"]
    assert isinstance(values, dict)
    assert {k: decoded[k] for k in values} == values
    assert decoded["version"] == consumer.FEATURE_VERSION


def test_not_available_is_none_and_never_zero() -> None:
    decoded = consumer.decode_feature(bytes.fromhex(str(_vector("all_not_available")["hex"])))
    for name in ("heart_rate_bpm", "rr_last", "rr_count", "ppg_sqi", "ecg_quality", "motion_level"):
        assert decoded[name] is None
    assert decoded["battery_pct"] is None
    assert decoded["temperature_c100"] is None
    assert decoded["stress_level"] == 3


def test_a_negative_temperature_survives_the_round_trip() -> None:
    raw = consumer.encode_feature({"temperature_c100": -1234})
    assert consumer.decode_feature(raw)["temperature_c100"] == -1234


def test_decode_rejects_a_wrong_length_or_version() -> None:
    good = consumer.encode_feature({})
    with pytest.raises(consumer.ConsumerCodecError, match="expected"):
        consumer.decode_feature(good + b"\x00")
    with pytest.raises(consumer.ConsumerCodecError, match="version"):
        consumer.decode_feature(bytes([9]) + good[1:])


def test_encode_rejects_what_the_wire_cannot_carry() -> None:
    with pytest.raises(consumer.ConsumerCodecError, match="unknown"):
        consumer.encode_feature({"nope": 1})
    with pytest.raises(consumer.ConsumerCodecError, match="outside"):
        consumer.encode_feature({"sequence": 70000})
    with pytest.raises(consumer.ConsumerCodecError, match="sentinel"):
        consumer.encode_feature({"battery_pct": 255})
    with pytest.raises(consumer.ConsumerCodecError, match="version"):
        consumer.encode_feature({"version": 1})


def test_status_decodes_every_field() -> None:
    raw = bytearray(consumer.STATUS_BYTES)
    raw[0] = consumer.STATUS_VERSION
    struct.pack_into("<H", raw, 4, 247)
    struct.pack_into("<I", raw, 28, 0xDEADBEEF)
    status = consumer.decode_status(bytes(raw))
    assert status["mtu"] == 247
    assert status["fw_build"] == 0xDEADBEEF
    with pytest.raises(consumer.ConsumerCodecError):
        consumer.decode_status(bytes(raw)[:-1])


def test_stream_metadata_decodes_units_and_rejects_garbage() -> None:
    entry = struct.pack("<BBBBHH", 1, 2, 1, 1, 500, 1000)
    rows = consumer.decode_stream_metadata(bytes([1]) + entry)
    assert rows == [consumer.StreamMeta(1, 2, 1, "uV", 500, 1000)]
    with pytest.raises(consumer.ConsumerCodecError, match="empty"):
        consumer.decode_stream_metadata(b"")
    with pytest.raises(consumer.ConsumerCodecError, match="entries need"):
        consumer.decode_stream_metadata(bytes([2]) + entry)
    bad_unit = struct.pack("<BBBBHH", 1, 2, 1, 99, 500, 1000)
    with pytest.raises(consumer.ConsumerCodecError, match="unit"):
        consumer.decode_stream_metadata(bytes([1]) + bad_unit)


def test_control_writes() -> None:
    assert consumer.encode_control("start") == b"\x01"
    assert consumer.encode_control("stop") == b"\x02"
    assert consumer.encode_control("set_feature_rate", 5) == b"\x03\x05"
    with pytest.raises(consumer.ConsumerCodecError, match="outside"):
        consumer.encode_control("set_feature_rate", 6)
    with pytest.raises(consumer.ConsumerCodecError, match="needs"):
        consumer.encode_control("set_feature_rate")
    with pytest.raises(consumer.ConsumerCodecError, match="no argument"):
        consumer.encode_control("start", 1)
    with pytest.raises(consumer.ConsumerCodecError, match="unknown"):
        consumer.encode_control("reboot")
