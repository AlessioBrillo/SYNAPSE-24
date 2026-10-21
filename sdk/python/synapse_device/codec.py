# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Raw sample frame codec (host side).

Python implementation of the Synapse Band wire format. The frame carries raw
samples only — no derived features — so the recorded dataset is complete and
the firmware stays small and energy-cheap.

This is the SDK-side mirror of ``firmware/common/ble/synapse_raw_frame.c``:
the byte layout below is the one the pod actually transmits.

Frame layout (little-endian)::

    offset  size  field
    0       1     version (==1)
    1       1     stream id  (0x01 ECG … 0x10 SYNC)
    2       2     sequence number (wraps at 65535)
    4       4     timestamp_us (low 32 bits, pod clock)
    8       1     flags    (bit0: timestamp wrapped)
    9       1     sample_count (values, channel-minor)
    10      1     format   (0x01 S8 … 0x03 S32)
    11      1     dropped  (samples lost since the previous frame, 0-255)
    12   N..     payload  (sample_count × format_size bytes)

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

#: Saturation limit of the header loss counter (byte 11).
MAX_DROPPED: Final[int] = 255

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
    dropped: int = 0


def format_size(fmt: RawFormat) -> int:
    """Bytes per sample for ``fmt``."""
    try:
        return _FORMAT_SIZE[fmt]
    except KeyError as exc:
        raise RawCodecError(f"unknown raw format {fmt}") from exc


def max_samples(fmt: RawFormat, channels: int) -> int:
    """Max samples that fit in one frame, given format and channel count.

    The budget is the *notification* payload (244 bytes) minus the 12-byte
    header: a frame that counts only the payload produces a notification the
    pod (and the host) rejects.
    """
    per = format_size(fmt) * channels
    return (MAX_PAYLOAD - HEADER_SIZE) // per


def encode_frame(
    stream: RawStream,
    sequence: int,
    timestamp_us: int,
    flags: int,
    fmt: RawFormat,
    samples: np.ndarray,
    dropped: int = 0,
) -> bytes:
    """Encode one frame. ``samples`` is shape (sample_count, channels).

    The argument order is the one of the host codec
    (:func:`synapse24.hardware.raw_codec.encode_frame`): the two encoders
    produce the same bytes for the same frame, and a caller that mixes them
    gets a frame nobody can decode.

    ``dropped`` is the number of samples lost for this stream since the
    previous transmitted frame (saturated at 255, 0 = nothing lost).
    """
    samples = np.asarray(samples)
    if samples.ndim != 2:
        raise RawCodecError("samples must be 2-D (sample_count, channels)")
    count, channels = samples.shape
    if channels != STREAM_CHANNELS.get(stream, 1):
        raise RawCodecError(
            f"stream {stream.name} expects {STREAM_CHANNELS[stream]} channels, got {channels}"
        )
    fs = format_size(fmt)
    if count * channels * fs > MAX_PAYLOAD - HEADER_SIZE:
        raise RawCodecError("frame payload exceeds BLE MTU budget")
    # The wire header carries the number of VALUES (channel-minor) in a single
    # byte, exactly like the firmware codec.
    values = count * channels
    if values > 255:
        raise RawCodecError("frame carries more than 255 values: too big for the header")

    # The caller's flags are kept: the wrap bit is added when the timestamp
    # does not fit in 32 bits, the others are the caller's business.
    flags = int(flags) & 0xFF
    if timestamp_us >= (1 << 32):
        flags |= FLAG_TS_EPOCH_WRAPPED
        timestamp_us &= 0xFFFFFFFF

    header = bytearray(HEADER_SIZE)
    header[0] = FRAME_VERSION
    header[1] = int(stream)
    header[2:4] = (sequence & 0xFFFF).to_bytes(2, "little")
    header[4:8] = (timestamp_us & 0xFFFFFFFF).to_bytes(4, "little")
    header[8] = flags
    header[9] = values & 0xFF
    header[10] = int(fmt)
    header[11] = min(int(dropped), MAX_DROPPED) & 0xFF

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
    sequence = int.from_bytes(frame[2:4], "little")
    ts_low = int.from_bytes(frame[4:8], "little")
    flags = frame[8]
    values = frame[9]
    fmt = RawFormat(frame[10])
    dropped = frame[11]

    channels = STREAM_CHANNELS[stream]
    fs = format_size(fmt)
    if values % channels:
        raise RawCodecError(
            f"frame declares {values} values, not a multiple of {channels} channels"
        )
    count = values // channels
    expected = HEADER_SIZE + values * fs
    if len(frame) < expected:
        raise RawCodecError(f"frame truncated: have {len(frame)} bytes, need {expected}")

    header = RawHeader(
        version=version,
        stream=stream,
        fmt=fmt,
        flags=flags,
        timestamp_us=ts_low,
        sequence=sequence,
        sample_count=count,
        dropped=dropped,
    )

    dt = _FORMAT_DTYPE[fmt]
    samples = np.frombuffer(frame[HEADER_SIZE:expected], dtype=dt).reshape(count, channels)
    return header, samples.astype(np.float64)
