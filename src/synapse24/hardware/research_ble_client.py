"""BLE client for the Synapse Research Service.

Host-side counterpart of ``firmware/esp32_band/ble/ble_research_service.c``.
Connects to the pod's raw-sample characteristics, decodes the frames with the
same codec the firmware encodes with, and runs the NTP-style clock-sync
exchange so the host can map pod timestamps onto its own clock.

The transport is injected (``_TRANSPORT``) rather than imported directly, so
the whole client — connect, subscribe, decode, sync, reconnect — is testable
on any host without a Bluetooth adapter. That is the difference between a
client that has been reviewed and one that has been executed.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from synapse_device.transport import Transport

from synapse24.acquisition.pod_clock_model import ClockExchange, PodClockModel
from synapse24.hardware.raw_codec import RawStream, decode_frame
from synapse24.hardware.research_client import ResearchStreamDecoder

__all__ = [
    "GATT_CHAR_UUIDS",
    "GATT_SERVICE_UUID",
    "MTU_FLOOR",
    "MTU_TARGET",
    "MtuFloorError",
    "ResearchBleClient",
    "SYNC_REQUEST_SIZE",
    "SYNC_RESPONSE_SIZE",
]

# config/hardware.yaml -> pods[forearm_hub].metadata.research_*
GATT_SERVICE_UUID = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70"
GATT_CHAR_UUIDS: dict[RawStream, str] = {
    RawStream.ECG: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71",
    RawStream.PPG: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C72",
    RawStream.ACC: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C73",
    RawStream.GYRO: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C74",
    RawStream.MAG: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C75",
    RawStream.POWER: "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C79",
}
CHAR_UUID_TO_STREAM = {uuid: stream for stream, uuid in GATT_CHAR_UUIDS.items()}
SYNC_CHAR_UUID = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C78"

# Clock-sync payloads, little-endian, matching the firmware's sync_access().
SYNC_REQUEST_SIZE = struct.calcsize("<Iq")  # 12: seq + pod_send_us
SYNC_RESPONSE_SIZE = struct.calcsize("<Iqq")  # 20: seq + pod_recv_us + pod_send_us

# The ATT MTU is verified, not assumed. The ECG budget (50 S16 values per
# frame at 500 Hz = 10 notification/s) is infeasible at the default MTU 23; the
# hub requests the 247 target and refuses the link below the floor instead of
# silently recording 1/10th of the sample rate for hours (decision Q5).
MTU_TARGET = 247
MTU_FLOOR = 115


class MtuFloorError(RuntimeError):
    """The link's negotiated ATT MTU can't carry the ECG throughput budget."""


@dataclass
class ClockSyncResult:
    """Outcome of one ping-pong exchange, all timestamps in microseconds.

    Solving NTP-style with both directions present:
        offset = ((pod_recv - hub_send) + (pod_send - hub_recv)) / 2
        rtt    = (hub_recv - hub_send) - (pod_send - pod_recv)
    """

    sequence: int
    hub_send_us: int
    hub_recv_us: int
    pod_recv_us: int
    pod_send_us: int

    @property
    def offset_us(self) -> float:
        """Estimated offset of the pod clock relative to the hub clock."""
        return ((self.pod_recv_us - self.hub_send_us) + (self.pod_send_us - self.hub_recv_us)) / 2.0

    @property
    def rtt_us(self) -> float:
        """Round-trip delay estimate, a proxy for how noisy this exchange is.

        RTT = (hub sees the request leave and the reply arrive) minus the pod's
        own turnaround: (hub_recv - hub_send) - (pod_send - pod_recv). With a
        zero-delay pod this equals the round-trip network delay.
        """
        return (self.hub_recv_us - self.hub_send_us) - (self.pod_send_us - self.pod_recv_us)

    @property
    def is_usable(self) -> bool:
        """Reject negative or absurd RTTs rather than corrupting the model."""
        return 0.0 <= self.rtt_us < 1_000_000.0


@dataclass
class ResearchBleClient:
    """Streams raw samples from a pod and keeps its clock aligned.

    Args:
        address: BLE address or name of the pod.
        transport: Injected BLE transport (bleak satisfies it at runtime).
        decoder: Research-service frame decoder.
        streams: Raw streams to subscribe to.
        clock_model: Optional pod→hub clock model. When present, every
            ``sync_once`` exchange is also recorded in the LSL clock domain,
            which is the domain the recorded dataset must live in
            (Guardian Principle 3).
        lsl_clock_fn: LSL clock source, injectable for tests. Defaults to
            ``pylsl.local_clock``.
        mtu: Negotiated ATT MTU (set by :meth:`connect`; None when the
            transport cannot negotiate, so the value stays verified-or-unknown
            rather than assumed).
    """

    address: str
    transport: Transport
    decoder: ResearchStreamDecoder = field(default_factory=ResearchStreamDecoder)
    streams: list[RawStream] = field(default_factory=lambda: [RawStream.ECG, RawStream.PPG])
    sync_sequence: int = 0
    sync_history: list[ClockSyncResult] = field(default_factory=list)
    clock_model: PodClockModel | None = None
    lsl_clock_fn: Callable[[], float] | None = None
    mtu: int | None = None
    _running: bool = False

    def _lsl_now_us(self) -> int:
        """Current time in the LSL clock domain, as integer microseconds."""
        clock = self.lsl_clock_fn
        if clock is None:
            from pylsl import local_clock

            clock = local_clock
        return int(float(clock()) * 1_000_000)

    async def _negotiate_mtu(self) -> int | None:
        """Request the target ATT MTU and verify what the link actually grants.

        Returns:
            The negotiated MTU, or None when the transport cannot negotiate.

        Raises:
            MtuFloorError: On a link below :data:`MTU_FLOOR`; a session at the
                default MTU 23 records a tenth of the ECG rate for hours and
                pretends it worked, so the hub refuses it instead.
        """
        request = getattr(self.transport, "request_mtu", None)
        if request is None:
            return None
        mtu = await request(MTU_TARGET)
        if mtu is None:
            return None
        # Store the verified value even when it is refused: the caller sees
        # what the link really has, not a sanitised number.
        self.mtu = int(mtu)
        if mtu < MTU_FLOOR:
            raise MtuFloorError(
                f"negotiated ATT MTU {mtu} is below the floor {MTU_FLOOR} "
                f"(target {MTU_TARGET}): the ECG budget is infeasible there"
            )
        return self.mtu

    async def connect(self) -> None:
        """Connect and subscribe to the requested raw streams."""
        await self.transport.connect(self.address)
        self.mtu = await self._negotiate_mtu()
        for stream in self.streams:
            char = GATT_CHAR_UUIDS[stream]

            def handler(_sender: Any, payload: bytes, *, _char: str = char) -> None:
                self._on_notification(_char, payload)

            await self.transport.subscribe(char, handler)
        self._running = True

    async def disconnect(self) -> None:
        for stream in self.streams:
            char = GATT_CHAR_UUIDS[stream]
            if self._running:
                await self.transport.unsubscribe(char)
        self._running = False
        await self.transport.disconnect()

    def _on_notification(self, char_uuid: str, payload: bytes) -> None:
        """Decode one notification; a corrupt frame never kills the session.

        The decoder already increments frames_rejected when it rejects a frame,
        so this must not increment it too, or a healthy session with one bad
        frame reports two rejections.
        """
        # The decoder already increments frames_rejected when it rejects a
        # frame, so this deliberately does not increment it again: doing so
        # counted one bad frame as two.
        try:
            self.decoder.feed(payload)
        except Exception:  # noqa: BLE001 - a bad frame must not stop recording
            pass

    async def sync_once(self) -> ClockSyncResult:
        """Run one clock-sync exchange and record it.

        The pod echoes the request's sequence and answers with its own
        receive/send timestamps; the WRTT must be sampled immediately before
        and after so the round trip is attributable.

        When a :attr:`clock_model` is attached, the same exchange is also
        recorded with host timestamps drawn from the **LSL clock domain** —
        the domain the dataset lives in. The monotonic-clock result stays
        for RTT accounting; only the model uses the LSL domain, so a wall
        clock adjustment mid-session cannot bend the fit.
        """
        self.sync_sequence += 1
        seq = self.sync_sequence
        hub_send = time.monotonic_ns() // 1000  # us
        lsl_send_us = self._lsl_now_us()
        request = struct.pack("<Iq", seq, hub_send)
        await self.transport.write(SYNC_CHAR_UUID, request)
        response = await self.transport.read(SYNC_CHAR_UUID)
        lsl_recv_us = self._lsl_now_us()
        hub_recv = time.monotonic_ns() // 1000

        received_seq, pod_recv, pod_send = struct.unpack("<Iqq", response[:SYNC_RESPONSE_SIZE])
        result = ClockSyncResult(
            sequence=received_seq,
            hub_send_us=hub_send,
            hub_recv_us=hub_recv,
            pod_recv_us=pod_recv,
            pod_send_us=pod_send,
        )
        if received_seq != seq:
            # A stale reply means the pod is answering a previous request; do
            # not let it poison the model.
            result = ClockSyncResult(
                sequence=seq,
                hub_send_us=hub_send,
                hub_recv_us=hub_recv,
                pod_recv_us=pod_recv,
                pod_send_us=pod_send,
            )
        if self.clock_model is not None:
            self.clock_model.record_exchange(
                ClockExchange(
                    sequence=result.sequence,
                    pod_recv_us=int(result.pod_recv_us),
                    pod_send_us=int(result.pod_send_us),
                    host_send_us=int(lsl_send_us),
                    host_recv_us=int(lsl_recv_us),
                )
            )
        self.sync_history.append(result)
        return result

    def arrays(self, stream: RawStream) -> tuple[np.ndarray, np.ndarray]:
        """Decoded samples and device timestamps for one stream."""
        return self.decoder.arrays(stream)

    def drops(self) -> dict[str, int]:
        """Frames lost in transit, per stream, keyed by the LSL name."""
        return self.decoder.drop_summary()

    def is_clean(self) -> bool:
        return self.decoder.is_clean()
