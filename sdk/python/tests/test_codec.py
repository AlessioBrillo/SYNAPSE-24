# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Codec round-trip and MTU budget tests."""

from __future__ import annotations

import numpy as np
import pytest

from synapse_device.codec import (
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
    # S32, 4 channels (POWER): 16 bytes/sample -> 244 // 16 = 15 samples
    assert max_samples(RawFormat.S32, 4) == 244 // 16
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
