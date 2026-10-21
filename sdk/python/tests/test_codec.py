# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Codec round-trip and MTU budget tests."""

from __future__ import annotations

import numpy as np
import pytest

from synapse_device.codec import (
    HEADER_SIZE,
    MAX_PAYLOAD,
    RawCodecError,
    RawFormat,
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
        samples = np.arange(channels * 7, dtype=np.float64).reshape(7, channels)
        frame = encode_frame(stream, seq, ts, 0, RawFormat.S16, samples)
        header, out = decode_frame(frame)
        assert header.stream == stream
        assert header.timestamp_us == ts & 0xFFFFFFFF
        assert header.sequence == seq
        assert header.sample_count == 7
        assert out.shape == (7, channels)
        np.testing.assert_array_equal(out, samples)


def test_mtu_budget():
    # S32, 4 channels (POWER): 16 bytes/value. The budget is the
    # notification payload (244) minus the 12-byte header: 232 // 16 = 14.
    assert max_samples(RawFormat.S32, 4) == (MAX_PAYLOAD - HEADER_SIZE) // 16
    # Too many samples -> error
    samples = np.zeros((100, 4), dtype=np.float64)
    with pytest.raises(RawCodecError):
        encode_frame(RawStream.POWER, 0, 0, 0, RawFormat.S32, samples)


def test_wrapped_timestamp():
    ts = (1 << 32) + 1234  # above 32-bit, flag must be set
    samples = np.zeros((1, 1), dtype=np.float64)
    frame = encode_frame(RawStream.ECG, 0, ts, 0, RawFormat.S16, samples)
    header, _ = decode_frame(frame)
    assert header.flags & 0x01
    assert header.timestamp_us == ts & 0xFFFFFFFF


def test_truncated_frame_rejected():
    good = encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S16, np.zeros((1, 1)))
    with pytest.raises(RawCodecError):
        decode_frame(good[:10])  # intentional short slice


def test_loss_counter_travels_in_the_header():
    """The header carries the samples lost since the previous frame."""
    samples = np.zeros((1, 1), dtype=np.float64)
    frame = encode_frame(RawStream.ECG, 3, 0, 0, RawFormat.S16, samples, dropped=7)
    header, _ = decode_frame(frame)
    assert header.dropped == 7


def test_sdk_and_host_codecs_agree():
    """The SDK and the host must produce the same bytes for the same frame.

    Two encoders of the same wire format are two chances to disagree; the
    parity test in ``tests/test_raw_frame_parity.py`` pins the C encoder to
    the host one, and this test pins the SDK to the host.
    """
    from synapse24.hardware.raw_codec import encode_frame as host_encode

    samples = np.array([[1, 2], [3, 4]], dtype=np.int64)
    sdk_frame = encode_frame(RawStream.PPG, 9, 1234, 0, RawFormat.S16, samples, dropped=2)
    host_frame = host_encode(RawStream.PPG, 9, 1234, 0, RawFormat.S16, samples, dropped=2)
    assert sdk_frame == host_frame


def test_sdk_and_host_codecs_agree_with_flags():
    """The flags of the caller must reach the wire on both sides."""
    from synapse24.hardware.raw_codec import FLAG_TS_EPOCH_WRAPPED
    from synapse24.hardware.raw_codec import encode_frame as host_encode

    samples = np.array([[1.0]], dtype=np.int64)
    sdk_frame = encode_frame(RawStream.ECG, 1, 10, FLAG_TS_EPOCH_WRAPPED, RawFormat.S16, samples)
    host_frame = host_encode(RawStream.ECG, 1, 10, FLAG_TS_EPOCH_WRAPPED, RawFormat.S16, samples)
    assert sdk_frame == host_frame
    assert sdk_frame[8] & FLAG_TS_EPOCH_WRAPPED


def test_the_sdk_respects_the_frame_budget():
    """A frame must fit the notification payload, header included.

    The host codec counts *values* per frame, the SDK counts *samples* per
    channel: the two budgets must describe the same frame.
    """
    from synapse24.hardware.raw_codec import HEADER_SIZE as HOST_HEADER
    from synapse24.hardware.raw_codec import MAX_PAYLOAD as HOST_MAX
    from synapse24.hardware.raw_codec import max_samples as host_max_samples

    assert HEADER_SIZE == HOST_HEADER
    assert MAX_PAYLOAD == HOST_MAX
    for fmt in RawFormat:
        for channels in (1, 2, 3, 4):
            # The SDK rounds the samples down, so it can be one value below
            # the host's budget: never above it.
            assert max_samples(fmt, channels) * channels <= host_max_samples(fmt)


def test_an_oversized_frame_is_rejected():
    """A frame that does not fit the MTU must be refused, not truncated."""
    too_many = max_samples(RawFormat.S8, 1) + 1
    with pytest.raises(RawCodecError):
        encode_frame(RawStream.ECG, 0, 0, 0, RawFormat.S8, np.zeros((too_many, 1), dtype=np.int64))
