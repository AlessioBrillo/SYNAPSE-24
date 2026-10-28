# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Codec round-trip and MTU budget tests.

The SDK codec is the CANONICAL one (decision Q7): the host pipeline imports
it through the ``synapse24.hardware.raw_codec`` shim, so "SDK vs host" is one
object identity — and the byte parity with the C encoder stays pinned in the
repo's own ``tests/test_raw_frame_parity.py``.
"""

from __future__ import annotations

import numpy as np
import pytest

from synapse_device.codec import (
    FLAG_TS_EPOCH_WRAPPED,
    HEADER_SIZE,
    MAX_PAYLOAD,
    RawCodecError,
    RawFormat,
    RawHeader,
    RawStream,
    decode_frame,
    encode_frame,
    format_size,
    max_samples,
)

_STREAM_CHANNELS = {
    RawStream.ECG: 1,
    RawStream.PPG: 2,
    RawStream.ACC: 3,
    RawStream.GYRO: 3,
    RawStream.MAG: 3,
    RawStream.TEMP: 1,
    RawStream.POWER: 4,
    RawStream.SYNC: 1,
}


@pytest.mark.parametrize("fmt", [RawFormat.S8, RawFormat.S16, RawFormat.S32])
def test_format_size(fmt):
    assert format_size(fmt) in (1, 2, 4)


def test_roundtrip_all_streams():
    ts = 1_234_567_890
    seq = 42
    for stream, channels in _STREAM_CHANNELS.items():
        values = np.arange(channels * 7, dtype=np.int64)
        frame = encode_frame(stream, seq, ts, 0, RawFormat.S16, values)
        header, out = decode_frame(frame)
        assert header.stream == stream
        assert header.timestamp_us == ts & 0xFFFFFFFF
        assert header.sequence == seq
        assert header.sample_count == channels * 7  # header counts VALUES
        assert out.dtype == np.int32
        assert out.shape == (channels * 7,)
        # Channel-minor: a reshape to (-1, channels) recovers the samples.
        np.testing.assert_array_equal(out.reshape(-1, channels), values.reshape(-1, channels))


def test_mtu_budget():
    # The budget counts VALUES: a POWER frame carries 4 per sample, so
    # (244 - 12) // 4 = 58 values = 14 samples.
    assert max_samples(RawFormat.S32) == (MAX_PAYLOAD - HEADER_SIZE) // 4
    # Too many values -> error, never truncation.
    with pytest.raises(RawCodecError):
        encode_frame(RawStream.POWER, 0, 0, 0, RawFormat.S32, np.zeros(400, dtype=np.int64))


def test_sample_range_is_enforced_not_wrapped():
    """A value the format cannot represent must be refused, not truncated.

    The silent cast is how real data corrupts quietly.
    """
    with pytest.raises(RawCodecError, match="out of range"):
        encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S16, [40_000])
    with pytest.raises(RawCodecError, match="out of range"):
        encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S16, [-40_000])
    # The positive boundary is fine.
    encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S16, [32_767])


def test_wrapped_timestamp():
    """The encoder masks the stamp to 32 bits and keeps the caller's flags.

    The wrap BIT is the caller's (or firmware's) business: the codec must not
    invent one, it must only never lose the caller's.
    """
    ts = (1 << 32) + 1234  # above 32-bit: masked, NOT flagged by the codec
    frame = encode_frame(RawStream.ECG, 0, ts, 0, RawFormat.S16, [0])
    header, _ = decode_frame(frame)
    assert header.timestamp_us == ts & 0xFFFFFFFF
    assert header.ts_epoch_wrapped is False, "the codec must not invent a wrap flag"

    # A caller-provided flag travels untouched.
    framed = encode_frame(RawStream.ECG, 0, ts, FLAG_TS_EPOCH_WRAPPED, RawFormat.S16, [0])
    flagged, _ = decode_frame(framed)
    assert flagged.ts_epoch_wrapped is True
    assert flagged.timestamp_us == ts & 0xFFFFFFFF


def test_truncated_frame_rejected():
    good = encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S16, [0])
    with pytest.raises(RawCodecError):
        decode_frame(good[:10])  # intentional short slice


def test_unknown_stream_and_format_rejected():
    good = encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S16, [0])
    bad_stream = bytearray(good)
    bad_stream[1] = 0x2A
    with pytest.raises(RawCodecError, match="stream"):
        decode_frame(bytes(bad_stream))
    bad_fmt = bytearray(good)
    bad_fmt[10] = 0x0A
    with pytest.raises(RawCodecError, match="format"):
        decode_frame(bytes(bad_fmt))


def test_loss_counter_travels_in_the_header():
    """The header carries the samples lost since the previous frame."""
    frame = encode_frame(RawStream.ECG, 3, 0, 0, RawFormat.S16, [0], dropped=7)
    header, _ = decode_frame(frame)
    assert header.dropped == 7
    assert header.reserved == 7  # compatibility alias


def test_wrapped_property_reports_the_flag():
    frame = encode_frame(RawStream.ECG, 0, 0, 0x01, RawFormat.S16, [0])
    header, _ = decode_frame(frame)
    assert header.ts_epoch_wrapped is True
    plain, _ = decode_frame(encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S16, [0]))
    assert plain.ts_epoch_wrapped is False


def test_the_host_shim_is_the_same_object():
    """One codec, one identity: the host module re-exports the SDK's."""
    import synapse24.hardware.raw_codec as host

    assert host.encode_frame is encode_frame
    assert host.decode_frame is decode_frame
    assert host.RawHeader is RawHeader
    assert host.RawStream is RawStream
    assert host.RawFormat is RawFormat
    assert host.STREAM_CHANNELS == _STREAM_CHANNELS
    assert host.HEADER_SIZE == HEADER_SIZE == 12
    assert host.MAX_PAYLOAD == MAX_PAYLOAD == 244


def test_no_channel_recount_regression():
    """Regression: an encoder that multiplied by channel count again.

    The frame length is header + values * format_size; the historical bug
    (multi-channel frames carrying stale bytes / reading past the buffer)
    must stay impossible.
    """
    values = [1, 2, 3, 4, 5, 6]  # 2 samples x 3 channels
    frame = encode_frame(RawStream.ACC, 0, 0, 0, RawFormat.S16, values)
    assert len(frame) == HEADER_SIZE + len(values) * 2
