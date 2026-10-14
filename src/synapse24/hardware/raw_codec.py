"""SYNAPSE-24 raw sample frame codec (host side).

Python mirror of ``firmware/common/ble/synapse_raw_frame.c``. The C encoder is
the authority; this decoder is verified against it byte-for-byte by
``tests/test_raw_frame_parity.py``, which compiles the C source on the CI host
through ``tests/firmware_host``. If the two ever diverge, that test fails.

The frame carries **raw samples only** — no derived features. Signal quality,
HRV and sensor fusion stay on the host, so the recorded dataset is complete
and the firmware stays small and cheap in energy (Architecture.md §55-62).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

import numpy as np

__all__ = [
    "FRAME_VERSION",
    "HEADER_SIZE",
    "MAX_PAYLOAD",
    "RawCodecError",
    "RawFormat",
    "RawHeader",
    "RawStream",
    "STREAM_CHANNELS",
    "decode_frame",
    "encode_frame",
    "format_size",
    "max_samples",
]

FRAME_VERSION = 1
MAX_PAYLOAD = 244  # BLE notification payload with a 247-byte negotiated MTU
HEADER_SIZE = 12


class RawCodecError(ValueError):
    """Raised when a frame is malformed or cannot be decoded."""


class RawStream(IntEnum):
    """Logical sample streams. One stream id per LSL stream (see hardware.yaml)."""

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


_FORMAT_SIZE = {RawFormat.S8: 1, RawFormat.S16: 2, RawFormat.S32: 4}
_FORMAT_DTYPE = {RawFormat.S8: "<i1", RawFormat.S16: "<i2", RawFormat.S32: "<i4"}

FLAG_TS_EPOCH_WRAPPED = 0x01

# Channel count per stream, used by the recorder to reshape the sample block.
STREAM_CHANNELS: dict[RawStream, int] = {
    RawStream.ECG: 1,
    RawStream.PPG: 2,  # red + IR
    RawStream.ACC: 3,
    RawStream.GYRO: 3,
    RawStream.MAG: 3,
    RawStream.TEMP: 1,
    RawStream.POWER: 4,  # voltage_mv, current_ma, power_mw, battery_pct
    RawStream.SYNC: 1,
}


def format_size(fmt: RawFormat | int) -> int:
    """Bytes per sample for a format. Raises for an unsupported format."""
    try:
        return _FORMAT_SIZE[RawFormat(fmt)]
    except (KeyError, ValueError) as exc:
        raise RawCodecError(f"unsupported sample format: {fmt!r}") from exc


def max_samples(fmt: RawFormat | int) -> int:
    """Number of samples of @p fmt that fit in a single frame."""
    return (MAX_PAYLOAD - HEADER_SIZE) // format_size(fmt)


@dataclass(frozen=True)
class RawHeader:
    """Decoded frame header; the sample block stays encoded."""

    version: int
    stream: RawStream
    sequence: int
    timestamp_us: int
    flags: int
    sample_count: int
    sample_format: RawFormat
    reserved: int

    @property
    def ts_epoch_wrapped(self) -> bool:
        """True when the 32-bit device microsecond counter wrapped."""
        return bool(self.flags & FLAG_TS_EPOCH_WRAPPED)


def encode_frame(
    stream: RawStream | int,
    sequence: int,
    timestamp_us: int,
    flags: int,
    fmt: RawFormat | int,
    samples: np.ndarray | list[int],
) -> bytes:
    """Encode one frame.

    Args:
        stream: Logical stream id.
        sequence: Wrapping 16-bit sequence number.
        timestamp_us: Device microseconds of the first sample (low 32 bits used).
        flags: OR of ``FLAG_*`` bits.
        fmt: Sample encoding.
        samples: Interleaved samples (channel-minor).

    Returns:
        The frame bytes.
    """
    sample_format = RawFormat(fmt)
    size = format_size(sample_format)

    arr = np.asarray(samples, dtype=np.int64).reshape(-1)
    count = int(arr.size)
    if count == 0:
        raise RawCodecError("frame must carry at least one sample")
    if count > max_samples(sample_format):
        raise RawCodecError(
            f"{count} samples exceed the {max_samples(sample_format)}-sample frame limit"
        )

    lo = np.int64(arr)
    limit = np.int64(1) << np.int64(8 * size)
    if np.any(lo >= limit // 2) or np.any(lo < -(limit // 2)):
        raise RawCodecError(f"sample out of range for {sample_format.name}")

    header = bytes(
        [
            FRAME_VERSION,
            int(RawStream(stream)),
            sequence & 0xFF,
            (sequence >> 8) & 0xFF,
            timestamp_us & 0xFF,
            (timestamp_us >> 8) & 0xFF,
            (timestamp_us >> 16) & 0xFF,
            (timestamp_us >> 24) & 0xFF,
            flags & 0xFF,
            count,
            int(sample_format),
            0,
        ]
    )
    return header + arr.astype(_FORMAT_DTYPE[sample_format], copy=False).tobytes()


def decode_frame(frame: bytes | bytearray | np.ndarray) -> tuple[RawHeader, np.ndarray]:
    """Decode one frame into its header and a 1-D sample array.

    Args:
        frame: The raw frame bytes.

    Returns:
        A ``(header, samples)`` tuple. ``samples`` is channel-minor and must be
        reshaped with :data:`STREAM_CHANNELS` by the caller.

    Raises:
        RawCodecError: If the frame is malformed.
    """
    buf = bytes(frame)
    if len(buf) < HEADER_SIZE:
        raise RawCodecError(f"frame is {len(buf)} bytes, need at least {HEADER_SIZE}")

    (
        version,
        stream_id,
        seq_lo,
        seq_hi,
        ts0,
        ts1,
        ts2,
        ts3,
        flags,
        count,
        fmt_id,
        reserved,
    ) = np.frombuffer(buf[:HEADER_SIZE], dtype=np.uint8)

    if int(version) != FRAME_VERSION:
        raise RawCodecError(f"unsupported frame version {int(version)}")

    try:
        stream = RawStream(int(stream_id))
    except ValueError as exc:
        raise RawCodecError(f"unknown stream id {int(stream_id)}") from exc

    try:
        sample_format = RawFormat(int(fmt_id))
    except ValueError as exc:
        raise RawCodecError(f"unsupported sample format {int(fmt_id)}") from exc

    size = format_size(sample_format)
    if count == 0:
        raise RawCodecError("frame declares zero samples")

    needed = HEADER_SIZE + int(count) * size
    if len(buf) < needed:
        raise RawCodecError(f"frame is {len(buf)} bytes, declares {needed}")

    samples = np.frombuffer(buf[HEADER_SIZE:needed], dtype=_FORMAT_DTYPE[sample_format]).astype(
        np.int32
    )

    header = RawHeader(
        version=int(version),
        stream=stream,
        sequence=int(seq_lo) | (int(seq_hi) << 8),
        timestamp_us=int(ts0) | (int(ts1) << 8) | (int(ts2) << 16) | (int(ts3) << 24),
        flags=int(flags),
        sample_count=int(count),
        sample_format=sample_format,
        reserved=int(reserved),
    )
    return header, samples
