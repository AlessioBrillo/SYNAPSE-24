"""Tests for the Synapse Research Service BLE client.

The transport is injected, so the whole client — connect, subscribe, decode,
clock sync, gap detection — runs without a Bluetooth adapter. That is the point:
a client reviewed by eye is not a client that has been executed.

Async is driven with ``asyncio.run`` rather than pytest-asyncio, to avoid adding
a plugin whose only job would be to launch a coroutine.
"""

from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field

import numpy as np
import pytest

from synapse24.acquisition.pod_clock_model import PodClockModel
from synapse24.hardware.raw_codec import RawFormat, RawStream, encode_frame
from synapse24.hardware.research_ble_client import (
    GATT_CHAR_UUIDS,
    GATT_SERVICE_UUID,
    SYNC_REQUEST_SIZE,
    ClockSyncResult,
    ResearchBleClient,
)
from synapse24.hardware.research_client import ResearchStreamDecoder
from tests.test_research_service_hil import SyntheticPod  # noqa: E402


def _run(coro):
    """Drive one coroutine to completion."""
    return asyncio.run(coro)


@dataclass
class FakeTransport:
    """Records writes and replays a scripted notification stream."""

    notifications: dict[str, list[bytes]] = field(default_factory=dict)
    connected: bool = False
    subscribed: list[str] = field(default_factory=list)
    writes: list[tuple[str, bytes]] = field(default_factory=list)
    sync_reply: bytes = b""

    async def connect(self, address: str) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def start_notify(self, char_uuid: str, callback) -> None:  # noqa: ANN001
        self.subscribed.append(char_uuid)
        # Deliver the scripted notifications immediately, in order.
        for payload in self.notifications.get(char_uuid, []):
            callback(self, bytearray(payload))

    async def stop_notify(self, char_uuid: str) -> None:
        if char_uuid in self.subscribed:
            self.subscribed.remove(char_uuid)

    async def write_gatt_char(self, char_uuid: str, data: bytes) -> None:
        self.writes.append((char_uuid, bytes(data)))

    async def read_gatt_char(self, char_uuid: str) -> bytes:
        return self.sync_reply


def _pod_frames(duration_s: float = 1.0) -> dict[str, list[bytes]]:
    """Build per-characteristic notification lists from the synthetic pod."""
    pod = SyntheticPod(duration_s=duration_s, seed=4)
    by_stream: dict[RawStream, list[bytes]] = {}
    for stream, frame in pod.frames():
        by_stream.setdefault(stream, []).append(frame)
    return {uuid: by_stream.get(stream, []) for stream, uuid in GATT_CHAR_UUIDS.items()}


def test_uuids_match_the_manifest() -> None:
    """The client must address the same service the firmware registers."""
    assert GATT_SERVICE_UUID == "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70"
    assert GATT_CHAR_UUIDS[RawStream.ECG].endswith("4C71")
    assert GATT_CHAR_UUIDS[RawStream.PPG].endswith("4C72")
    assert len(set(GATT_CHAR_UUIDS.values())) == len(GATT_CHAR_UUIDS), "UUIDs must be unique"


def test_connect_decodes_a_full_session() -> None:
    frames = _pod_frames(1.0)
    transport = FakeTransport(notifications=frames)
    client = ResearchBleClient(address="SYNAPSE-BAND", transport=transport)

    _run(client.connect())
    assert transport.connected
    assert set(transport.subscribed) == {GATT_CHAR_UUIDS[s] for s in (RawStream.ECG, RawStream.PPG)}

    ecg, ecg_ts = client.arrays(RawStream.ECG)
    assert ecg.size == 500  # 1.0 s at 500 Hz
    assert np.all(np.diff(ecg_ts) > 0)

    ppg, ppg_ts = client.arrays(RawStream.PPG)
    assert ppg.size == 2 * 50  # red + IR
    assert np.all(np.diff(ppg_ts) > 0)

    assert client.is_clean(), f"unexpected drops: {client.drops()}"


def test_disconnect_unsubscribes_everything() -> None:
    frames = _pod_frames(0.2)
    transport = FakeTransport(notifications=frames)
    client = ResearchBleClient(address="SYNAPSE-BAND", transport=transport)

    _run(client.connect())
    assert transport.subscribed
    _run(client.disconnect())
    assert transport.subscribed == []
    assert not transport.connected


