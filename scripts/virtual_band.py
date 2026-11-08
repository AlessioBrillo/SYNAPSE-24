#!/usr/bin/env python3
"""A virtual Synapse band: a BLE peripheral that speaks the consumer GATT contract.

The band's behaviour lives in ``virtual_band_core.py`` (pure, tested in the gate); this file is the
radio: it serves the GATT table through the OS and forwards phone events to a ``BandSession``.

    uv run python scripts/virtual_band.py --print-table
    uv run python scripts/virtual_band.py --probe                      # static values, nRF Connect
    uv run python scripts/virtual_band.py --scenario clean --log band.jsonl

Scenarios: ``clean``, ``disconnect`` (link drops at 20 s, back after 5 s), ``restart`` (raw
sequences start over at 30 s), ``cp_error`` (the first ``set_time`` is refused).

Windows only for now: it drives the OS GATT server (``winrt``, already installed by bleak). Not
``bless``: on Windows it needs two uninstallable packages and writes HKLM to rename the adapter.
Manual tool, not part of the CI gate; the radio imports live inside :class:`WinRtPeripheral`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Callable
from functools import reduce
from operator import or_
from typing import Any

from virtual_band_core import (
    FIXTURES,
    NOTIFY,
    READ,
    SCENARIOS,
    WRITE,
    BandSession,
    Char,
    build_table,
)

POLL_S = 0.02  # ponytail: asyncio.sleep on Windows jitters ~15 ms; fine for 1 Hz and framed raw


def by_name(table: list[Char], name: str) -> str:
    """The UUID of the characteristic called ``name``."""
    return next(c.uuid for c in table if c.name == name)


def first_record(path: Any) -> bytes:
    """The first frame of a phone sidecar (``u16 length | i64 t_rx_ns | frame``)."""
    data = path.read_bytes()
    n = int.from_bytes(data[:2], "little")
    return bytes(data[10 : 10 + n])


class WinRtPeripheral:
    """The GATT table served through Windows' ``GattServiceProvider`` (no bless: see module doc).

    With a ``session`` the phone's events drive the band's behaviour; without one it only logs
    (the probe). Every handler is hopped onto the asyncio loop before touching the session.
    """

    def __init__(
        self,
        table: list[Char],
        loop: asyncio.AbstractEventLoop,
        log: Callable[[dict[str, Any]], None],
        session: BandSession | None = None,
    ) -> None:
        # the winrt imports are here, not at module level: the core needs no radio
        from winrt.windows.devices.bluetooth import BluetoothError
        from winrt.windows.devices.bluetooth import genericattributeprofile as gatt
        from winrt.windows.storage.streams import DataReader, DataWriter

        self._ok = BluetoothError.SUCCESS
        self._gatt, self._reader, self._writer = gatt, DataReader, DataWriter
        self._loop, self._log, self.session = loop, log, session
        self.table = table
        self.by_uuid = {c.uuid: c for c in table}
        self.values = {c.uuid: c.value for c in table}
        self._control = by_name(table, "control_point")
        self._live: dict[str, Any] = {}
        self._providers: list[tuple[Any, Any]] = []  # (provider, advertising parameters)
        self._links: list[Any] = []  # GattSession of every phone seen
        self._subscribed: dict[str, int] = {}

    def now_ms(self) -> int:
        return int(self._loop.time() * 1000)

    def _buffer(self, data: bytes) -> Any:
        w = self._writer()
        w.write_bytes(bytearray(data))
        return w.detach_buffer()

    def _bytes(self, buf: Any) -> bytes:
        out = bytearray(buf.length)
        self._reader.from_buffer(buf).read_bytes(out)
        return bytes(out)

    def _touch(self, link: Any) -> None:
        """Any request from a phone means it is connected."""
        if link not in self._links:
            self._links.append(link)
        if self.session:
            self.session.connect(self.now_ms())

    def _on_read(self, sender: Any, args: Any) -> None:
        deferral = args.get_deferral()
        key = str(sender.uuid).lower()

        async def go() -> None:
            req = await args.get_request_async()
            self._touch(args.session)
            self._log(
                {"event": "read", "char": self.by_uuid[key].name, "mtu": args.session.max_pdu_size}
            )
            req.respond_with_value(self._buffer(self.values[key]))
            deferral.complete()

        asyncio.run_coroutine_threadsafe(go(), self._loop)

    def _on_write(self, sender: Any, args: Any) -> None:
        deferral = args.get_deferral()
        key = str(sender.uuid).lower()

        async def go() -> None:
            req = await args.get_request_async()
            self._touch(args.session)
            value = self._bytes(req.value)
            error = 0
            if self.session and key == self._control:
                error = self.session.write_control(self.now_ms(), value)
            else:
                self._log({"event": "write", "char": self.by_uuid[key].name, "hex": value.hex()})
            if req.option == self._gatt.GattWriteOption.WRITE_WITH_RESPONSE:
                if error:
                    req.respond_with_protocol_error(0x80 + error)  # as ctl_access in the firmware
                else:
                    req.respond()
            deferral.complete()

        asyncio.run_coroutine_threadsafe(go(), self._loop)

    def _on_subscribers(self, sender: Any, _: Any) -> None:
        key = str(sender.uuid).lower()
        clients = list(sender.subscribed_clients)
        links = [c.session for c in clients]
        mtu = [s.max_pdu_size for s in links]
        self._loop.call_soon_threadsafe(self._subscribers_changed, key, len(clients), links, mtu)

    def _subscribers_changed(self, key: str, count: int, links: list[Any], mtu: list[int]) -> None:
        self._subscribed[key] = count
        name = self.by_uuid[key].name
        if not self.session:
            self._log({"event": "subscribers", "char": name, "count": count, "mtu": mtu})
            return
        if count:
            for link in links:
                self._touch(link)
        self.session.subscribe(self.now_ms(), key, count > 0)
        if count:
            self._log({"event": "mtu", "char": name, "mtu": mtu})
        elif not any(self._subscribed.values()):
            self.session.disconnect(self.now_ms())  # Windows drops the subscriptions with the link

    async def start(self) -> None:
        """Create every service and characteristic, then advertise the consumer service."""
        g = self._gatt
        flags = {
            READ: g.GattCharacteristicProperties.READ,
            WRITE: g.GattCharacteristicProperties.WRITE,
            NOTIFY: g.GattCharacteristicProperties.NOTIFY,
        }
        consumer_service = self.table[0].service
        for svc in dict.fromkeys(c.service for c in self.table):
            made = await g.GattServiceProvider.create_async(uuid.UUID(svc))
            if made.error != self._ok:
                # Windows reserves some standard services (0x180A is DisabledByPolicy)
                self._log({"event": "service_skipped", "service": svc, "error": int(made.error)})
                if svc == consumer_service:
                    raise SystemExit(f"cannot create the consumer service: {made.error}")
                continue
            provider = made.service_provider
            for c in (x for x in self.table if x.service == svc):
                params = g.GattLocalCharacteristicParameters()
                params.characteristic_properties = reduce(or_, (flags[p] for p in c.props))
                res = await provider.service.create_characteristic_async(uuid.UUID(c.uuid), params)
                if res.error != self._ok:
                    raise SystemExit(f"cannot create characteristic {c.name}: {res.error}")
                char = res.characteristic
                char.add_read_requested(self._on_read)
                char.add_write_requested(self._on_write)
                char.add_subscribed_clients_changed(self._on_subscribers)
                self._live[c.uuid] = char
            adv = g.GattServiceProviderAdvertisingParameters()
            adv.is_connectable = True
            adv.is_discoverable = svc == consumer_service  # only the consumer service is advertised
            provider.add_advertisement_status_changed(
                lambda s, _, svc=svc: self._log(
                    {"event": "adv_status", "service": svc, "status": int(s.advertisement_status)}
                )
            )
            provider.start_advertising(adv)
            self._providers.append((provider, adv))
        self._log({"event": "advertising", "consumer_service": consumer_service})

    async def notify(self, uuid_s: str, data: bytes) -> bool:
        """Push a value to the subscribed phone; False when it did not go out."""
        char = self._live.get(uuid_s)
        if char is None or not char.subscribed_clients.size:
            return False
        try:
            await char.notify_value_async(self._buffer(data))
        except OSError as exc:
            self._log(
                {"event": "notify_failed", "char": self.by_uuid[uuid_s].name, "error": str(exc)}
            )
            return False
        return True

    def drop_link(self) -> None:
        """Disconnect the phone.

        ponytail: WinRT has no server-side disconnect; closing the sessions and withdrawing the
        advertisement is the best it offers. Whether a given phone really drops is what the PR-4
        run on the radio shows.
        """
        for link in self._links:
            try:
                link.maintain_connection = False
                link.close()
            except Exception as exc:  # WinRT raises plain exceptions of many types
                self._log({"event": "drop_link_failed", "error": str(exc)})
        for provider, _ in self._providers:
            provider.stop_advertising()
        self._links.clear()

    def readvertise(self) -> None:
        for provider, adv in self._providers:
            provider.start_advertising(adv)

    def stop(self) -> None:
        for provider, _ in self._providers:
            provider.stop_advertising()


def _require_windows() -> None:
    if sys.platform != "win32":
        raise SystemExit("the virtual band uses the Windows GATT server (winrt); no other host yet")


async def run_probe(log: Callable[[dict[str, Any]], None]) -> None:
    """Serve the table with a static feature frame until interrupted."""
    _require_windows()
    band = WinRtPeripheral(build_table(), asyncio.get_running_loop(), log)
    feature = by_name(band.table, "feature")
    band.values[feature] = first_record(FIXTURES / "session_60s.synapse.bin")
    await band.start()
    try:
        while True:
            await asyncio.sleep(1)
            await band.notify(feature, band.values[feature])
    finally:
        band.stop()


async def run_band(scenario: str, log: Callable[[dict[str, Any]], None]) -> None:
    """Play ``scenario`` to whoever connects, until interrupted."""
    _require_windows()
    session = BandSession(scenario, log=log)
    band = WinRtPeripheral(build_table(), asyncio.get_running_loop(), log, session)
    await band.start()
    try:
        while True:
            await asyncio.sleep(POLL_S)
            events, actions = session.poll(band.now_ms())
            for ev in events:
                if not await band.notify(ev.char, ev.data):
                    log({"event": "not_sent", "char": band.by_uuid[ev.char].name})
            for action in actions:
                getattr(band, action)()
    finally:
        band.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--probe", action="store_true", help="advertise the table with static values")
    ap.add_argument("--scenario", choices=SCENARIOS, help="replay the fixtures as this scenario")
    ap.add_argument("--log", help="also write every event to this JSONL file")
    ap.add_argument("--print-table", action="store_true", help="print the GATT table and exit")
    args = ap.parse_args(argv)
    if args.print_table:
        for c in build_table():
            print(f"{c.service}  {c.uuid}  {'/'.join(c.props):<12} {c.name}")
        return 0
    if not (args.probe or args.scenario):
        ap.error("give --probe, --scenario or --print-table")
    sink = open(args.log, "w", encoding="utf-8") if args.log else None  # noqa: SIM115

    def log(record: dict[str, Any]) -> None:
        line = json.dumps(record)
        print(line, flush=True)
        if sink:
            sink.write(line + "\n")
            sink.flush()

    try:
        asyncio.run(run_probe(log) if args.probe else run_band(args.scenario, log))
    except KeyboardInterrupt:
        pass
    finally:
        if sink:
            sink.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
