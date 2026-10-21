"""Raw-frame ingestion for the live Phase 1 validator.

`validate_phase1_entry.py` historically parsed the legacy Nordic-UART
``sensor_sample_t`` payload. The canonical firmware now streams the Synapse
Research Service frame format, so this module decodes that instead and pushes
the result into the same LSL outlets the rest of the gate reads.

The frame format is versioned and length-checked, so a payload from an
unexpected firmware build is reported rather than silently mis-parsed.

Timestamps: the frame header carries the pod's own microsecond stamp, which
is **not** the LSL clock domain (Guardian Principle 3). When a
:class:`~synapse24.acquisition.pod_clock_model.PodClockModel` is supplied,
every pushed timestamp is mapped into the LSL domain through it, so the
dataset keeps the device's true inter-sample spacing (free of BLE arrival
jitter) while sharing one timeline with markers and other pods. Without a
calibrated model nothing is claimed: the LSL outlet gets the host arrival
time and the sync gate is expected to fail loudly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from synapse24.hardware.raw_codec import RawStream, decode_frame
from synapse24.hardware.research_client import (
    UNIT_DIVISOR,
    ResearchStreamDecoder,
    normative_stream_rates,
)

logger = logging.getLogger(__name__)

# LSL outlet names, matching config/hardware_bringup.yaml.
OUTLET_BY_STREAM = {
    RawStream.ECG: "SYNAPSE_ECG_T0",
    RawStream.PPG: "SYNAPSE_PPG_T0",
    RawStream.ACC: "SYNAPSE_ACC_T0",
    RawStream.GYRO: "SYNAPSE_GYRO_T0",
    RawStream.MAG: "SYNAPSE_MAG_T0",
    RawStream.POWER: "SYNAPSE_Power",
}

# (outlet name, channel values, timestamp in seconds)
PushCallback = Callable[[str, list[float], float], None]


def _default_lsl_clock() -> float:
    try:
        from pylsl import local_clock

        return float(local_clock())
    except Exception:  # noqa: BLE001 - CI hosts without liblsl fall back
        import time

        return float(time.time())


def _normative_stream_rates() -> dict[RawStream, float]:
    """Nominal rate per raw stream, from config/hardware.yaml (single source)."""
    return normative_stream_rates()


@dataclass
class RawFrameIngestor:
    """Feeds research-service frames into LSL outlets and counts what arrives.

    Args:
        decoder: Research-service frame decoder with gap detection.
        push_callback: Called with (outlet, channel values, LSL timestamp).
        clock_model: Optional pod→hub clock model used to map pod stamps
            into the LSL clock domain.
        lsl_clock_fn: LSL clock source, injectable for tests.
        stream_rates_hz: Nominal rate per stream, used to space samples
            inside a frame (the header stamps only the first one). Defaults
            to the config/hardware.yaml forearm-hub rates.
    """

    decoder: ResearchStreamDecoder = field(default_factory=ResearchStreamDecoder)
    sample_counts: dict[str, int] = field(default_factory=dict)
    push_callback: PushCallback | None = None
    clock_model: Any = None
    lsl_clock_fn: Callable[[], float] | None = None
    stream_rates_hz: dict[RawStream, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.stream_rates_hz:
            self.stream_rates_hz = _normative_stream_rates()

    def reset_session(self) -> None:
        """Start a clean session: zero the decoder and this ingestor's counts.

        Called when a recording starts, so a pod reconnect is not read as a
        catastrophic loss (see ResearchStreamDecoder.reset).
        """
        self.decoder.reset()
        self.sample_counts.clear()

    def _push_timestamp(self, pod_timestamp_us: float) -> float:
        """LSL-domain timestamp for one pod microsecond stamp.

        Mapped through the clock model when it is calibrated; otherwise the
        host arrival time, with the mapping gap visible in the sync gate
        rather than hidden in the timestamps.
        """
        if self.clock_model is not None:
            mapped = self.clock_model.to_lsl_seconds(pod_timestamp_us)
            if mapped is not None:
                return float(mapped)
        clock = self.lsl_clock_fn or _default_lsl_clock
        return float(clock())

    def handle_notification(self, char_uuid: str, payload: bytes) -> bool:
        """Decode one notification and push its samples to the matching outlet.

        Returns:
            True when the payload was a valid research-service frame and was
            forwarded. False when it was not, which lets the caller fall back to
            the legacy parser for a firmware that still emits the old format.
        """
        try:
            header, samples = decode_frame(payload)
        except Exception:  # noqa: BLE001 - the legacy parser gets a chance
            self.decoder.frames_rejected += 1
            return False

        outlet = OUTLET_BY_STREAM.get(header.stream)
        if outlet is None or not samples.size:
            return False

        # The decoder owns gap accounting and the 64-bit timeline; route
        # through it so both paths agree, and so the push uses the extended
        # (wrap-corrected) stamp rather than the low-32 header value.
        self.decoder.feed(payload)
        pod_timestamp_us = self.decoder.last_first_us

        channels = _channels_for(header.stream)
        if samples.size % channels:
            logger.warning(
                "%s frame carried %d samples, not a multiple of %d channels",
                header.stream.name,
                samples.size,
                channels,
            )
            return False

        block = samples.reshape(-1, channels)
        if self.push_callback is not None:
            # The header stamps only the frame's first sample, so the rest
            # are spaced at the stream's nominal rate: the device's true
            # inter-sample spacing survives, without BLE arrival jitter.
            rate_hz = float(self.stream_rates_hz.get(header.stream, 0.0) or 0.0)
            divisor = UNIT_DIVISOR.get(header.stream, 1.0)
            for i, row in enumerate(block):
                sample_us = pod_timestamp_us + (i * 1_000_000.0 / rate_hz if rate_hz > 0 else 0.0)
                # Engineering units, the same conversion the decoder applies
                # in arrays(): pushing raw counts would make the LSL streams
                # and every downstream quality metric read two different
                # signals from one wire.
                values = [float(v) / divisor for v in row]
                self.push_callback(outlet, values, self._push_timestamp(sample_us))

        self.sample_counts[header.stream.name.lower()] = (
            self.sample_counts.get(header.stream.name.lower(), 0) + samples.size
        )
        return True

    def drop_summary(self) -> dict[str, int]:
        """Frames lost in transit, per stream."""
        return self.decoder.drop_summary()

    def is_clean(self) -> bool:
        """True when no frames were lost and none were rejected."""
        return self.decoder.is_clean()


def _channels_for(stream: RawStream) -> int:
    from synapse24.hardware.raw_codec import STREAM_CHANNELS

    return STREAM_CHANNELS.get(stream, 1)


def ingest_frame_bytes(payload: bytes) -> tuple[RawStream | None, np.ndarray]:
    """Decode a single frame, returning (stream, samples) or (None, empty)."""
    try:
        header, samples = decode_frame(payload)
    except Exception:  # noqa: BLE001
        return None, np.zeros(0, dtype=np.float64)
    return header.stream, samples
