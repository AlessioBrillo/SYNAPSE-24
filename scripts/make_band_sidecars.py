#!/usr/bin/env python3
"""The files a phone writes while recording a band, as fixtures for Averyn's server and screens.

Averyn (ADR-0030) stores what the band sends **as received**: one append-only file per kind, each
record being

    u16  length   (little endian)
    i64  t_rx_ns  (little endian; the phone's monotonic clock since the recording started)
    u8[length]    the frame bytes exactly as notified

* ``<id>.synapse-raw.bin``: raw research frames (``contracts/raw_frame_v1.json``);
* ``<id>.synapse.bin``: consumer v2 records (``contracts/gatt_v2.json``): feature frames (27 B)
  and status records (40 B), told apart by their length.

This writes one synthetic 60 s session of each, with the faults a radio link produces, and the
answer key a correct reader must reproduce (``session_60s.expected.json``). The bytes come from the
SDK encoders (``synapse_device.codec`` / ``consumer``), never from a hand-written struct, and the
answer key from the reference rules in ``make_band_fixtures.oracle`` (the contract's sequence rule).
Nothing here is a measurement.

    uv run python scripts/make_band_sidecars.py          # write
    uv run python scripts/make_band_sidecars.py --check  # fail on drift
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

import make_band_fixtures as fx  # noqa: E402
from synapse_device import codec, consumer  # noqa: E402

OUT_DIR = ROOT / "contracts" / "fixtures"
RAW_BIN = OUT_DIR / "session_60s.synapse-raw.bin"
L1_BIN = OUT_DIR / "session_60s.synapse.bin"
EXPECTED = OUT_DIR / "session_60s.expected.json"

#: u16 length + i64 reception time, little endian.
RECORD_HEADER = struct.Struct("<Hq")
SECONDS = 60.0


def pack(records: list[tuple[int, bytes]]) -> bytes:
    """``(recv_ms, frame)`` -> the sidecar bytes."""
    return b"".join(RECORD_HEADER.pack(len(raw), ms * 1_000_000) + raw for ms, raw in records)


def unpack(blob: bytes) -> list[tuple[int, bytes]]:
    """The sidecar bytes -> ``(recv_ms, frame)``; the reader every consumer implements."""
    out: list[tuple[int, bytes]] = []
    pos = 0
    while pos < len(blob):
        length, t_ns = RECORD_HEADER.unpack_from(blob, pos)
        pos += RECORD_HEADER.size
        out.append((t_ns // 1_000_000, blob[pos : pos + length]))
        pos += length
    return out


# --------------------------------------------------------------------------- the raw session


def raw_session() -> list[tuple[int, bytes]]:
    """60 s of ECG, PPG and the three IMU streams, with one fault of each kind."""
    streams = fx._session(SECONDS)
    ecg = streams[codec.RawStream.ECG]

    # (1) ECG: a band restart after 100 frames (the sequence rule needs a counter past its
    # out-of-order window to tell a reboot from a late frame), then the rest of the minute as a
    # fresh sequence with a device clock that starts again.
    reboot_at = 100
    restarted = fx._frames(codec.RawStream.ECG, SECONDS - ecg[reboot_at][0] / 1000.0, t0_us=300_000)
    shift = ecg[reboot_at][0] + 150
    ecg[:] = [*ecg[:reboot_at], *[(shift + ms, raw) for ms, raw in restarted]]
    # (2) ECG: four consecutive frames never arrive (after the restart, so the gap is its own).
    del ecg[reboot_at + 30 : reboot_at + 34]

    # (3) PPG: the band reports 9 samples it dropped before one frame.
    ppg = streams[codec.RawStream.PPG]
    ms, original = ppg[20]
    header, values = codec.decode_frame(original)
    ppg[20] = (
        ms,
        codec.encode_frame(
            header.stream,
            header.sequence,
            header.timestamp_us,
            header.flags,
            header.sample_format,
            values,
            dropped=9,
        ),
    )

    # (4) ACC: one frame arrives twice.
    acc = streams[codec.RawStream.ACC]
    acc.insert(31, (acc[30][0] + 1, acc[30][1]))

    frames = fx._interleave(streams)

    # (5) A frame of a version this contract does not define: counted, never interpreted.
    bad = bytearray(frames[40][1])
    bad[0] = 2
    frames.insert(41, (frames[40][0] + 1, bytes(bad)))
    return frames


# --------------------------------------------------------------------------- the L1 session


def l1_session() -> list[tuple[int, bytes]]:
    """60 feature frames at 1 Hz (two lost, one of an unknown version) and a status every 5 s."""
    records: list[tuple[int, bytes]] = []
    sequence = 0
    for second in range(int(SECONDS)):
        if second in (20, 21):
            sequence += 1  # two frames lost on the link
        values: dict[str, int | None] = {
            "flags": 4,  # transmitting
            "sequence": sequence & 0xFFFF,
            "t_dev_ms": second * 1000,
            "heart_rate_bpm": 60 + (second % 21),
            "ppg_sqi": 200 + second % 50,
            "motion_artifact": 10 + second % 20,
            "motion_level": 15,
            "battery_pct": 90,
            "sensor_health": 0x17,
        }
        if second < 10:  # warming up: nothing but the heart rate is known yet
            for key in ("ppg_sqi", "motion_artifact", "motion_level"):
                values[key] = None
        frame = consumer.encode_feature(values)
        if second == 40:
            frame = bytes([3, *frame[1:]])  # a version this contract does not define
        records.append((second * 1000 + 15, frame))
        sequence += 1
        if second % 5 == 0:
            status = {
                "tier": 0,
                "sensor_health": 0x17,
                "flags": 4,
                "mtu": 247,
                "uptime_s": second,
                "dropped_frames": 2 if second >= 30 else 0,
                "ring_overflow": 0,
            }
            records.append((second * 1000 + 40, consumer.encode_status(status)))
    return records


def l1_expected(records: list[tuple[int, bytes]]) -> dict[str, Any]:
    tracker = fx._Tracker()
    unknown = 0
    features = 0
    statuses = 0
    for _ms, raw in records:
        if len(raw) == consumer.STATUS_BYTES:
            statuses += 1
        elif raw[0] != consumer.FEATURE_VERSION:
            unknown += 1
        else:
            features += 1
            tracker.on_frame(int.from_bytes(raw[2:4], "little"))
    return {
        "feature_frames": features,
        "status_records": statuses,
        "unknown_version_frames": unknown,
        "lost_frames": tracker.lost,
        "restarts": tracker.restarts,
        "out_of_order": tracker.out_of_order,
    }


# --------------------------------------------------------------------------- build


def build() -> dict[Path, bytes]:
    raw = raw_session()
    l1 = l1_session()
    expected = {
        "description": (
            "Answer key for session_60s.synapse-raw.bin and session_60s.synapse.bin. GENERATED by "
            "scripts/make_band_sidecars.py; the sidecar record is u16 length | i64 t_rx_ns | frame "
            "(little endian), t_rx_ns being the phone's monotonic clock since the recording began."
        ),
        "synthetic": True,
        "record_header": "<Hq",
        "out_of_order_window": fx.OUT_OF_ORDER_WINDOW,
        "raw": {"records": len(raw), **fx.oracle(raw)},
        "l1": {"records": len(l1), **l1_expected(l1)},
    }
    return {
        RAW_BIN: pack(raw),
        L1_BIN: pack(l1),
        EXPECTED: (json.dumps(expected, separators=(",", ":")) + "\n").encode(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    files = build()
    if args.check:
        stale = [p for p, data in files.items() if not p.is_file() or p.read_bytes() != data]
        for path in stale:
            print(f"SIDECAR FIXTURE DRIFT: {path.relative_to(ROOT)} is stale")
        if stale:
            print("run scripts/make_band_sidecars.py")
            return 1
        print("BAND SIDECARS OK")
        return 0
    for path, data in files.items():
        path.write_bytes(data)
        print(f"wrote {path.relative_to(ROOT)} ({len(data) // 1024} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
