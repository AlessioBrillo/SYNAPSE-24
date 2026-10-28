# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""Session recording for the ``synapse record`` CLI (synthetic + BLE).

Both paths decode frames through the real codec and write an **XDF 1.0**
file with the canonical writer (``synapse_device.xdf_writer``), so the
result is read back by ``pyxdf`` -- a third-party parser -- and not by a
home-grown reader that would happily accept a file no other tool can open.

The round-trip is a hard check: if ``pyxdf`` cannot read the file back with
the expected number of samples, the recording fails instead of leaving a
corrupt session behind.

``record_synthetic`` is the offline path (no hardware). ``record_ble`` is
the real-pod path: it connects, negotiates the link MTU (reported, never
assumed), subscribes the raw-stream characteristics, and anchors the pod's
(pod-boot-relative, 32-bit) frame stamps to the wall clock of the first
frame. **No sync exchange is performed here**: the recorded timestamps are
"pod-relative, wall-anchored", and the calibrated pod->host mapping stays a
research-client concern (MVP-1.4: the NTP-style ``ClockExchange`` lives in
:mod:`synapse_device.clock`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import numpy as np

from .codec import STREAM_CHANNELS, RawCodecError, RawHeader, RawStream, decode_frame
from .transport import (
    CHAR_UUID_TO_STREAM,
    MTU_TARGET,
    SyntheticTransport,
    Transport,
    build_transport,
)
from .xdf_writer import write_xdf

# The characteristic UUIDs come from the transport module (one GATT surface,
# decision Q7); record.py derives the RawStream view of that map instead of
# carrying a second copy of the UUID strings.
_GATT_RAW_CHARS: dict[str, RawStream] = {
    uuid: RawStream(stream_id) for uuid, stream_id in CHAR_UUID_TO_STREAM.items()
}

# Stream metadata: name, LSL type, channel names and units.
STREAM_META: dict[RawStream, tuple[str, str, list[str], list[str]]] = {
    RawStream.ECG: ("ECG", "ECG", ["ecg_uv"], ["uV"]),
    RawStream.PPG: ("PPG", "PPG", ["ppg_red", "ppg_ir"], ["au", "au"]),
    RawStream.ACC: ("ACC", "IMU", ["acc_x_mg", "acc_y_mg", "acc_z_mg"], ["mg", "mg", "mg"]),
    RawStream.GYRO: ("GYRO", "IMU", ["gyro_x_dps", "gyro_y_dps", "gyro_z_dps"], ["dps"] * 3),
    RawStream.MAG: ("MAG", "IMU", ["mag_x_ut", "mag_y_ut", "mag_z_ut"], ["uT"] * 3),
}

# The streams a recording subscribes: the physical-data characteristics of
# STREAM_META. The POWER characteristic is the Tier-1 telemetry probe of
# ``synapse info``, not a session stream, and SYNC belongs to the clock
# model, not the codec.
_RECORD_UUIDS: tuple[str, ...] = tuple(
    uuid for uuid, stream in _GATT_RAW_CHARS.items() if stream in STREAM_META
)

# Wrap-witness rule for the extended pod clock: a backward low step that
# spans THIS much of the 32-bit counter can only be a roll; smaller backward
# steps are delayed deliveries, handled by the monotonic clamp.
_MAX_DELIVERY_GAP_S = 900.0
_WRAP_REGRESSION_FLOOR = (1 << 32) - int(_MAX_DELIVERY_GAP_S * 1_000_000)


class _PodClock:
    """Extends one stream's 32-bit frame stamps to monotonic device time.

    Per N5: the frame's wrap flag is one witness, and a backward low stamp
    that carries the whole 32-bit counter (flag lost with the frame) is the
    other -- *only when the step is so large nothing else explains it*.
    Backward steps inside the counter are delivery reordering for the
    monotonic clamp; they must never read as wraps.
    """

    wraps: int = 0
    _last_low: int | None = None

    def extend(self, header: RawHeader) -> int:
        """Return this frame's monotonic 64-bit device microseconds.

        A flagged frame is the FIRST frame after the roll: one region up
        before the stamp is assembled. The first frame of a capture carries
        no history, so it can never be identified as post-wrap on its own.
        """
        low = int(header.timestamp_us)
        if self._last_low is not None and (
            header.ts_epoch_wrapped or self._last_low - low >= _WRAP_REGRESSION_FLOOR
        ):
            self.wraps += 1
        self._last_low = low
        return (self.wraps << 32) | low


class XdfRoundTripError(RuntimeError):
    """Raised when the XDF file just written cannot be read back."""


class BleRecordError(RuntimeError):
    """Raised when a BLE recording collected nothing usable."""


def _verify_roundtrip(path: Path) -> int:
    """Read the XDF back with pyxdf and return the number of recovered samples."""
    try:
        import pyxdf
    except ImportError as exc:  # pragma: no cover - pyxdf is an optional extra
        raise XdfRoundTripError(
            "pyxdf is required to verify the XDF round-trip: pip install synapse-device[xdf]"
        ) from exc
    streams, _header = pyxdf.load_xdf(str(path))
    return int(sum(len(stream.get("time_stamps", [])) for stream in streams))


def _measured_rate(device_us: np.ndarray, n_chunks: list[int]) -> float:
    """Return the median per-frame sample rate read from the pod's own clock.

    Args:
        device_us: Monotonic pod-relative device stamps per frame (us).
        n_chunks: The number of samples carried by the SAME-indexed frame.

    Returns:
        The measured sample rate in Hz, or 0.0 when it cannot be measured
        (fewer than two sample-carrying frame boundaries). 0.0, not a
        guess, is the LSL "irregular" semantic.

    """
    if len(device_us) < 2 or not any(count > 0 for count in n_chunks[:-1]):
        return 0.0
    deltas = np.diff(device_us).astype(np.float64)
    carried = np.asarray(n_chunks[:-1], dtype=np.float64)
    usable = deltas > 0
    if not bool(usable.any()):
        return 0.0
    return float(np.median(carried[usable] * 1e6 / deltas[usable]))


def _xdf_payload(
    collected: list[tuple[RawHeader, np.ndarray, int]],
    streams: list[RawStream],
    source_prefix: str,
    wall_origin_s: float,
) -> tuple[list[dict[str, object]], dict[RawStream, float]]:
    """Turn decoded frames into XDF stream payloads plus measured rates.

    Args:
        collected: ``(header, channel-minor values, 64-bit pod-relative
            device us)`` for every frame, in arrival order.
        streams: The declared candidate stream set.
        source_prefix: XDF ``source_id`` prefix per stream (e.g.
            ``synapse-device-3C:1D:...``).
        wall_origin_s: Wall-clock seconds of the FIRST delivered frame; the
            device-relative pod deltas are added onto this anchor.

    Returns:
        One XDF stream dict per stream that delivered frames, and the
        per-stream measured rate in Hz (0.0 = unmeasured/irregular).

    """
    by_stream: dict[RawStream, list[tuple[int, np.ndarray]]] = {stream: [] for stream in streams}
    for header, values, pod_us in collected:
        stream = RawStream(header.stream)
        if stream in by_stream:
            by_stream[stream].append((pod_us, values))

    pod_origin = min((frame[2] for frame in collected), default=0)
    payload: list[dict[str, object]] = []
    measured: dict[RawStream, float] = {}
    for stream in streams:
        entries = by_stream[stream]
        if not entries:
            continue
        # Order by the pod's own device timeline, then clamp: a stale frame
        # keeps its data, never fights the timeline.
        entries.sort(key=lambda entry: entry[0])
        device_us = np.maximum.accumulate(np.array([entry[0] for entry in entries], dtype=np.int64))
        n_chans = STREAM_CHANNELS[stream]
        chunks = [np.asarray(values).reshape(-1, n_chans) for _, values in entries]
        n_chunks = [chunk.shape[0] for chunk in chunks]
        rate = _measured_rate(device_us, n_chunks)
        measured[stream] = rate

        # Per-sample wall timestamps: the frame header stamps its first
        # sample (base = wall anchor + device delta), the following samples
        # are spaced by the stream period measured above. When no rate could
        # be measured, every sample of the frame shares the frame's base
        # (an XDF nominal rate of 0 = irregular, never an assumption).
        stamps: list[np.ndarray] = []
        for index, chunk in enumerate(chunks):
            base = wall_origin_s + (float(device_us[index]) - float(pod_origin)) * 1e-6
            if rate > 0.0:
                stamps.append(base + np.arange(chunk.shape[0], dtype=np.float64) / rate)
            else:
                stamps.append(np.full(chunk.shape[0], base, dtype=np.float64))
        timestamps = np.concatenate(stamps)

        data = np.concatenate(chunks).astype(np.float64)
        name, type_, channels, units = STREAM_META[stream]
        payload.append(
            {
                "name": name,
                "type": type_,
                "channel_count": int(data.shape[1]),
                "sampling_rate": rate,
                "channel_format": "float32",
                "channel_names": channels,
                "channel_units": units,
                "source_id": f"{source_prefix}-{name.lower()}",
                "data": data,
                "timestamps": timestamps,
            }
        )
    return payload, measured


def _extend_stamps(
    delivered: list[tuple[RawHeader, np.ndarray, float]],
) -> list[tuple[RawHeader, np.ndarray, int]]:
    """Extend each frame's low 32-bit stamps through a per-stream pod clock.

    Returns the ``(header, values, 64-bit pod-relative device us)`` tuples
    that :func:`_xdf_payload` consumes, in arrival order.
    """
    clocks: dict[int, _PodClock] = {}
    frames: list[tuple[RawHeader, np.ndarray, int]] = []
    for header, values, _epoch in delivered:
        clock = clocks.setdefault(int(header.stream), _PodClock())
        frames.append((header, values, clock.extend(header)))
    return frames


async def _capture_frames(
    bridge: Transport,
    streams: list[RawStream],
    duration_s: float,
    *,
    start_emitter: Callable[[], Awaitable[None]] | None = None,
    stop_emitter: Callable[[], Awaitable[None]] | None = None,
) -> list[tuple[RawHeader, np.ndarray, float]]:
    """Subscribe the raw characteristics for ``duration_s`` wall-clock seconds.

    Each delivered frame is recorded with its wall-clock arrival (s), so the
    caller can anchor the pod's device-relative stamps to the host wall
    clock. Malformed frames are dropped (they were already counted on the
    wire, not trusted); frames whose stream is not in ``streams`` are
    ignored here.

    Args:
        bridge: Connected transport to capture from.
        streams: The streams to keep (the session streams of STREAM_META).
        duration_s: Wall-clock capture window.
        start_emitter: Optional async hook fired after subscribing (the
            synthetic pod needs this to start emitting).
        stop_emitter: Optional async hook fired after unsubscribing.

    Returns:
        ``[(header, channel-minor values, delivered_epoch_s), ...]`` in
        delivery order.

    """
    delivered: list[tuple[RawHeader, np.ndarray, float]] = []

    def _on_frame(_uuid: str, frame: bytes) -> None:
        try:
            header, values = decode_frame(frame)
        except RawCodecError:
            return
        if header.stream in streams:
            delivered.append((header, values, time.time()))

    for uuid in _RECORD_UUIDS:
        await bridge.subscribe(uuid, _on_frame)
    try:
        if start_emitter is not None:
            await start_emitter()
        await asyncio.sleep(max(duration_s, 0.0))
    finally:
        for uuid in _RECORD_UUIDS:
            try:
                await bridge.unsubscribe(uuid)
            except Exception:  # noqa: BLE001 - unsubscribe is best effort
                pass
    if stop_emitter is not None:
        await stop_emitter()
    return delivered


def _write_and_verify(
    frames: list[tuple[RawHeader, np.ndarray, int]],
    wall_origin_s: float,
    streams: list[RawStream],
    source_prefix: str,
    out_path: Path,
) -> tuple[list[dict[str, object]], dict[RawStream, float]]:
    """Build the payload, write the XDF and fail loudly on a bad round-trip.

    Returns:
        ``(payload, measured)`` exactly as :func:`_xdf_payload` produced
        them, for the caller's summary.

    """
    payload, measured = _xdf_payload(frames, streams, source_prefix, wall_origin_s)
    expected_total = int(sum(int(np.asarray(item["data"]).shape[0]) for item in payload))
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_xdf(path, payload)
    recovered = _verify_roundtrip(path)
    if recovered != expected_total:
        raise XdfRoundTripError(
            f"XDF round-trip mismatch for {path}: wrote {expected_total} samples, "
            f"read back {recovered}"
        )
    return payload, measured


def record_synthetic(out_path: Path, duration_s: float = 30.0, rate_hz: int = 50) -> None:
    """Record ``duration_s`` seconds of synthetic multi-stream data to an XDF file.

    The synthetic pod speaks the real frame format, so this exercises the
    full recording path (decode -> pod-clock extension -> XDF) without
    hardware. The declared stream rate is the one MEASURED from the frames'
    own timestamps, never the requested one.

    Args:
        out_path: Destination ``.xdf`` file.
        duration_s: Length of the synthetic recording.
        rate_hz: Rate the synthetic pod runs at.

    Raises:
        XdfRoundTripError: if the file cannot be read back with the expected
            number of samples (a silent corruption would poison every
            downstream analysis).

    """
    streams = list(STREAM_META)
    bridge = SyntheticTransport("SYNTHETIC")

    async def _start() -> None:
        await bridge.start(list(_RECORD_UUIDS), rate_hz=rate_hz, duration_s=duration_s)

    async def _stop() -> None:
        await bridge.stop()

    async def _run() -> list[tuple[RawHeader, np.ndarray, float]]:
        await bridge.connect("SYNTHETIC")
        try:
            return await _capture_frames(
                bridge, streams, duration_s + 0.5, start_emitter=_start, stop_emitter=_stop
            )
        finally:
            try:
                await bridge.disconnect()
            except Exception:  # noqa: BLE001 - disconnect is best effort
                pass

    delivered = asyncio.run(_run())
    wall_origin_s = delivered[0][2] if delivered else 0.0
    _write_and_verify(
        _extend_stamps(delivered),
        wall_origin_s,
        streams,
        "synapse-device-synthetic",
        out_path,
    )


def record_ble(
    address: str,
    out_path: Path,
    duration_s: float = 30.0,
    transport: Transport | None = None,
    mtu_target: int | None = MTU_TARGET,
) -> dict[str, object]:
    """Record a real pod over BLE into an XDF file (the ``--source <addr>`` path).

    Connects to ``address``, negotiates the link MTU (reported, never
    assumed), subscribes the raw characteristics and captures notification
    frames for ``duration_s`` wall-clock seconds. The XDF timestamps are the
    pod's device-relative stamps anchored to the wall clock of the FIRST
    delivery; no sync exchange is performed (see the module docstring).

    Args:
        address: The real BLE address of the pod. ``SYNTHETIC`` routes
            through :func:`synapse_device.transport.build_transport` to the
            in-process pod.
        out_path: Destination ``.xdf`` file.
        duration_s: Wall-clock capture window.
        transport: Injectable transport (tests); default is the built one.
        mtu_target: ATT MTU to request; ``None`` skips the negotiation.

    Returns:
        Summary dict: ``granted_mtu`` (int or None), ``frames`` (per-stream
        frame counts), ``rate_hz`` (measured from the frames' own stamps),
        ``dropped`` (the loss counters the frames carried, summed),
        ``stream_names``.

    Raises:
        BleRecordError: when the pod streamed no frame at all.
        XdfRoundTripError: when the written XDF cannot be read back intact.

    """
    streams = list(STREAM_META)

    async def _run() -> tuple[list[tuple[RawHeader, np.ndarray, int]], float, int | None]:
        bridge = transport if transport is not None else build_transport(address)
        await bridge.connect(address)
        granted: int | None = None
        if mtu_target is not None:
            granted = await bridge.request_mtu(int(mtu_target))
        try:
            delivered = await _capture_frames(bridge, streams, duration_s)
        finally:
            try:
                await bridge.disconnect()
            except Exception:  # noqa: BLE001 - disconnect is best effort
                pass
        if not delivered:
            return [], 0.0, granted
        # The wall anchor is the FIRST delivery; the pod stamps are extended
        # per stream afterwards (wrap flag + regression floor).
        return _extend_stamps(delivered), delivered[0][2], granted

    frames, wall_origin_s, granted = asyncio.run(_run())
    if not frames:
        raise BleRecordError(
            f"no frames arrived within {duration_s:.1f} s from {address}: the pod streamed "
            f"nothing on the raw characteristics (check the pod is awake, the research "
            f"service is on, and the requested MTU {mtu_target} was granted)"
        )
    _, measured = _write_and_verify(
        frames, wall_origin_s, streams, f"synapse-device-{address}", Path(out_path)
    )

    frame_counts: dict[str, int] = {}
    for frame in frames:
        name = STREAM_META[RawStream(frame[0].stream)][0]
        frame_counts[name] = frame_counts.get(name, 0) + 1
    return {
        "granted_mtu": granted,
        "frames": frame_counts,
        "rate_hz": {STREAM_META[stream][0]: rate for stream, rate in measured.items()},
        "dropped": int(sum(frame[0].dropped for frame in frames)),
        "stream_names": [STREAM_META[stream][0] for stream in streams],
    }
