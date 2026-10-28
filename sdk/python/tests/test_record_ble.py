# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Pins for the BLE recording path (``record_ble``).

The fake pod plays REAL frames (built with ``encode_frame``) into the recor-
der on a wall-clock schedule, so every tested property is the recorded pro-
perty, not a mock's echo: the measured rate comes from the frames' own dev-
ice stamps, the anchor comes from the delivery wall clock, and the wrap-
straddling capture proves the extended clock keeps the timeline monotonic.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("pyxdf")

import pyxdf  # noqa: E402

from synapse_device.codec import (  # noqa: E402
    FLAG_TS_EPOCH_WRAPPED,
    RawFormat,
    RawStream,
    encode_frame,
)
from synapse_device.record import BleRecordError, record_ble  # noqa: E402
from synapse_device.transport import CHAR_ECG, MTU_TARGET  # noqa: E402


def _ecg_frame(seq: int, ts_us: int, *, flags: int = 0, dropped: int = 0) -> bytes:
    """Build one real ECG frame (1 channel, S16) through the codec."""
    return encode_frame(RawStream.ECG, seq, ts_us, flags, RawFormat.S16, [[seq * 10]], dropped)


class FakePodTransport:
    """Transport SDK surface playing a scripted frame schedule on the loop."""

    def __init__(
        self,
        schedule: list[tuple[float, str, bytes]] | None = None,
        *,
        granted_mtu: int | None = MTU_TARGET,
    ) -> None:
        self.schedule = list(schedule or [])
        self.granted_mtu = granted_mtu
        self.connected = False
        self.address = ""
        self.mtu_requests: list[int] = []
        self.unsubscribed: list[str] = []
        self._tasks: list[asyncio.Task] = []

    async def connect(self, address: str) -> None:
        self.address = address
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False
        tasks = self._tasks
        self._tasks = []
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def subscribe(self, char_uuid: str, handler: Callable[[str, bytes], None]) -> None:
        plays = [entry for entry in self.schedule if entry[1] == char_uuid]
        self._tasks.append(asyncio.create_task(self._play(char_uuid, handler, plays)))

    async def unsubscribe(self, char_uuid: str) -> None:
        self.unsubscribed.append(char_uuid)

    async def request_mtu(self, mtu: int) -> int | None:
        self.mtu_requests.append(mtu)
        return self.granted_mtu

    async def write(self, char_uuid: str, data: bytes, response: bool = True) -> None:
        raise AssertionError(f"the record path must not write to {char_uuid}")

    async def read(self, char_uuid: str) -> bytes:
        raise AssertionError(f"the record path must not read {char_uuid}")

    async def _play(
        self,
        char_uuid: str,
        handler: Callable[[str, bytes], None],
        plays: list[tuple[float, str, bytes]],
    ) -> None:
        started = time.monotonic()
        for delay, _uuid, frame in plays:
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(delay - elapsed, 0.0))
            handler(char_uuid, frame)


def _stream_by_name(streams: list[dict], name: str) -> dict:
    for stream in streams:
        if stream["info"]["name"][0] == name:
            return stream
    raise AssertionError(f"stream {name} missing from {[s['info']['name'][0] for s in streams]}")


def _load_raw(out: Path) -> tuple[list[dict], dict]:
    """Load the XDF WITHOUT pyxdf's dejitter smoothing.

    The recorder declares the rate it measured; the wall-anchored stamps may
    legitimately be uneven (delivery jitter). Pinning the *file* means
    pinning what was written, not pyxdf's smoothed view.
    """
    return pyxdf.load_xdf(str(out), dejitter_timestamps=False)


def test_measured_rate_and_wall_anchored_timestamps(tmp_path: Path) -> None:
    """Rate measured from pod stamps, wall anchor from the first delivery."""
    out = tmp_path / "session.xdf"
    schedule = [
        (0.05 + index * 0.05, CHAR_ECG, _ecg_frame(index, index * 10_000)) for index in range(3)
    ]
    before = time.time()
    summary = record_ble(
        "AA:BB:CC:DD:EE:FF", out, duration_s=0.4, transport=FakePodTransport(schedule)
    )
    after = time.time()

    assert summary["granted_mtu"] == MTU_TARGET
    assert summary["frames"] == {"ECG": 3}
    rate = summary["rate_hz"]["ECG"]  # measured, not the nominal constant
    assert abs(rate - 100.0) <= 2.0

    streams, _ = _load_raw(out)
    stamps = _stream_by_name(streams, "ECG")["time_stamps"]
    assert before <= stamps[0] <= after  # pod stamp 0 == the wall anchor
    np.testing.assert_allclose(np.diff(stamps), 0.01, atol=0.002)


