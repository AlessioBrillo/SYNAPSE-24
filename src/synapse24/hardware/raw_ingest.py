"""Raw-frame ingestion for the live Phase 1 validator.

`validate_phase1_entry.py` historically parsed the legacy Nordic-UART
``sensor_sample_t`` payload. The canonical firmware now streams the Synapse
Research Service frame format, so this module decodes that instead and pushes
the result into the same LSL outlets the rest of the gate reads.

The frame format is versioned and length-checked, so a payload from an
unexpected firmware build is reported rather than silently mis-parsed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from synapse24.hardware.raw_codec import RawStream, decode_frame
from synapse24.hardware.research_client import ResearchStreamDecoder

logger = logging.getLogger(__name__)

# LSL outlet names, matching config/hardware_bringup.yaml.
OUTLET_BY_STREAM = {
    RawStream.ECG: "SYNAPSE_ECG_T0",
    RawStream.PPG: "SYNAPSE_PPG_T0",
    RawStream.ACC: "SYNAPSE_ACC_T0",
    RawStream.GYRO: "SYNAPSE_GYRO_T0",
    RawStream.MAG: "SYNAPSE_MAG_T0",
}

# (outlet name, channel values, timestamp in seconds)
PushCallback = Callable[[str, list[float], float], None]


@dataclass
class RawFrameIngestor:
    """Feeds research-service frames into LSL outlets and counts what arrives."""

    decoder: ResearchStreamDecoder = field(default_factory=ResearchStreamDecoder)
    sample_counts: dict[str, int] = field(default_factory=dict)
    push_callback: PushCallback | None = None

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

        # The decoder owns gap accounting; route through it so both paths agree.
        self.decoder.feed(payload)

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
            for row in block:
                self.push_callback(outlet, [float(v) for v in row], header.timestamp_us / 1e6)

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
