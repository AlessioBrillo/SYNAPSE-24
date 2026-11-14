#!/usr/bin/env python3
"""Regenerate the golden vectors of the consumer contract and of the raw frame.

* ``contracts/vectors/consumer_v2.json``: feature and status records, the stream metadata, the
  control point (writes and refusals), the standard Heart Rate Measurement and a feature sequence
  across the 16-bit wrap, from ``synapse_device.consumer``;
* ``contracts/vectors/raw_frame_v1.json``: raw research frames, from
  ``synapse_device.codec.encode_frame``.

Averyn vendors both files (and checks their sha256) to test its parsers, so the bytes must come
from the SDK encoders and never from a hand-written struct. ``--check`` fails when a committed
file differs, and runs in ``tests/test_consumer_v2_parity.py``.

    uv run python scripts/make_consumer_vectors.py          # write
    uv run python scripts/make_consumer_vectors.py --check  # fail on drift
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from synapse_device import codec, consumer  # noqa: E402

GATT_V2 = ROOT / "contracts" / "gatt_v2.json"
_CONTRACT: dict[str, Any] = json.loads(GATT_V2.read_text(encoding="utf-8"))
#: The contract version, read from the generated contract (hardware.yaml is its only source).
CONSUMER_CONTRACT_VERSION: str = _CONTRACT["version"]

OUT_CONSUMER = ROOT / "contracts" / "vectors" / "consumer_v2.json"
OUT_RAW = ROOT / "contracts" / "vectors" / "raw_frame_v1.json"

# `values` use null for "not available"; a missing optional field is "not available" too.
FEATURE_CASES: list[tuple[str, dict[str, int | None]]] = [
    (
        "all_not_available",
        {"flags": 0, "sequence": 0, "t_dev_ms": 0, "sensor_health": 0},
    ),
    (
        "typical_resting",
        {
            "flags": 4,
            "sequence": 42,
            "t_dev_ms": 3_600_000,
            "stress_level": 0,
            "sensor_health": 7,
            "heart_rate_bpm": 62,
            "rr_last": 991,  # 968 ms in 1/1024 s: round(968 * 1024 / 1000)
            "rr_count": 1,
            "ppg_sqi": 230,
            "ecg_quality": 200,
            "motion_level": 12,
            "motion_artifact": 20,
            "battery_pct": 87,
            "temperature_c100": 3345,
            "rmssd_ms": 48,
            "sdnn_ms": 61,
        },
    ),
    (
        "negative_temperature",
        {
            "flags": 0,
            "sequence": 0,
            "t_dev_ms": 0,
            "sensor_health": 0,
            "temperature_c100": -1234,
            "heart_rate_bpm": 0,
        },
    ),
    (
        "stress_class_unknown_is_not_na",
        {"flags": 4, "sequence": 7, "t_dev_ms": 1000, "stress_level": 3, "sensor_health": 3},
    ),
    (
        "charging_with_power_sensor",
        {"flags": 5, "sequence": 8, "t_dev_ms": 2000, "sensor_health": 0x1F, "battery_pct": 100},
    ),
    (
        "extremes",
        {
            "flags": 15,
            "sequence": 65535,
            "t_dev_ms": 4_294_967_295,
            "stress_level": 2,
            "sport_type": 4,
            "sensor_health": 31,
            "heart_rate_bpm": 300,
            "rr_last": 65534,
            "rr_count": 254,
            "ppg_sqi": 254,
            "ecg_quality": 254,
            "motion_level": 254,
            "motion_artifact": 254,
            "battery_pct": 100,
            "temperature_c100": 32766,
            "rmssd_ms": 65534,
            "sdnn_ms": 65534,
        },
    ),
]

STATUS_CASES: list[tuple[str, dict[str, int | None]]] = [
    ("fresh_boot", {"tier": 0, "sensor_health": 0, "flags": 0, "mtu": 23}),
    (
        "running_with_losses",
        {
            "tier": 1,
            "sensor_health": 0x17,
            "flags": 0x04,
            "mtu": 247,
            "uptime_s": 86_400,
            "epoch_wraps": 3,
            "ring_overflow": 11,
            "stale_samples": 22,
            "dropped_frames": 33,
            "read_errors": 44,
            "fw_build": 0xDEADBEEF,
            "ppg_skipped": 55,
            "power_dropped": 66,
        },
    ),
]

# name, stream, sequence, timestamp_us (low 32 bits), flags, format, values (channel-minor), dropped
RAW_CASES: list[tuple[str, codec.RawStream, int, int, int, codec.RawFormat, list[int], int]] = [
    (
        "ecg_s16_extremes",
        codec.RawStream.ECG,
        1,
        1_000_000,
        0,
        codec.RawFormat.S16,
        [0, 100, -100, 32767, -32768],
        0,
    ),
    (
        "ppg_s32_two_channels",
        codec.RawStream.PPG,
        65535,
        2_000_000,
        0,
        codec.RawFormat.S32,
        [100_000, 120_000, 100_100, 120_100],
        0,
    ),
    (
        "acc_s16_three_channels",
        codec.RawStream.ACC,
        7,
        3_000_000,
        0,
        codec.RawFormat.S16,
        [10, -20, 1000, 12, -22, 998],
        0,
    ),
    (
        "timestamp_counter_wrapped",
        codec.RawStream.GYRO,
        8,
        0x00000100,
        codec.FLAG_TS_EPOCH_WRAPPED,
        codec.RawFormat.S16,
        [1, 2, 3],
        0,
    ),
    (
        "loss_reported_and_saturated",
        codec.RawStream.MAG,
        9,
        4_000_000,
        0,
        codec.RawFormat.S16,
        [-5, 6, -7],
        300,  # the encoder saturates the byte at 255
    ),
    (
        "power_s32_four_channels",
        codec.RawStream.POWER,
        3,
        5_000_000,
        0,
        codec.RawFormat.S32,
        [3_700, -42, 155, 87],
        0,
    ),
]


def _raw_vectors() -> dict[str, Any]:
    valid = []
    frames: dict[str, bytes] = {}
    for name, stream, seq, ts, flags, fmt, values, dropped in RAW_CASES:
        frame = codec.encode_frame(stream, seq, ts, flags, fmt, values, dropped=dropped)
        frames[name] = frame
        valid.append(
            {
                "name": name,
                "hex": frame.hex(),
                "expected": {
                    "stream": stream.name.lower(),
                    "sequence": seq,
                    "timestamp_us": ts,
                    "ts_epoch_wrapped": bool(flags & codec.FLAG_TS_EPOCH_WRAPPED),
                    "sample_format": fmt.name.lower(),
                    "dropped": min(dropped, codec.MAX_DROPPED),
                    "values": values,
                },
            }
        )
    base = bytearray(frames["ecg_s16_extremes"])

    def mutated(index: int, value: int) -> str:
        frame = bytearray(base)
        frame[index] = value
        return frame.hex()

    invalid = [
        {"name": "nothing", "hex": "", "reason": "too_short"},
        {"name": "header_cut", "hex": base[:11].hex(), "reason": "too_short"},
        {"name": "payload_cut", "hex": base[:-1].hex(), "reason": "truncated"},
        {"name": "unknown_version", "hex": mutated(0, 2), "reason": "unknown_version"},
        {"name": "unknown_stream", "hex": mutated(1, 0x7F), "reason": "unknown_stream"},
        {"name": "unknown_format", "hex": mutated(10, 0x09), "reason": "unknown_format"},
        {"name": "zero_values", "hex": mutated(9, 0), "reason": "zero_samples"},
    ]
    return {
        "description": (
            "Golden vectors for the raw research frame v1 (contracts/raw_frame_v1.json). "
            "`valid` frames decode to `expected` (values are the wire integers, channel-minor); "
            "`invalid` frames must be refused and counted as a loss, never interpreted. "
            "GENERATED by scripts/make_consumer_vectors.py with synapse_device.codec.encode_frame."
        ),
        "valid": valid,
        "invalid": invalid,
    }


def _stream_metadata_vector() -> dict[str, Any]:
    """The Stream Metadata blob of the contract's stream table, from the SDK encoder."""
    rows = [
        consumer.StreamMeta(
            stream_id=s["stream_id"],
            sample_format=s["format"],
            channels=s["channels"],
            unit=s["unit"],
            rate_hz=s["rate_hz"],
            unit_scale=s["unit_scale"],
        )
        for s in _CONTRACT["stream_metadata"]["streams"]
    ]
    return {
        "rows": [
            {
                "stream_id": r.stream_id,
                "format": r.sample_format,
                "channels": r.channels,
                "unit": r.unit,
                "rate_hz": r.rate_hz,
                "unit_scale": r.unit_scale,
            }
            for r in rows
        ],
        "hex": consumer.encode_stream_metadata(rows).hex(),
    }