def test_corrupt_frame_does_not_kill_the_session() -> None:
    first = encode_frame(RawStream.ECG, 0, 1000, 0, RawFormat.S16, [100, 200, 300])
    bad = bytes([0x7F]) + first[1:]
    third = encode_frame(RawStream.ECG, 1, 2000, 0, RawFormat.S16, [400, 500, 600])
    transport = FakeTransport(notifications={GATT_CHAR_UUIDS[RawStream.ECG]: [first, bad, third]})
    client = ResearchBleClient(address="SYNAPSE-BAND", transport=transport, streams=[RawStream.ECG])

    _run(client.connect())
    samples, _ = client.arrays(RawStream.ECG)
    # The research service quantises ECG to microvolts, so wire values arrive
    # divided by 1000 (see UNIT_DIVISOR in research_client.py).
    assert samples.tolist() == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    assert client.decoder.frames_rejected == 1


def _exchange(hub_send: int, hub_recv: int, pod_recv: int, pod_send: int) -> ClockSyncResult:
    return ClockSyncResult(1, hub_send, hub_recv, pod_recv, pod_send)


def test_clock_sync_zero_offset_is_recovered() -> None:
    """Identical clocks: the offset must vanish and the RTT be positive."""
    # 80 us one-way delay, 80 us pod turnaround, no clock offset.
    result = _exchange(hub_send=0, hub_recv=240, pod_recv=80, pod_send=160)
    assert result.offset_us == 0.0
    assert result.rtt_us == 160.0
    assert result.is_usable


def test_clock_sync_offset_is_recovered() -> None:
    """The offset is recovered whether the pod runs ahead or behind."""
    ahead = _exchange(
        hub_send=0,
        hub_recv=240,
        pod_recv=80 + 1500,
        pod_send=160 + 1500,
    )
    assert ahead.offset_us == 1500.0
    assert ahead.rtt_us == 160.0, "the offset must not leak into the RTT"

    behind = _exchange(
        hub_send=0,
        hub_recv=240,
        pod_recv=80 - 1500,
        pod_send=160 - 1500,
    )
    assert behind.offset_us == -1500.0
    assert behind.rtt_us == 160.0


def test_clock_sync_rtt_grows_with_delay() -> None:
    """A slower link shows up as a larger RTT.

    Asymmetric delay also biases the offset: that is the known NTP limitation,
    not a bug. The offset averages the two one-way estimates, so unequal delays
    leave a residual. Documented here so nobody later "fixes" it by redefining
    the offset.
    """
    # 100 us each way, 80 us of pod turnaround.
    result = _exchange(hub_send=0, hub_recv=400, pod_recv=100, pod_send=180)
    assert result.rtt_us == 320.0
    # The 40 us of asymmetry (100 out vs 80 turn) surfaces as apparent offset.
    assert result.offset_us == -60.0


def test_clock_sync_rejects_implausible_exchanges() -> None:
    """A reply that predates the request must not enter the drift model."""
    # The pod claims to have answered 400 us before the hub had even sent.
    bogus = _exchange(hub_send=1_000_000, hub_recv=1_000_050, pod_recv=999_600, pod_send=999_680)
    assert bogus.rtt_us < 0.0
    assert not bogus.is_usable


def test_sync_once_writes_the_request_and_reads_the_reply() -> None:
    transport = FakeTransport(sync_reply=struct.pack("<Iqq", 1, 5000, 5100))
    client = ResearchBleClient(address="SYNAPSE-BAND", transport=transport, streams=[])

    result = _run(client.sync_once())

    assert transport.writes
    char, payload = transport.writes[0]
    assert char.endswith("4C78"), "the sync exchange must use the SYNC characteristic"
    assert len(payload) == SYNC_REQUEST_SIZE
    assert struct.unpack("<I", payload[:4])[0] == 1

    assert result.sequence == 1
    assert result.pod_recv_us == 5000
    assert result.pod_send_us == 5100
    assert len(client.sync_history) == 1


def test_dropped_frames_are_reported() -> None:
    """A hole in the data must be visible, not smoothed over."""
    frames = []
    for seq in (0, 1, 3, 4):  # sequence 2 is missing: a lost notification
        frames.append(encode_frame(RawStream.ECG, seq, 1000 + seq, 0, RawFormat.S16, [10, 20, 30]))

    transport = FakeTransport(notifications={GATT_CHAR_UUIDS[RawStream.ECG]: frames})
    client = ResearchBleClient(address="SYNAPSE-BAND", transport=transport, streams=[RawStream.ECG])
    _run(client.connect())

    assert not client.is_clean()
    assert client.drops() == {"ECG": 1}


