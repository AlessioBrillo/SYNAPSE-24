"""Host-side decoding of the Synapse Research Service BLE stream.

Responsible for turning raw GATT notifications back into timestamped,
per-channel numpy arrays, and for exposing them as LSL outlets so the rest of
the pipeline (quality metrics, XDF recording, fusion) can treat live hardware
exactly like a public dataset.

Two things happen here that matter more than the parsing itself:

1. **Sequence-gap detection.** The firmware counts frames it could not send
   instead of silently dropping them, and this decoder watches for jumps in
   the per-stream sequence number. A hole in a biosignal dataset is far more
   dangerous than a recording that stops, so a gap is surfaced as a DropEvent
   rather than smoothed over.

2. **64-bit timeline reconstruction.** The pod's microsecond counter is only
   32 bits wide and wraps every ~4295 s (~71 min). Frames carry the low 32
   bits plus a wrap flag; :class:`DeviceClock` rebuilds a monotonic 64-bit
   timeline so that a 24 h Tier-0 recording does not jump backwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from synapse24.hardware.raw_codec import (
    HEADER_SIZE,
    MAX_PAYLOAD,
    STREAM_CHANNELS,
    RawCodecError,
    RawHeader,
    RawStream,
    decode_frame,
    format_size,
)

__all__ = [
    "DropEvent",
    "DeviceClock",
    "ResearchStreamDecoder",
    "TS_WRAP_US",
    "normative_stream_rates",
]

# The device microsecond counter wraps at 2^32 us (~4295 s).
TS_WRAP_US = 1 << 32


def normative_stream_rates() -> dict[RawStream, float]:
    """Nominal rate per raw stream, from ``config/hardware.yaml``.

    The manifest is the single source of truth for the rates: the samples of
    a frame are spaced with the stream period, and a hardcoded literal is how
    the rates drifted in the first place.
    """
    try:
        from synapse24.hardware.esp32_tier0 import forearm_hub_normative_rates

        rates = forearm_hub_normative_rates()
    except Exception:  # noqa: BLE001 - rates only affect intra-frame spacing
        return {}
    return {
        RawStream.ECG: float(rates["ecg_hz"]),
        RawStream.PPG: float(rates["ppg_hz"]),
        RawStream.ACC: float(rates["imu_hz"]),
        RawStream.GYRO: float(rates["imu_hz"]),
        RawStream.MAG: float(rates["imu_hz"]),
        RawStream.POWER: 1.0,  # one telemetry frame per second
    }


# Unit divisors applied by the firmware when quantising (see
# synapse_research_unit_t in ble_research_service.h).
UNIT_DIVISOR: dict[RawStream, float] = {
    RawStream.ECG: 1000.0,  # uV
    RawStream.PPG: 1.0,  # raw ADC counts
    RawStream.ACC: 1000.0,  # mg
    RawStream.GYRO: 1000.0,  # mrad/s
    RawStream.MAG: 100.0,  # 0.01 uT
    RawStream.TEMP: 1.0,  # 0.01 degC
    RawStream.POWER: 1.0,  # mV, mA, mW, % — already engineering units
    RawStream.SYNC: 1.0,
}

# LSL stream names, from config/hardware.yaml.
LSL_STREAM_NAMES: dict[RawStream, str] = {
    RawStream.ECG: "SYNAPSE_ECG_T0",
    RawStream.PPG: "SYNAPSE_PPG_T0",
    RawStream.ACC: "SYNAPSE_ACC_T0",
    RawStream.GYRO: "SYNAPSE_GYRO_T0",
    RawStream.MAG: "SYNAPSE_MAG_T0",
    RawStream.TEMP: "SYNAPSE_TEMP_T0",
    RawStream.POWER: "SYNAPSE_Power",
    RawStream.SYNC: "SYNAPSE_SYNC_T0",
}

# The largest pod-time step between two consecutive accepted frames that the
# numeric wrap witness still covers (defect N5). A backward step spanning this
# much of the counter can only be a roll. On a live link it cannot be a delayed
# notification: the BLE supervision timeout kills a stalled connection in tens
# of seconds, so silences beyond this arrive only in two shapes — the genuine
# roll (with the firmware's flag), and the time-compressed gates whose frame
# strides (240 s validator soak, 600 s codec compression) sit inside it on
# purpose. A longer silence without the flag is a restart/reconnect, which the
# sequence re-base (N2) handles, never a roll.
_MAX_FRAME_GAP_US = 900_000_000
# A backward step of the low stamp this large can only be a roll: the previous
# stamp was within one frame gap of the counter top and the new one landed in
# the fresh region. A delayed delivery regresses by its backlog; it cannot
# span the whole counter.
_ROLL_REGRESSION_FLOOR = (1 << 32) - _MAX_FRAME_GAP_US

# The streams the pod puts on the wire (the SYNC stream is handled by the
# clock model, not by the frame decoder), in the same order as the codec.
WIRE_STREAMS: tuple[RawStream, ...] = tuple(s for s in RawStream if s != RawStream.SYNC)


@dataclass
class DropEvent:
    """A detected hole in the sample stream."""

    stream: RawStream
    expected_seq: int
    received_seq: int
    gap: int

    def __str__(self) -> str:
        return (
            f"{self.stream.name}: sequence jumped {self.expected_seq} -> "
            f"{self.received_seq} (gap of {self.gap} frame(s) lost in transit)"
        )


@dataclass
class DeviceClock:
    """Rebuilds a monotonic 64-bit device timeline from 32-bit frame stamps."""

    wraps: int = 0
    _last_low: int | None = None
    _last_us: int = 0
    # The pod's full 64-bit time as of the last sync anchor (set_wrap_base).
    # A frame's capture cannot be in the future of the pod's own present-time
    # read: a claimed region beyond the anchor's is the stale-delivery
    # artefact, not a roll, and is dropped (defect N5).
    _pod_now_us: int | None = None

    def extend(self, timestamp_low: int, flags: int = 0) -> int:
        """Convert a frame's low-32-bit stamp into a monotonic microsecond time."""
        if self._last_low is not None:
            # Two independent witnesses for the 2^32-us roll (~71 minutes): the
            # pod's wrap flag on the first frame after the edge, and the numeric
            # regression of the low stamps. Either one must move the wrap
            # counter exactly once. The old code required the regression, which
            # made the flag pure decoration: when the link stalls across the
            # edge, the next frame's stamp can already sit ABOVE the previous
            # one while its flag still says "wrapped" — the regression never
            # fires and 71 minutes of pod time went missing. (defect N3)
            #
            # The regression, though, only *witnesses* a roll when it is big
            # enough that nothing else explains it (defect N5, found by the
            # compressed-soak gate): a low stamp can also regress because a
            # notification was DELAYED — BLE retries, a busy hub loop, or a
            # periodic sync anchor whose pod stamp outruns the stale frames
            # ahead of it. That step is bounded by the delivery backlog and is
            # micro-seconds to seconds; treating it as a wrap added 2^32 us
            # (~71.6 min) of phantom pod time. Only a backward step that spans
            # the counter — previous stamp near the top, new stamp within one
            # credible inter-frame gap of the bottom — is the roll.
            if flags & 0x01 or self._last_low - timestamp_low >= _ROLL_REGRESSION_FLOOR:
                self.wraps += 1
        candidate = (self.wraps << 32) | timestamp_low
        if self._pod_now_us is not None and candidate > self._pod_now_us + _MAX_FRAME_GAP_US:
            # The frame reaches beyond the anchor's present by more than any
            # credible inter-frame gap: its true capture sits in the region
            # BELOW the anchor's (a delivery that lagged across a region
            # boundary), so one region down. The legitimate roll case claims
            # only the anchor plus the elapsed silence — far below one region.
            candidate -= 1 << 32
        if candidate <= self._last_us:
            candidate = self._last_us + 1
        self._last_low = timestamp_low
        self._last_us = candidate
        return candidate

    def set_wrap_base(self, wraps: int, ref_low: int) -> None:
        """Anchor the 64-bit reconstruction to a known full 64-bit pod time.

        Called after a clock-sync exchange reveals the pod's true
        ``esp_timer_get_time()`` value (64 bits, since boot). Without this,
        ``wraps`` starts at 0 and every pod up longer than ~71 minutes gets
        its timestamps shifted by a multiple of 2^32 µs (~71.6 min).

        The anchor sets the wrap region and the numeric foundation, and
        records the pod's present-time value for the plausibility drop in
        :meth:`extend`. It deliberately does NOT raise ``_last_us``: the
        monotonic baseline stays frame-based, so a frame that was captured
        just before the anchor keeps its own capture time instead of being
        nudged up to the anchor's present (a sync exchange sits between
        frames by construction; only a delayed delivery straddles it).
        """
        self.wraps = wraps
        self._last_low = ref_low
        self._pod_now_us = (wraps << 32) | ref_low


