# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Raw sample frame codec (host side).

Python implementation of the Synapse Band wire format. The frame carries raw
samples only — no derived features — so the recorded dataset is complete and
the firmware stays small and energy-cheap.

Frame layout (little-endian):

    offset  size  field
    0       1     version (==1)
    1       1     stream id  (0x01 ECG … 0x10 SYNC)
    2       1     format   (0x01 S8 … 0x03 S32)
    3       1     flags    (bit0: timestamp wrapped)
    4       4     timestamp_us (low 32 bits, pod clock)
    8       2     sequence number
    10      2     sample_count
    12   N..     payload  (sample_count × channels × format_size bytes)

N ≤ 244 so a notification fits a 247-byte BLE MTU.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Final

import numpy as np

FRAME_VERSION: Final[int] = 1
MAX_PAYLOAD: Final[int] = 244
HEADER_SIZE: Final[int] = 12


class RawCodecError(ValueError):
    """Raised when a frame is malformed or cannot be decoded."""


class RawStream(IntEnum):
    """Logical sample streams (one stream id per LSL stream)."""

    ECG = 0x01
    PPG = 0x02
    ACC = 0x03
    GYRO = 0x04
    MAG = 0x05
    TEMP = 0x06
    POWER = 0x07
    SYNC = 0x10


class RawFormat(IntEnum):
    """Sample encoding width."""

    S8 = 0x01
    S16 = 0x02
    S32 = 0x03


_FORMAT_SIZE: Final[dict[RawFormat, int]] = {RawFormat.S8: 1, RawFormat.S16: 2, RawFormat.S32: 4}
_FORMAT_DTYPE: Final[dict[RawFormat, str]] = {
    RawFormat.S8: "<i1",
    RawFormat.S16: "<i2",
    RawFormat.S32: "<i4",
}

FLAG_TS_EPOCH_WRAPPED = 0x01

# Channel count per stream, used to reshape the decoded sample block.
STREAM_CHANNELS: Final[dict[RawStream, int]] = {
    RawStream.ECG: 1,
    RawStream.PPG: 2,  # red + IR
    RawStream.ACC: 3,
    RawStream.GYRO: 3,
    RawStream.MAG: 3,
    RawStream.TEMP: 1,
    RawStream.POWER: 4,  # voltage_mv, current_ma, power_mw, battery_pct
    RawStream.SYNC: 1,
}


@dataclass(frozen=True)
class RawHeader:
    version: int
    stream: RawStream
    fmt: RawFormat
    flags: int
    timestamp_us: int
    sequence: int
    sample_count: int


def format_size(fmt: RawFormat) -> int:
    """Bytes per sample for ``fmt``."""
    try:
        return _FORMAT_SIZE[fmt]
    except KeyError as exc:
        raise RawCodecError(f"unknown raw format {fmt}") from exc


def max_samples(fmt: RawFormat, channels: int) -> int:
    """Max samples that fit in one payload, given format and channel count."""
    per = format_size(fmt) * channels
    return MAX_PAYLOAD // per


def encode_frame(
    stream: RawStream,
    fmt: RawFormat,
    timestamp_us: int,
    sequence: int,
    samples: np.ndarray,
) -> bytes:
    """Encode one frame. ``samples`` is shape (sample_count, channels)."""
    samples = np.asarray(samples)
    if samples.ndim != 2:
        raise RawCodecError("samples must be 2-D (sample_count, channels)")
    count, channels = samples.shape
    if channels != STREAM_CHANNELS.get(stream, 1):
        raise RawCodecError(
            f"stream {stream.name} expects {STREAM_CHANNELS[stream]} channels, got {channels}"
        )
    fs = format_size(fmt)
    if count * channels * fs > MAX_PAYLOAD:
        raise RawCodecError("frame payload exceeds BLE MTU budget")

    flags = 0
    if timestamp_us >= (1 << 32):
        flags |= FLAG_TS_EPOCH_WRAPPED
        timestamp_us &= 0xFFFFFFFF

    header = bytearray(HEADER_SIZE)
    header[0] = FRAME_VERSION
    header[1] = int(stream)
    header[2] = int(fmt)
    header[3] = flags
    header[4:8] = (timestamp_us & 0xFFFFFFFF).to_bytes(4, "little")
    header[8:10] = (sequence & 0xFFFF).to_bytes(2, "little")
    header[10:12] = (count & 0xFFFF).to_bytes(2, "little")

    dt = _FORMAT_DTYPE[fmt]
    payload = samples.astype(dt).tobytes()
    return bytes(header) + payload


def decode_frame(frame: bytes) -> tuple[RawHeader, np.ndarray]:
    """Decode one frame into ``(header, samples)``.

    ``samples`` has shape ``(sample_count, channels)`` and dtype matching the
    frame format (upcast to float for convenience).
    """
    if len(frame) < HEADER_SIZE:
        raise RawCodecError(f"frame too short: {len(frame)} < {HEADER_SIZE}")
    version = frame[0]
    if version != FRAME_VERSION:
        raise RawCodecError(f"unsupported frame version {version}")

    stream = RawStream(frame[1])
    fmt = RawFormat(frame[2])
    flags = frame[3]
    ts_low = int.from_bytes(frame[4:8], "little")
    sequence = int.from_bytes(frame[8:10], "little")
    count = int.from_bytes(frame[10:12], "little")

    header = RawHeader(
        version=version,
        stream=stream,
        fmt=fmt,
        flags=flags,
        timestamp_us=ts_low,
        sequence=sequence,
        sample_count=count,
    )

    channels = STREAM_CHANNELS[stream]
    fs = format_size(fmt)
    expected = HEADER_SIZE + count * channels * fs
    if len(frame) < expected:
        raise RawCodecError(f"frame truncated: have {len(frame)} bytes, need {expected}")

    dt = _FORMAT_DTYPE[fmt]
    samples = np.frombuffer(frame[HEADER_SIZE:expected], dtype=dt).reshape(count, channels)
    return header, samples.astype(np.float64)