def _control_vectors() -> dict[str, Any]:
    """Control point writes the band accepts, and the writes it refuses with their error code."""
    accepted = [
        ("start", "start", None),
        ("stop", "stop", None),
        ("set_feature_rate_1_hz", "set_feature_rate", 1),
        ("set_feature_rate_max", "set_feature_rate", consumer.FEATURE_RATE_MAX_HZ),
        ("set_time_2025_01_01", "set_time", 1_735_689_600_000),
    ]
    refused = [
        ("empty", "", "bad_length"),
        ("unknown_opcode", "09", "bad_opcode"),
        ("start_with_argument", "0100", "bad_length"),
        ("set_feature_rate_zero", "0300", "bad_argument"),
        (
            "set_feature_rate_too_fast",
            bytes((3, consumer.FEATURE_RATE_MAX_HZ + 1)).hex(),
            "bad_argument",
        ),
        ("set_time_short", "0400", "bad_length"),
        ("set_time_before_2020", "04" + (1_000).to_bytes(8, "little").hex(), "bad_argument"),
    ]
    out_refused = []
    for name, hexstr, expected in refused:
        got: int | None = None
        try:
            consumer.parse_control(bytes.fromhex(hexstr))
        except consumer.ControlError as exc:
            got = exc.code
        if got != consumer.CONTROL_ERRORS[expected]:  # a vector the SDK does not refuse is wrong
            raise AssertionError(f"control vector {name}: expected {expected}, got code {got}")
        out_refused.append(
            {
                "name": name,
                "hex": hexstr,
                "error": expected,
                "code": consumer.CONTROL_ERRORS[expected],
            }
        )
    return {
        "accepted": [
            {"name": n, "op": op, "arg": arg, "hex": consumer.encode_control(op, arg).hex()}
            for n, op, arg in accepted
        ],
        "refused": out_refused,
    }


