# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""BLE transport for the Synapse Band.

The :class:`Transport` protocol is the surface the client needs. At runtime
:class:`BleakTransport` satisfies it via bleak; in tests a
:class:`SyntheticTransport` speaks the real frame format without any Bluetooth
adapter, so connect / subscribe / decode / clock-sync / reconnect are all
exercisable offline.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import Callable
from typing import Protocol, runtime_checkable

# GATT v1 contract, from config/hardware.yaml (frozen).
SERVICE_UUID = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70"
CHAR_ECG = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71"
CHAR_PPG = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C72"
CHAR_ACC = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C73"
CHAR_GYRO = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C74"
CHAR_MAG = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C75"
CHAR_POWER = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C79"
CHAR_SYNC = "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C78"

# Stream id → characteristic UUID (raw research service).
CHAR_UUID_TO_STREAM: dict[str, int] = {
    CHAR_ECG: 0x01,
    CHAR_PPG: 0x02,
    CHAR_ACC: 0x03,
    CHAR_GYRO: 0x04,
    CHAR_MAG: 0x05,
    CHAR_POWER: 0x07,
}

SYNC_REQUEST_SIZE = struct.calcsize("<Iq")  # seq + pod_send_us
SYNC_RESPONSE_SIZE = struct.calcsize("<Iqq")  # seq + pod_recv_us + pod_send_us

NotificationHandler = Callable[[str, bytes], None]

# The ATT MTU the hub asks for (config/hardware.yaml bringup.ble.mtu_target).
# The granted value is ALWAYS the link's answer, never this constant.
MTU_TARGET = 247


@runtime_checkable
class Transport(Protocol):
    """Minimal BLE surface the client needs."""

    async def connect(self, address: str) -> None: ...
    async def disconnect(self) -> None: ...
    async def subscribe(self, char_uuid: str, handler: NotificationHandler) -> None: ...
    async def unsubscribe(self, char_uuid: str) -> None: ...
    async def write(self, char_uuid: str, data: bytes, response: bool = True) -> None: ...
    async def read(self, char_uuid: str) -> bytes: ...

    async def request_mtu(self, mtu: int) -> int | None:
        """Ask the link for ``mtu`` ATT bytes and report what it granted.

        Returns:
            The granted MTU, or None when the backend cannot negotiate (the
            caller keeps an unknown MTU instead of an assumed one).

        Raises:
            TransportError: when called while not connected.

        """
        ...


class TransportError(RuntimeError):
    """Raised when a transport operation fails."""


class SyntheticTransport(Transport):
    """In-process fake pod: speaks the real frame format, injectable faults.

    Used by tests and by the ``synapse record --source synthetic`` command so
    every code path runs without hardware.
    """

    def __init__(self, address: str = "SYNTHETIC") -> None:
        self.address = address
        self.connected = False
        self.mtu: int | None = None
        # The granted MTU the simulated link reports on request_mtu().
        self.granted_mtu: int | None = MTU_TARGET
        self._subscribed: dict[str, NotificationHandler] = {}
        self._notify_queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None

    async def connect(self, address: str) -> None:
        await asyncio.sleep(0.01)  # emulate scan/connect latency
        self.connected = True
        self.address = address

    async def disconnect(self) -> None:
        self.connected = False
        self._subscribed.clear()
        self._running = False
        if self._task is not None:
            self._task.cancel()

    async def subscribe(self, char_uuid: str, handler: NotificationHandler) -> None:
        if not self.connected:
            raise TransportError("not connected")
        self._subscribed[char_uuid] = handler

    async def unsubscribe(self, char_uuid: str) -> None:
        self._subscribed.pop(char_uuid, None)

    async def write(self, char_uuid: str, data: bytes, response: bool = True) -> None:
        if not self.connected:
            raise TransportError("not connected")

    async def read(self, char_uuid: str) -> bytes:
        if not self.connected:
            raise TransportError("not connected")
        return b""

    async def request_mtu(self, mtu: int) -> int | None:
        if not self.connected:
            raise TransportError("not connected")
        # The synthetic link grants whatever it is told (granted_mtu=None
        # models a backend that cannot negotiate: the MTU stays unknown).
        if self.granted_mtu is None:
            return None
        self.mtu = int(self.granted_mtu)
        return self.mtu

    # --- synthetic pod behaviour ------------------------------------------

    async def start(self, streams: list[str], rate_hz: int = 50, duration_s: float = 10.0) -> None:
        """Begin emitting synthetic frames on ``streams`` (test helper)."""
        self._running = True
        self._task = asyncio.create_task(self._emit(streams, rate_hz, duration_s))

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _emit(self, streams: list[str], rate_hz: int, duration_s: float) -> None:
        period = 1.0 / rate_hz
        total = int(duration_s * rate_hz)
        seq = 0
        ts = 0
        import numpy as np  # local import: codec is optional at import time

        from .codec import RawFormat, RawStream, encode_frame

        # CHAR_UUID_TO_STREAM maps uuid string -> stream id int
        for _ in range(total):
            if not self._running:
                break
            for uuid in streams:
                stream_id = CHAR_UUID_TO_STREAM.get(uuid)
                if stream_id is None:
                    continue
                stream = RawStream(stream_id)
                channels = {0x01: 1, 0x02: 2, 0x03: 3, 0x04: 3, 0x05: 3, 0x06: 1, 0x07: 4}.get(
                    stream_id, 1
                )
                samples = np.zeros((1, channels), dtype=np.float64)
                frame = encode_frame(stream, seq, ts, 0, RawFormat.S16, samples)
                handler = self._subscribed.get(uuid)
                if handler is not None:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(uuid, frame)
                    else:
                        handler(uuid, frame)
                seq += 1
            ts += int(period * 1e6)
            await asyncio.sleep(period)


