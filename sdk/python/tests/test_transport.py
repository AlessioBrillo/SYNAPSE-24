# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Synthetic transport: connects, subscribes, emits real-format frames."""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from synapse_device.codec import (
    STREAM_CHANNELS,
    RawFormat,
    RawStream,
    decode_frame,
    encode_frame,
)
from synapse_device.transport import (
    CHAR_ECG,
    CHAR_PPG,
    MTU_TARGET,
    SyntheticTransport,
    TransportError,
)


async def _emit_and_collect() -> list:
    t = SyntheticTransport()
    captured = []

    def handler(uuid, data):
        header, values = decode_frame(data)
        captured.append((header, values))

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
    for header, values in captured:
        assert header.sample_format == RawFormat.S16
        assert values.shape[0] == header.sample_count
        assert header.sample_count % STREAM_CHANNELS[header.stream] == 0
        assert np.all(np.isfinite(values))


def test_roundtrip_via_codec():
    values = np.array([100, 200, 300, 400], dtype=np.int64)  # 2 PPG samples
    frame = encode_frame(RawStream.PPG, 7, 5, 0, RawFormat.S16, values)
    header, out = decode_frame(frame)
    assert header.sample_count == 4  # values, channel-minor
    assert out.reshape(-1, 2).shape[0] == 2  # samples
    assert header.sequence == 7
    assert header.timestamp_us == 5
    np.testing.assert_array_equal(out, values)


def test_request_mtu_grants_the_simulated_link():
    async def _go():
        t = SyntheticTransport()
        await t.connect("SYN-0000")
        assert t.mtu is None, "MTU unknown until it is negotiated"
        granted = await t.request_mtu(MTU_TARGET)
        assert granted == MTU_TARGET
        assert t.mtu == MTU_TARGET
        await t.disconnect()

    asyncio.run(_go())


def test_request_mtu_before_connect_raises():
    async def _go():
        t = SyntheticTransport()
        with pytest.raises(TransportError):
            await t.request_mtu(MTU_TARGET)

    asyncio.run(_go())


def test_transport_without_negotiation_reports_unknown():
    """A backend that cannot negotiate keeps the MTU UNKNOWN (None)."""
    t = SyntheticTransport()
    t.granted_mtu = None
    asyncio.run(t.connect("SYN-0000"))
    assert asyncio.run(t.request_mtu(MTU_TARGET)) is None
    assert t.mtu is None
