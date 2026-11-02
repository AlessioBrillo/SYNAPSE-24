"""Live feed for the local viewer: raw frames -> decimated JSON messages.

A *source* produces raw frames in the research-service wire format (the real BLE
transport, or a synthetic generator that goes through the same encode/decode path).
The hub decodes them, detects losses *with their reason*, reduces each frame to a
min/max envelope (what a plot can actually show) and fans the messages out to the
WebSocket clients.

Nothing here invents data: with ``SYNAPSE_LIVE_SOURCE`` unset there is no source and
the socket only reports the catalog status (``source: "off"``). ``synthetic`` is a demo
and says so in the ``hello`` message; ``ble:<address>`` talks to a band.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from synapse_device.codec import RawFormat, RawStream, decode_frame, encode_frame

logger = logging.getLogger("synapse24.live")

SOURCE_ENV = "SYNAPSE_LIVE_SOURCE"

_GATT_V2 = Path(__file__).resolve().parents[1] / "contracts" / "gatt_v2.json"

#: Envelope buckets per frame: a frame is at most ~120 values, the plot needs far fewer.
ENVELOPE_BUCKETS = 32

Emit = Callable[[dict[str, Any]], None]


def stream_metadata() -> dict[int, dict[str, Any]]:
    """Stream id -> {name, channels, unit, rate_hz, unit_scale, format}, from ``gatt_v2.json``.

    The viewer never hardcodes a rate or a unit: it shows what the contract says.
    """
    doc = json.loads(_GATT_V2.read_text(encoding="utf-8"))
    return {int(s["stream_id"]): dict(s) for s in doc["stream_metadata"]["streams"]}


def minmax_envelope(
    values: np.ndarray, buckets: int
) -> tuple[list[list[float]], list[list[float]]]:
    """Reduce ``(n, channels)`` samples to per-bucket minima and maxima.

    A plot of ``n`` points at a few hundred pixels shows the envelope anyway; sending the
    min and the max of each bucket keeps the peaks (an R wave must not be decimated away).
    """
    n = values.shape[0]
    if n == 0:
        return [], []
    count = max(1, min(buckets, n))
    edges = np.linspace(0, n, count + 1, dtype=int)
    lows: list[list[float]] = []
    highs: list[list[float]] = []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        chunk = values[lo:hi]
        lows.append([float(x) for x in chunk.min(axis=0)])
        highs.append([float(x) for x in chunk.max(axis=0)])
    return lows, highs


class LossTracker:
    """Per-stream loss detection, with the reason."""

    def __init__(self) -> None:
        self._expected: dict[int, int] = {}

    def observe(self, stream: int, sequence: int, dropped: int) -> list[dict[str, Any]]:
        """Losses revealed by a frame; empty when the stream is intact."""
        events: list[dict[str, Any]] = []
        expected = self._expected.get(stream)
        if expected is not None:
            gap = (sequence - expected) & 0xFFFF
            # A gap above half the sequence space is a repeat or a reorder, not a loss.
            if 0 < gap < 0x8000:
                events.append({"reason": "sequence_gap", "lost_frames": gap})
        if dropped > 0:
            # The device says it discarded samples for this stream (ring full, refused notify).
            events.append({"reason": "device_dropped", "lost_samples": int(dropped)})
        self._expected[stream] = (sequence + 1) & 0xFFFF
        return events


class FrameDecoder:
    """Raw frame bytes -> viewer messages (``frame`` and, when needed, ``loss``)."""

    def __init__(self) -> None:
        self.meta = stream_metadata()
        self.losses = LossTracker()
        self.frames = 0

    def reset(self) -> None:
        """Forget the expected sequences: a restarted source begins again at 0."""
        self.losses = LossTracker()

    def decode(self, data: bytes) -> list[dict[str, Any]]:
        header, values = decode_frame(data)
        stream = int(header.stream)
        meta = self.meta.get(stream)
        if meta is None:
            return []  # a stream the contract does not describe (TEMP, SYNC): nothing to plot
        self.frames += 1
        channels = int(meta["channels"])
        if values.size % channels:
            # A parseable header over a payload that is not whole samples: report it, do not
            # plot a plausible but shorter signal.
            raise ValueError(
                f"{values.size} values do not fit {channels} channel(s) of {meta['name']}"
            )
        samples = values.reshape(-1, channels).astype(np.float64)
        lows, highs = minmax_envelope(samples, ENVELOPE_BUCKETS)
        out: list[dict[str, Any]] = [
            {
                "type": "loss",
                "stream": meta["name"],
                "sequence": header.sequence,
                **event,
            }
            for event in self.losses.observe(stream, header.sequence, header.dropped)
        ]
        out.append(
            {
                "type": "frame",
                "stream": meta["name"],
                "sequence": header.sequence,
                "t_us": header.timestamp_us,
                "n": int(samples.shape[0]),
                "min": lows,
                "max": highs,
            }
        )
        return out


class LiveSource(Protocol):
    """Something that produces raw frames until cancelled."""

    name: str
    demo: bool

    async def run(self, on_frame: Callable[[bytes], None]) -> None: ...


class SyntheticSource:
    """A demo pod: plausible ECG / PPG / IMU through the real frame encoder.

    The signals are synthetic and the viewer is told so (``demo: true``). ``gap_every``
    skips a sequence number every N frames of the ECG, so the loss reporting can be seen.
    """

    name = "synthetic"
    demo = True

    def __init__(self, tick_s: float = 0.1, gap_every: int = 0) -> None:
        self.tick_s = tick_s
        self.gap_every = gap_every
        self._meta = stream_metadata()

    @staticmethod
    def _ecg(t: np.ndarray, hr_bpm: float = 72.0) -> np.ndarray:
        """Sum of gaussians per beat (P, QRS, T), in microvolts."""
        phase = (t * hr_bpm / 60.0) % 1.0
        wave = (
            120 * np.exp(-((phase - 0.18) ** 2) / 0.0008)
            - 150 * np.exp(-((phase - 0.285) ** 2) / 0.00015)
            + 1100 * np.exp(-((phase - 0.30) ** 2) / 0.00012)
            - 220 * np.exp(-((phase - 0.315) ** 2) / 0.00015)
            + 260 * np.exp(-((phase - 0.55) ** 2) / 0.006)
        )
        return wave

    def _samples(self, stream: int, t0_s: float, n: int, rate_hz: int) -> np.ndarray:
        t = t0_s + np.arange(n) / rate_hz
        channels = int(self._meta[stream]["channels"])
        if stream == int(RawStream.ECG):
            return self._ecg(t).reshape(-1, 1)
        if stream == int(RawStream.PPG):
            pulse = np.sin(2 * np.pi * 1.2 * t) + 0.35 * np.sin(4 * np.pi * 1.2 * t + 0.6)
            return np.stack([52000 + 3000 * pulse, 61000 + 4200 * pulse], axis=1)
        base = {int(RawStream.ACC): (60, 20, 980), int(RawStream.GYRO): (5, 2, 1)}.get(
            stream, (150, -40, 300)
        )
        wobble = np.sin(2 * np.pi * 0.4 * t)
        return np.stack([b + 8 * wobble * (i + 1) for i, b in enumerate(base[:channels])], axis=1)

    async def run(self, on_frame: Callable[[bytes], None]) -> None:
        sequences = dict.fromkeys(self._meta, 0)
        ecg_frames = 0
        clock_s = 0.0
        while True:
            for stream, meta in self._meta.items():
                if stream == int(RawStream.POWER):
                    continue  # 1 Hz telemetry: not worth a plot
                rate = int(meta["rate_hz"])
                n = max(1, round(rate * self.tick_s))
                samples = self._samples(stream, clock_s, n, rate)
                fmt = RawFormat(int(meta["format"]))
                frame = encode_frame(
                    stream,
                    sequences[stream],
                    int(clock_s * 1e6),
                    0,
                    fmt,
                    np.rint(samples).astype(np.int64).reshape(-1),
                )
                sequences[stream] = (sequences[stream] + 1) & 0xFFFF
                on_frame(frame)
                if stream == int(RawStream.ECG):
                    ecg_frames += 1
                    if self.gap_every and ecg_frames % self.gap_every == 0:
                        # the next ECG frame will carry a sequence that skips one: a lost frame
                        sequences[stream] = (sequences[stream] + 1) & 0xFFFF
            clock_s += self.tick_s
            await asyncio.sleep(self.tick_s)


class TransportSource:
    """A band, through the SDK transport (BLE, or any object satisfying ``Transport``)."""

    demo = False

    def __init__(self, address: str, transport: Any = None) -> None:
        self.name = f"ble:{address}" if transport is None else f"transport:{address}"
        self._address = address
        self._transport = transport

    async def run(self, on_frame: Callable[[bytes], None]) -> None:
        from synapse_device.transport import (
            CHAR_ACC,
            CHAR_ECG,
            CHAR_GYRO,
            CHAR_MAG,
            CHAR_PPG,
            build_transport,
        )

        transport = self._transport or build_transport(self._address)
        await transport.connect(self._address)
        try:
            await transport.request_mtu(247)
            subscribed = 0
            for uuid in (CHAR_ECG, CHAR_PPG, CHAR_ACC, CHAR_GYRO, CHAR_MAG):
                try:
                    await transport.subscribe(uuid, lambda _uuid, data: on_frame(bytes(data)))
                    subscribed += 1
                except Exception:  # noqa: BLE001 - one missing characteristic must not end the feed
                    logger.warning("cannot subscribe to %s: that stream will be missing", uuid)
            if subscribed == 0:
                raise RuntimeError("the device exposes none of the raw streams")
            await asyncio.Event().wait()  # until cancelled
        finally:
            await transport.disconnect()


def make_source(spec: str) -> LiveSource | None:
    """Build a source from ``SYNAPSE_LIVE_SOURCE``: ``synthetic`` | ``ble:<address>`` | off."""
    spec = spec.strip()
    if not spec or spec.lower() == "off":
        return None
    if spec.lower() == "synthetic":
        return SyntheticSource()
    if spec.lower().startswith("ble:") and spec[4:].strip():
        return TransportSource(spec[4:].strip())
    raise ValueError(f"{SOURCE_ENV}={spec!r}: expected 'synthetic', 'ble:<address>' or 'off'")


class LiveHub:
    """Runs one source for as many clients as are connected; stops it with the last one."""

    def __init__(self, source: LiveSource | None) -> None:
        self.source = source
        self.decoder = FrameDecoder()
        self._clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()
        self.last_error: str | None = None
        self.overflowed = 0

    def hello(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": "hello",
            "source": self.source.name if self.source else "off",
            "demo": bool(self.source and self.source.demo),
            "streams": [
                {
                    "name": m["name"],
                    "channels": m["channels"],
                    "unit": m["unit"],
                    "rate_hz": m["rate_hz"],
                }
                for m in self.decoder.meta.values()
                if m["name"] != "power"
            ],
        }
        if self.last_error:
            message["error"] = self.last_error  # a late client must see why there is no feed
        return message

    def _publish(self, message: dict[str, Any]) -> None:
        for queue in self._clients:
            if queue.full():
                try:
                    queue.get_nowait()  # a slow client loses its oldest message, not the others' feed
                except asyncio.QueueEmpty:  # pragma: no cover - raced with the consumer
                    pass
                self.overflowed += 1
            queue.put_nowait(message)

    def _on_frame(self, data: bytes) -> None:
        try:
            for message in self.decoder.decode(data):
                self._publish(message)
        except ValueError as exc:  # RawCodecError: a corrupt frame is reported, never plotted
            self._publish({"type": "error", "error": f"undecodable frame: {exc}"})

    def _bind_to_running_loop(self) -> None:
        """Drop state that belongs to another event loop (a restarted server, a test client)."""
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            self._loop = loop
            self._clients = set()
            self._task = None
            self._lock = asyncio.Lock()

    async def attach(self) -> asyncio.Queue[dict[str, Any]]:
        self._bind_to_running_loop()
        async with self._lock:  # attach and detach never interleave around the source task
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
            self._clients.add(queue)
            # A source that died is restarted by the next client, not left as a finished task.
            if self.source is not None and (self._task is None or self._task.done()):
                self.last_error = None
                self.decoder.reset()
                self._task = asyncio.create_task(self._run())
            return queue

    async def detach(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._bind_to_running_loop()
        async with self._lock:
            self._clients.discard(queue)
            if not self._clients and self._task is not None:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None

    async def _run(self) -> None:
        assert self.source is not None
        try:
            await self.source.run(self._on_frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the source is the outside world
            logger.exception("live source %s stopped", self.source.name)
            self.last_error = f"live source stopped: {exc}"
            self._publish({"type": "error", "error": self.last_error})