try:  # pragma: no cover - bleak optional at import time
    from bleak import BleakClient  # type: ignore

    class BleakTransport(Transport):
        """Real BLE transport via bleak."""

        def __init__(self) -> None:
            self._client: BleakClient | None = None
            self.mtu: int | None = None

        async def connect(self, address: str) -> None:
            self._client = BleakClient(address)
            await self._client.connect()
            if not self._client.is_connected:
                raise TransportError(f"could not connect to {address}")

        async def disconnect(self) -> None:
            if self._client is not None and self._client.is_connected:
                await self._client.disconnect()
            self._client = None

        async def subscribe(self, char_uuid: str, handler: NotificationHandler) -> None:
            if self._client is None:
                raise TransportError("not connected")

            def _cb(_characteristic: object, data: bytearray) -> None:
                if asyncio.iscoroutinefunction(handler):
                    asyncio.create_task(handler(char_uuid, bytes(data)))
                else:
                    handler(char_uuid, bytes(data))

            await self._client.start_notify(char_uuid, _cb)

        async def unsubscribe(self, char_uuid: str) -> None:
            if self._client is not None:
                await self._client.stop_notify(char_uuid)

        async def write(self, char_uuid: str, data: bytes, response: bool = True) -> None:
            if self._client is None:
                raise TransportError("not connected")
            await self._client.write_gatt_char(char_uuid, data, response=response)

        async def read(self, char_uuid: str) -> bytes:
            if self._client is None:
                raise TransportError("not connected")
            return await self._client.read_gatt_char(char_uuid)

        async def request_mtu(self, mtu: int) -> int | None:
            """Negotiate the ATT MTU and report the link's answer.

            bleak exposes ``set_mtu_size`` (Windows/WinRT backend); where the
            backend cannot negotiate the real MTU stays UNKNOWN (None) —
            never the requested number dressed up as a fact.
            """
            if self._client is None:
                raise TransportError("not connected")
            setter = getattr(self._client, "set_mtu_size", None)
            if setter is None:
                return None
            granted = await setter(mtu)
            try:
                self.mtu = int(granted)
            except (TypeError, ValueError):
                self.mtu = None
            return self.mtu


except ImportError:  # pragma: no cover - bleak not installed

    class BleakTransport(Transport):  # type: ignore[no-redef]
        """Fallback when bleak is unavailable: every operation raises."""

        async def connect(self, address: str) -> None:
            raise TransportError("bleak not installed: `pip install synapse-device[ble]`")

        async def disconnect(self) -> None: ...
        async def subscribe(self, char_uuid: str, handler: NotificationHandler) -> None:
            raise TransportError("bleak not installed")

        async def unsubscribe(self, char_uuid: str) -> None: ...
        async def write(self, char_uuid: str, data: bytes, response: bool = True) -> None:
            raise TransportError("bleak not installed")

        async def read(self, char_uuid: str) -> bytes:
            raise TransportError("bleak not installed")

        async def request_mtu(self, mtu: int) -> int | None:
            raise TransportError("bleak not installed: `pip install synapse-device[ble]`")


def build_transport(address: str) -> Transport:
    """Build the runtime transport for ``address`` (the single factory seam).

    ``SYNTHETIC`` selects the in-process pod that speaks the real frame
    format; any other value builds the real BLE bridge (which raises a
    clear ``TransportError`` on use when bleak is not installed). The CLI's
    ``info``/``record`` commands and the recorder share this one seam, so
    tests can inject a fake by replacing one callable.
    """
    if address.upper() == "SYNTHETIC":
        return SyntheticTransport(address)
    return BleakTransport()


async def discover(name_filter: str = "SYNAPSE", timeout_s: float = 10.0) -> str | None:
    """Scan for a SYnapse pod and return its address, or None.

    The single place that touches the BLE scanner: every client goes through
    this helper, so the bleak import surface stays inside this module.
    """
    try:
        from bleak import BleakScanner
    except ImportError:
        raise TransportError("bleak not installed: `pip install synapse-device[ble]`") from None

    devices = await BleakScanner.discover(timeout=timeout_s)
    wanted = name_filter.upper()
    for device in devices:
        if (device.name or "").upper().find(wanted) >= 0:
            address = device.address
            if isinstance(address, str):
                return address
    return None
