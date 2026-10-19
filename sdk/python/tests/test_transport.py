# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Synthetic transport: connects, subscribes, emits real-format frames."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from synapse_device.codec import RawFormat, RawStream, decode_frame, encode_frame
from synapse_device.transport import CHAR_ECG, CHAR_PPG, SyntheticTransport, TransportError


async def _emit_and_collect() -> list:
    t = SyntheticTransport()
    captured = []

    def handler(uuid, data):
        header, samples = decode_frame(data)
        captured.append((header, samples))

    await t.connect("SYN-0000")
    await t.subscribe(CHAR_ECG, handler)
    await t.subscribe(CHAR_PPG, handler)
    await t.start([CHAR_ECG, CHAR_PPG], rate_hz=50, duration_s=0.2)
    await asyncio.sleep(0.4)
    await t.stop()
    await t.disconnect()
    return captured


def test_connect_disconnect():
    async def _go():
        t = SyntheticTransport()
        assert not t.connected
        await t.connect("SYN-0000")
        assert t.connected
        await t.disconnect()
        assert not t.connected

    asyncio.run(_go())


def test_subscribe_before_connect_raises():
    async def _go():
        t = SyntheticTransport()

        async def handler(uuid, data):  # noqa: ARG001
            pass

        with pytest.raises(TransportError):
            await t.subscribe(CHAR_ECG, handler)

    asyncio.run(_go())


def test_emits_real_frames():
    captured = asyncio.run(_emit_and_collect())
    assert len(captured) > 0, "synthetic pod emitted no frames"
    streams = {h.stream for h, _ in captured}
    assert RawStream.ECG in streams and RawStream.PPG in streams
    for header, samples in captured:
        assert header.fmt == RawFormat.S16
        assert samples.shape[1] == {RawStream.ECG: 1, RawStream.PPG: 2}[header.stream]
        assert np.all(np.isfinite(samples))


def test_roundtrip_via_codec():
    samples = np.array([[100, 200], [300, 400]], dtype=np.float64)
    frame = encode_frame(RawStream.PPG, RawFormat.S16, 5, 7, samples)
    header, out = decode_frame(frame)
    assert header.sample_count == 2
    np.testing.assert_array_equal(out, samples)