def test_decoder_shared_with_the_host_path() -> None:
    """The client must not carry a second decoder implementation."""
    client = ResearchBleClient(address="x", transport=FakeTransport())
    assert isinstance(client.decoder, ResearchStreamDecoder)


class DriftingPodTransport(FakeTransport):
    """Answers SYNC reads from a pod clock with a known offset and drift.

    The pod's stamps are a deterministic function of the injected LSL clock,
    so the test asserts the estimator recovers ground truth instead of
    merely agreeing with itself. The exchange geometry (radio legs + pod
    turnaround) is explicit, so the RTT is positive and the NTP offset is
    unbiased.
    """

    RADIO_US = 50.0
    TURNAROUND_US = 1500.0

    def __init__(
        self, lsl_now: list[float], offset_ms: float = 12.5, drift_ppm: float = 35.0
    ) -> None:
        super().__init__()
        self.lsl_now = lsl_now
        self.offset_ms = offset_ms
        self.drift_ppm = drift_ppm
        self.t0_s: float | None = None

    def _pod_at(self, host_s: float) -> int:
        """Pod stamp for the host instant `host_s`, offset and drifting."""
        if self.t0_s is None:
            self.t0_s = host_s
        elapsed_s = host_s - self.t0_s
        skew_us = elapsed_s * self.drift_ppm  # ppm in, microseconds out
        return int(host_s * 1_000_000.0 + skew_us + self.offset_ms * 1000.0)

    async def read_gatt_char(self, char_uuid: str) -> bytes:
        # The request reaches the pod one radio leg after the hub sent it;
        # the pod turns around, and the reply arrives one leg later, so the
        # host observes (turnaround + 2 * radio) of elapsed time.
        recv_s = self.lsl_now[0] + self.RADIO_US / 1_000_000.0
        send_s = recv_s + self.TURNAROUND_US / 1_000_000.0
        self.lsl_now[0] = send_s + self.RADIO_US / 1_000_000.0
        pod_recv = self._pod_at(recv_s)
        pod_send = self._pod_at(send_s)
        return struct.pack("<Iqq", self.sync_sequence, pod_recv, pod_send)

    @property
    def sync_sequence(self) -> int:
        return len(self.writes)

    def current_pod_us(self) -> int:
        """The pod's stamp for the current host instant."""
        return self._pod_at(self.lsl_now[0])


def test_sync_exchanges_feed_the_clock_model() -> None:
    """sync_once must record LSL-domain exchanges, not wall-clock ones."""
    lsl_now = [1_000.0]
    transport = DriftingPodTransport(lsl_now=lsl_now)
    model = PodClockModel(pod_id="forearm_hub")
    client = ResearchBleClient(
        address="SYNAPSE-BAND",
        transport=transport,
        streams=[],
        clock_model=model,
        lsl_clock_fn=lambda: lsl_now[0],
    )

    for _i in range(8):
        _run(client.sync_once())
        lsl_now[0] += 30.0

    assert model.is_calibrated
    fit = model.fit()
    assert fit is not None
    assert fit.offset_ms == pytest.approx(12.5, abs=2.0)
    assert fit.drift_ppm == pytest.approx(35.0, abs=15.0)
    # The model lives in the LSL domain: the pod's current stamp maps near
    # the current LSL time, not to a boot-epoch timestamp.
    mapped = model.to_lsl_seconds(transport.current_pod_us())
    assert mapped is not None
    assert mapped == pytest.approx(lsl_now[0], abs=0.05)


def test_sync_exchanges_survive_a_rejected_exchange() -> None:
    """An RTT-spiked exchange is logged and discarded, not fitted."""
    lsl_now = [2_000.0]
    transport = DriftingPodTransport(lsl_now=lsl_now)
    model = PodClockModel(pod_id="forearm_hub")
    client = ResearchBleClient(
        address="SYNAPSE-BAND",
        transport=transport,
        streams=[],
        clock_model=model,
        lsl_clock_fn=lambda: lsl_now[0],
    )
    for _i in range(5):
        _run(client.sync_once())
        lsl_now[0] += 30.0
    clean = model.fit()
    assert clean is not None

    # Freeze the LSL clock backwards: the exchange becomes implausible.
    lsl_now[0] -= 5_000.0
    _run(client.sync_once())
    assert model.fit() == clean, "a rejected exchange must not move the model"