@dataclass
class ResearchStreamDecoder:
    """Accumulates decoded frames per stream and exposes numpy-friendly output.

    ``history_limit`` caps the number of values kept per stream (0 = no cap).
    The live path sets it: the dataset is written by the *spool* (on disk),
    so the decoder only needs the recent window the quality metrics work on.
    Without a cap the decoder keeps every sample of the session in RAM, and
    a caller that asks for the tail on every notification pays an O(N) copy
    per notification — quadratic over a session.
    """

    samples: dict[RawStream, list[np.ndarray]] = field(default_factory=dict)
    timestamps: dict[RawStream, list[np.ndarray]] = field(default_factory=dict)
    sequences: dict[RawStream, int] = field(default_factory=dict)
    drops: list[DropEvent] = field(default_factory=list)
    # One timeline per stream: the wire carries only the low 32 bits of each
    # stream's own counter, and each stream is framed independently, so a
    # single shared DeviceClock would read every later stream's restart as a
    # 32-bit wrap and shift it by 2^32 µs (~71 minutes). That is not a
    # hypothetical: it put the ACC stream two hours ahead of the ECG stream
    # and made the pod→host clock map meaningless.
    clocks: dict[RawStream, DeviceClock] = field(default_factory=dict)
    frames_seen: int = 0
    frames_rejected: int = 0
    # Actual payload volume decoded from the wire (header + sample block of
    # every accepted frame). The previous estimate multiplied the frame count
    # by the 244-byte MTU payload, which over-reported real streams 2-6x
    # (an ECG frame is 112 bytes, a PPG frame 76) and made the BLE budget
    # gate judge the full rig against numbers it never sent.
    bytes_seen: int = 0
    # Extended (64-bit) timestamp of the most recent accepted frame, in
    # microseconds. The frame header only carries the low 32 bits of the
    # pod's counter, so anything mapping pod time into the host domain must
    # use this reconstructed value, never the raw header stamp.
    last_first_us: int = 0
    # Samples the pod reports as lost, per stream (the `dropped` byte of the
    # frame header). The sequence gap says *that* something is missing, the
    # counter says *how much*: a hole of one frame and a hole of a hundred
    # samples are different failures.
    dropped_samples: dict[RawStream, int] = field(default_factory=dict)
    # Per-stream count of detected firmware restarts (the pod reboots, or the
    # link breaks and re-forms without the caller masking it with reset()):
    # the sequence and the pod clock both count from zero again. That is
    # expected and must never be read as a loss of ~65k frames — but it must
    # not be swallowed either, so it is typed here for the session report.
    restarts: dict[RawStream, int] = field(default_factory=dict)
    # Maximum number of values kept per stream (0 = unlimited). The live
    # path sets it: the session is written by the spool (on disk), so the
    # decoder only needs the recent window.
    history_limit: int = 0
    # Nominal rate per stream (Hz), used to space the samples inside a frame.
    stream_rates_hz: dict[RawStream, float] = field(default_factory=dict)

    def feed(self, frame: bytes | bytearray) -> RawHeader:
        """Decode one GATT notification and buffer its samples.

        Returns:
            The decoded header, even when the payload was rejected, so callers
            can log or inspect malformed frames.

        Raises:
            RawCodecError: If the frame cannot be decoded at all (length or
                version). Recoverable per-stream problems are recorded as
                :class:`DropEvent` instead.
        """
        try:
            header, samples = decode_frame(frame)
        except RawCodecError:
            self.frames_rejected += 1
            raise

        if header.sample_count and header.stream in STREAM_CHANNELS:
            self._check_sequence(header)
            self._buffer(header, samples)
            self.frames_seen += 1
            # Wire truth: header + count values of the declared format.
            self.bytes_seen += HEADER_SIZE + int(header.sample_count) * format_size(
                header.sample_format
            )
        else:
            self.frames_rejected += 1
        return header

    def _check_sequence(self, header: RawHeader) -> None:
        previous = self.sequences.get(header.stream)
        if previous is None:
            self.sequences[header.stream] = header.sequence
            return
        # 16-bit sequence: detect a forward jump, tolerating the wrap itself.
        delta = (header.sequence - previous) & 0xFFFF
        if delta > 1:
            clock = self.clocks.get(header.stream)
            regressed = (
                clock is not None
                and clock._last_low is not None
                and header.timestamp_us < clock._last_low
            )
            if delta > 0x8000 and regressed:
                # Firmware restart, not loss: sequence counts from zero again
                # while the stream's own clock stepped backwards — the pod
                # rebooted (or the link re-formed without reset()). The old
                # code read every reconnect as "~65533 frames lost", drowning
                # the gate in phantom gaps (defect N2). Re-base the stream:
                # a fresh sequence baseline and a fresh wrap count.
                self.restarts[header.stream] = self.restarts.get(header.stream, 0) + 1
                self.sequences[header.stream] = header.sequence
                self.clocks.pop(header.stream, None)
                return
            self.drops.append(
                DropEvent(
                    stream=header.stream,
                    expected_seq=(previous + 1) & 0xFFFF,
                    received_seq=header.sequence,
                    gap=int(delta - 1),
                )
            )
        self.sequences[header.stream] = header.sequence

    def _buffer(self, header: RawHeader, samples: np.ndarray) -> None:
        channels = STREAM_CHANNELS.get(header.stream, 1)
        if samples.size % channels:
            self.frames_rejected += 1
            return

        device_clock = self.clocks.setdefault(header.stream, DeviceClock())
        first_us = device_clock.extend(header.timestamp_us, header.flags)
        self.last_first_us = first_us
        # Timestamps within a frame are not carried per sample: the frame
        # stamp is the time of the first sample, and the following samples
        # are spaced by the stream period (1/fs), not by one microsecond.
        # Spacing them by 1 us collapsed a 500 Hz ECG into a 1 MHz signal.
        n_samples = samples.size // channels
        rate_hz = self._rate_for(header.stream)
        if rate_hz > 0:
            step_us = 1_000_000.0 / rate_hz
            ts = first_us + np.arange(n_samples, dtype=np.float64) * step_us
        else:
            ts = first_us + np.arange(n_samples, dtype=np.float64)

        self.samples.setdefault(header.stream, []).append(
            samples.astype(np.float64) / UNIT_DIVISOR.get(header.stream, 1.0)
        )
        self.timestamps.setdefault(header.stream, []).append(ts.astype(np.float64))
        if header.dropped:
            self.dropped_samples[header.stream] = self.dropped_samples.get(header.stream, 0) + int(
                header.dropped
            )
        self._trim_history(header.stream)

    def _trim_history(self, stream: RawStream) -> None:
        """Drop the oldest frames of a stream beyond the history limit."""
        if not self.history_limit:
            return
        frames = self.samples.get(stream)
        if not frames:
            return
        total = 0
        for frame in reversed(frames):
            total += int(frame.size)
            if total > self.history_limit:
                break
        excess = total - self.history_limit
        while excess > 0 and len(frames) > 1:
            first = frames[0]
            if first.size > excess:
                break
            excess -= int(first.size)
            frames.pop(0)
            stamps = self.timestamps.get(stream)
            if stamps:
                stamps.pop(0)

    def _rate_for(self, stream: RawStream) -> float:
        """Nominal rate of a stream, from the manifest (loaded once)."""
        if not self.stream_rates_hz:
            self.stream_rates_hz = normative_stream_rates()
        return float(self.stream_rates_hz.get(stream, 0.0) or 0.0)

    def arrays(self, stream: RawStream) -> tuple[np.ndarray, np.ndarray]:
        """Return concatenated (samples, timestamps_us) for one stream."""
        if stream not in self.samples:
            empty = np.zeros(0, dtype=np.float64)
            return empty, empty.copy()
        return (
            np.concatenate(self.samples[stream]),
            np.concatenate(self.timestamps[stream]),
        )

    def tail(self, stream: RawStream, n_values: int) -> np.ndarray:
        """The last ``n_values`` values of a stream, without copying the history.

        The live path asks for the tail on every notification: concatenating
        the whole session each time is quadratic (and, on a 6-hour soak,
        minutes of pure copying).
        """
        frames = self.samples.get(stream)
        if not frames or n_values <= 0:
            return np.zeros(0, dtype=np.float64)
        needed = int(n_values)
        chunks: list[np.ndarray] = []
        total = 0
        for frame in reversed(frames):
            chunks.append(frame)
            total += int(frame.size)
            if total >= needed:
                break
        if not chunks:
            return np.zeros(0, dtype=np.float64)
        values = np.concatenate(list(reversed(chunks)))
        return np.asarray(values[-needed:], dtype=np.float64)

    def drop_summary(self) -> dict[str, int]:
        """Per-stream count of frames lost in transit."""
        counts: dict[str, int] = {}
        for event in self.drops:
            counts[event.stream.name] = counts.get(event.stream.name, 0) + event.gap
        return counts

    def restart_summary(self) -> dict[str, int]:
        """Per-stream count of firmware restarts (typed; not a loss)."""
        return {stream.name: count for stream, count in self.restarts.items()}

    def dropped_sample_summary(self) -> dict[str, int]:
        """Per-stream count of samples the pod reports as lost.

        The `dropped` byte of the frame header carries the magnitude of the
        loss the pod could not avoid (a full ring, a refused notification).
        It complements the sequence gap: the gap counts the *frames* the host
        never received, this counts the *samples* that are missing.
        """
        return {stream.name: count for stream, count in self.dropped_samples.items()}

    def is_clean(self) -> bool:
        """True when no frames were lost and none were rejected."""
        return not self.drops and self.frames_rejected == 0 and not self.dropped_samples

    def reset(self) -> None:
        """Forget the previous session.

        A pod that reconnects — or a second recording in the same process —
        restarts its per-stream sequence at zero. Without this reset the
        decoder compares 0 against the previous session's last sequence and
        reports the gap as ~65533 lost frames per stream. A reconnect is not
        a loss, and reporting it as one trains the reader to ignore the
        gate.
        """
        self.samples.clear()
        self.timestamps.clear()
        self.sequences.clear()
        self.clocks.clear()
        self.drops.clear()
        self.dropped_samples.clear()
        self.restarts.clear()
        self.frames_seen = 0
        self.frames_rejected = 0
        self.bytes_seen = 0
        self.last_first_us = 0

    def seed_from_sync(self, pod_full_us: int) -> None:
        """Anchor every stream's DeviceClock to a sync exchange's full pod time.

        ``pod_full_us`` is the pod's 64-bit ``esp_timer_get_time()`` from the
        SYNC exchange. Its high 32 bits are the wrap count; the low 32 bits
        are the reference low stamp. Every stream shares the same wrap base
        because they all read the same ``esp_timer_get_time()`` counter —
        including streams whose first frame has not arrived yet: a clock
        created by a later frame inherits the base instead of counting wraps
        from zero on a pod that has been up for hours.
        """
        wraps = pod_full_us >> 32
        ref_low = pod_full_us & 0xFFFFFFFF
        for stream in WIRE_STREAMS:
            self.clocks.setdefault(stream, DeviceClock()).set_wrap_base(wraps, ref_low)

    def estimated_bytes(self) -> int:
        """Actual payload volume seen on the wire, for the §55-62 budget."""
        return self.bytes_seen


def validate_notification_size(payload: bytes) -> None:
    """Reject payloads that could never have come from the research service."""
    if len(payload) > MAX_PAYLOAD:
        raise RawCodecError(
            f"notification is {len(payload)} bytes, exceeds MTU-3 budget of {MAX_PAYLOAD}"
        )
    if len(payload) < HEADER_SIZE:
        raise RawCodecError(
            f"notification is {len(payload)} bytes, below the {HEADER_SIZE}-byte header"
        )