def _heart_rate_vectors() -> list[dict[str, Any]]:
    """Return the standard Heart Rate Measurement (0x2A37) values.

    The band sends NO RR in it (the RR is under the rr_recording consent and travels in the
    feature frame); the RR case is the format a client must still parse from other straps.
    """
    cases = [
        ("62_bpm_no_rr", 62, None),
        ("62_bpm_with_rr", 62, 991),
        ("300_bpm_16_bit", 300, None),
    ]
    return [
        {"name": n, "bpm": bpm, "rr_raw": rr, "hex": consumer.encode_hr_measurement(bpm, rr).hex()}
        for n, bpm, rr in cases
    ]


def _feature_sequence_vector() -> list[dict[str, Any]]:
    """Three consecutive feature frames across the 16-bit sequence wrap: 65534, 65535, 0."""
    out = []
    for i, seq in enumerate((65534, 65535, 0)):
        values: dict[str, int | None] = {
            "flags": 4,
            "sequence": seq,
            "t_dev_ms": 1_000 * i,
            "sensor_health": 7,
            "heart_rate_bpm": 60 + i,
        }
        out.append(
            {"sequence": seq, "values": values, "hex": consumer.encode_feature(values).hex()}
        )
    return out


def _consumer_vectors() -> dict[str, Any]:
    return {
        "description": (
            f"Golden vectors for the consumer contract v{CONSUMER_CONTRACT_VERSION}: the feature "
            "frame, the status record, the stream metadata, the control point, the standard "
            "Heart Rate Measurement and a feature sequence across the 16-bit wrap. `values` use "
            "null (or a missing key) for 'not available'. GENERATED by "
            "scripts/make_consumer_vectors.py with synapse_device.consumer; the firmware encoder "
            "must produce the same bytes."
        ),
        "contract_version": CONSUMER_CONTRACT_VERSION,
        "stream_metadata": _stream_metadata_vector(),
        "control": _control_vectors(),
        "heart_rate_measurement": _heart_rate_vectors(),
        "feature_sequence_wrap": _feature_sequence_vector(),
        "feature": [
            {"name": n, "values": v, "hex": consumer.encode_feature(v).hex()}
            for n, v in FEATURE_CASES
        ],
        "status": [
            {"name": n, "values": v, "hex": consumer.encode_status(v).hex()}
            for n, v in STATUS_CASES
        ],
    }


def build() -> dict[Path, str]:
    """Every generated file and its exact content."""
    return {
        OUT_CONSUMER: json.dumps(_consumer_vectors(), indent=2) + "\n",
        OUT_RAW: json.dumps(_raw_vectors(), indent=2) + "\n",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    files = build()
    if args.check:
        stale = [
            p
            for p, content in files.items()
            if (p.read_text(encoding="utf-8") if p.is_file() else "") != content
        ]
        for path in stale:
            print(f"VECTOR DRIFT: {path} is stale: run scripts/make_consumer_vectors.py")
        if stale:
            return 1
        print("VECTORS OK")
        return 0
    for path, content in files.items():
        path.write_text(content, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