def test_no_frames_raises_and_leaves_no_file(tmp_path: Path) -> None:
    """A pod that streams nothing fails the record, it does not fog it up."""
    out = tmp_path / "empty.xdf"
    with pytest.raises(BleRecordError, match="no frames arrived"):
        record_ble("AA:BB:CC:DD:EE:FF", out, duration_s=0.2, transport=FakePodTransport([]))
    assert not out.exists()


def test_mtu_target_none_skips_the_negotiation(tmp_path: Path) -> None:
    """``mtu_target=None`` asks nothing: the MTU stays unknown, not assumed."""
    fake = FakePodTransport(
        [(0.05, CHAR_ECG, _ecg_frame(0, 0))],
        granted_mtu=123,
    )
    out = tmp_path / "noguess.xdf"
    summary = record_ble("AA:BB:CC:DD:EE:FF", out, duration_s=0.3, transport=fake, mtu_target=None)
    assert fake.mtu_requests == []
    assert summary["granted_mtu"] is None
    # One frame only: no rate is measurable, so the file must declare 0
    # (irregular), not a dressed-up nominal guess.
    streams, _ = pyxdf.load_xdf(str(out))
    assert float(_stream_by_name(streams, "ECG")["info"]["nominal_srate"][0]) == 0.0
    assert summary["rate_hz"]["ECG"] == 0.0


def test_wrap_straddle_keeps_the_timeline_monotonic(tmp_path: Path) -> None:
    """Stamp roll (flagged and unflagged) must extend time, not fold it back."""
    wrap = 1 << 32
    out = tmp_path / "wrap.xdf"
    schedule = [
        (0.05, CHAR_ECG, _ecg_frame(0, wrap - 1_000_000)),
        (0.15, CHAR_ECG, _ecg_frame(1, wrap - 500_000)),
        (0.25, CHAR_ECG, _ecg_frame(2, 500_000, flags=FLAG_TS_EPOCH_WRAPPED)),
        (0.35, CHAR_ECG, _ecg_frame(3, 1_200_000)),
    ]
    summary = record_ble(
        "AA:BB:CC:DD:EE:FF", out, duration_s=0.5, transport=FakePodTransport(schedule)
    )
    assert summary["frames"] == {"ECG": 4}
    # The pod deltas are 0.5 s, 1.0 s across the roll, 0.7 s: the median
    # rate read from the pod's own clock is ~1.43 Hz.
    assert abs(summary["rate_hz"]["ECG"] - 1.42857) <= 0.05

    streams, _ = _load_raw(out)
    stamps = _stream_by_name(streams, "ECG")["time_stamps"]
    diffs = np.diff(stamps)
    assert bool((diffs > 0).all())  # no backward step, no phantom 2^32 gap
    np.testing.assert_allclose(stamps[-1] - stamps[0], 2.2, atol=0.01)
    np.testing.assert_allclose(diffs, [0.5, 1.0, 0.7], atol=0.01)


def test_dropped_counts_comes_from_the_frames(tmp_path: Path) -> None:
    """The loss counter is the wire truth (frames), not a host-side guess."""
    out = tmp_path / "loss.xdf"
    schedule = [
        (0.05, CHAR_ECG, _ecg_frame(0, 0)),
        (0.10, CHAR_ECG, _ecg_frame(1, 10_000)),
        (0.15, CHAR_ECG, _ecg_frame(2, 20_000, dropped=2)),
    ]
    summary = record_ble(
        "AA:BB:CC:DD:EE:FF", out, duration_s=0.4, transport=FakePodTransport(schedule)
    )
    assert summary["dropped"] == 2


def test_every_capture_uuid_is_unsubscribed(tmp_path: Path) -> None:
    """A recording that leaves subscriptions open leaks the link."""
    from synapse_device.record import _RECORD_UUIDS  # noqa: PLC0415 - the pin's subject

    out = tmp_path / "cleanup.xdf"
    fake = FakePodTransport([(0.05, CHAR_ECG, _ecg_frame(0, 0))])
    record_ble("AA:BB:CC:DD:EE:FF", out, duration_s=0.3, transport=fake)
    assert set(fake.unsubscribed) == set(_RECORD_UUIDS)
