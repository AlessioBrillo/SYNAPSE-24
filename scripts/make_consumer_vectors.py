#!/usr/bin/env python3
"""Regenerate the golden vectors of the consumer contract 2.1 and of the raw frame.

* ``contracts/vectors/consumer_v2.json``: feature and status records, from
  ``synapse_device.consumer``;
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


def _consumer_vectors() -> dict[str, Any]:
    return {
        "description": (
            "Golden vectors for the consumer contract v2.1 feature frame and status record. "
            "`values` use null (or a missing key) for 'not available'. GENERATED by "
            "scripts/make_consumer_vectors.py with synapse_device.consumer; the firmware encoder "
            "must produce the same bytes."
        ),
        "contract_version": "2.1",
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
