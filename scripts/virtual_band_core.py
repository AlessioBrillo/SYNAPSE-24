#!/usr/bin/env python3
"""The pure core of the virtual band: the GATT table, the replay program and the band's behaviour.

No radio here (that is ``virtual_band.py``), so everything below runs in the CI gate.

* :func:`build_table` is the GATT table, built from ``contracts/gatt_v2.json`` and
  ``config/hardware.yaml``, never written by hand.
* :func:`load_program` is what the band sends and when: the two phone sidecar fixtures
  (``contracts/fixtures/session_60s.*``) replayed with their original timing, plus the standard
  Heart Rate Measurement derived from each feature frame.
* :class:`BandSession` is the band's behaviour towards one phone: the control point, the raw gate,
  who is subscribed to what, and the scenarios (``clean``, ``disconnect``, ``restart``,
  ``cp_error``). It is driven by a clock and by radio events and returns what to notify, so a test
  can play a whole session in milliseconds.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT / "scripts"))

import make_band_sidecars as sidecars  # noqa: E402
from synapse_device import consumer  # noqa: E402

GATT_V2 = ROOT / "contracts" / "gatt_v2.json"
HARDWARE = ROOT / "config" / "hardware.yaml"
FIXTURES = ROOT / "contracts" / "fixtures"
BASE_UUID = "0000{:04x}-0000-1000-8000-00805f9b34fb"

# Characteristic properties, as strings so the table needs no BLE library to be built and tested.
READ, WRITE, NOTIFY = "read", "write", "notify"

SCENARIOS = ("clean", "disconnect", "restart", "cp_error")
DISCONNECT_AT_MS = 20_000  # `disconnect`: the link drops here...
READVERTISE_AFTER_MS = 5_000  # ...and the band advertises again this long after
RESTART_AT_MS = 30_000  # `restart`: the raw sequences and timestamps start over here
TRANSMITTING = 1 << 2  # the `transmitting` bit of the feature and status flags
RAW_STREAM_NAMES = {1: "ecg", 2: "ppg", 3: "acc", 4: "gyro", 5: "mag", 7: "power"}


@dataclass(frozen=True)
class Char:
    """One characteristic of the table: where it lives, what it allows, what it holds at start."""

    service: str
    uuid: str
    props: tuple[str, ...]
    value: bytes
    name: str


def _find(node: Any, key: str) -> Any:
    """First value of ``key`` anywhere in a nested dict/list (the research UUIDs sit under a pod)."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for child in node.values():
            found = _find(child, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find(child, key)
            if found is not None:
                return found
    return None


def build_table() -> list[Char]:
    """The whole GATT table of the band: consumer v2, research, and the standard services."""
    gatt = json.loads(GATT_V2.read_text(encoding="utf-8"))
    hw = yaml.safe_load(HARDWARE.read_text(encoding="utf-8"))
    consumer_svc = gatt["service"]
    chars = gatt["characteristics"]
    research_svc = str(_find(hw, "research_service_uuid")).lower()
    research = _find(hw, "research_characteristics")
    std = _find(hw, "standard_services")
    info = _find(hw, "device_info_chars")

    def std_uuid(short: str) -> str:
        return BASE_UUID.format(int(short, 16))

    table = [
        Char(consumer_svc, chars["feature"], (READ, NOTIFY), b"", "feature"),
        Char(consumer_svc, chars["status"], (READ, NOTIFY), b"", "status"),
        Char(
            consumer_svc,
            chars["stream_metadata"],
            (READ,),
            consumer_stream_metadata(gatt),
            "stream_metadata",
        ),
        Char(consumer_svc, chars["control_point"], (WRITE,), b"", "control_point"),
    ]
    for name in ("ecg", "ppg", "acc", "gyro", "mag", "power"):
        table.append(Char(research_svc, str(research[name]).lower(), (NOTIFY,), b"", name))
    hr, battery, devinfo = (
        std_uuid(std["heart_rate"]),
        std_uuid(std["battery"]),
        std_uuid(std["device_information"]),
    )
    table += [
        Char(hr, std_uuid("2A37"), (NOTIFY,), b"", "heart_rate_measurement"),
        Char(battery, std_uuid("2A19"), (READ, NOTIFY), bytes([100]), "battery_level"),
        Char(devinfo, std_uuid(info["serial_number"]), (READ,), b"VIRTUAL-0001", "serial_number"),
        Char(devinfo, std_uuid(info["hardware_revision"]), (READ,), b"virtual", "hardware_rev"),
    ]
    return table


def consumer_stream_metadata(gatt: dict[str, Any]) -> bytes:
    """The stream_metadata read value: ``[count u8] + count * 8-byte entry`` from the contract."""
    streams = gatt["stream_metadata"]["streams"]
    blob = bytearray([len(streams)])
    for s in streams:
        blob += bytes([s["stream_id"], s["format"], s["channels"], s["unit_code"]])
        blob += int(s["rate_hz"]).to_bytes(2, "little") + int(s["unit_scale"]).to_bytes(2, "little")
    return bytes(blob)


# --------------------------------------------------------------------------- the replay program


@dataclass(frozen=True)
class Event:
    """One notification of the program: when (band time), on which characteristic, what bytes."""

    t_ms: int
    char: str
    data: bytes


def _restart(events: list[Event], at_ms: int) -> list[Event]:
    """The band reboots at ``at_ms``: every raw stream starts again from sequence 0 and time 0."""
    from synapse_device import codec

    names = {c.uuid: c.name for c in build_table()}
    base: dict[str, tuple[int, int]] = {}
    out: list[Event] = []
    for ev in events:
        data = ev.data
        if (
            ev.t_ms >= at_ms
            and names[ev.char] in RAW_STREAM_NAMES.values()
            and data[0] == codec.FRAME_VERSION
        ):
            seq = int.from_bytes(data[2:4], "little")
            ts = int.from_bytes(data[4:8], "little")
            seq0, ts0 = base.setdefault(ev.char, (seq, ts))
            data = (
                data[:2]
                + ((seq - seq0) & 0xFFFF).to_bytes(2, "little")
                + ((ts - ts0) & 0xFFFFFFFF).to_bytes(4, "little")
                + data[8:]
            )
        out.append(Event(ev.t_ms, ev.char, data))
    return out


def load_program(scenario: str = "clean") -> list[Event]:
    """The fixtures replayed with their original timing, ordered by time (stable)."""
    chars = {c.name: c.uuid for c in build_table()}
    events: list[Event] = []
    for t_ms, frame in sidecars.unpack((FIXTURES / "session_60s.synapse-raw.bin").read_bytes()):
        events.append(Event(t_ms, chars[RAW_STREAM_NAMES[frame[1]]], frame))
    for t_ms, frame in sidecars.unpack((FIXTURES / "session_60s.synapse.bin").read_bytes()):
        is_feature = len(frame) == consumer.FEATURE_BYTES
        events.append(Event(t_ms, chars["feature" if is_feature else "status"], frame))
        if is_feature and frame[0] == consumer.FEATURE_VERSION:
            values = consumer.decode_feature(frame)
            if values["heart_rate_bpm"] is not None:  # rr_last is already in 1/1024 s
                hr = consumer.encode_hr_measurement(values["heart_rate_bpm"], values["rr_last"])
                events.append(Event(t_ms, chars["heart_rate_measurement"], hr))
    events.sort(key=lambda e: e.t_ms)
    return _restart(events, RESTART_AT_MS) if scenario == "restart" else events


# --------------------------------------------------------------------------- the band's behaviour


class BandSession:
    """What the band does towards one phone. Feed it the clock and the radio's events.

    ``now_ms`` is any monotonic clock in milliseconds; band time 0 is the first connection and
    the band's clock keeps running across a dropped link, so the phone sees the gap as lost frames.
    """

    def __init__(
        self,
        scenario: str = "clean",
        program: list[Event] | None = None,
        log: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; one of {SCENARIOS}")
        self.scenario = scenario
        self.program = load_program(scenario) if program is None else program
        self._log = log or (lambda _: None)
        self._names = {c.uuid: c.name for c in build_table()}
        self._start_ms: int | None = None
        self._next = 0
        self._subs: set[str] = set()
        self._cp_error_pending = scenario == "cp_error"
        self._dropped = self._readvertised = False
        self.connected = False
        self.raw_on = True
        self.feature_rate_hz = 1
        self.time_ms: int | None = None

    def _t(self, now_ms: int) -> int | None:
        return None if self._start_ms is None else now_ms - self._start_ms

    def _record(self, now_ms: int, event: str, **fields: Any) -> None:
        self._log({"t_ms": self._t(now_ms), "event": event, **fields})

    def connect(self, now_ms: int) -> None:
        """A phone connected: raw streams run (contract rule) and nothing is subscribed yet."""
        if self.connected:
            return
        if self._start_ms is None:
            self._start_ms = now_ms
        self.connected, self.raw_on = True, True
        self._subs.clear()
        self._record(now_ms, "connect")

    def disconnect(self, now_ms: int) -> None:
        """The phone is gone."""
        if self.connected:
            self.connected = False
            self._subs.clear()
            self._record(now_ms, "disconnect")

    def subscribe(self, now_ms: int, char: str, on: bool) -> None:
        """The phone turned notifications of ``char`` on or off (CCCD write)."""
        (self._subs.add if on else self._subs.discard)(char)
        self._record(now_ms, "subscribe", char=self._names[char], on=on)

    def write_control(self, now_ms: int, data: bytes) -> int:
        """Apply a control point write; returns 0 or the contract error code to refuse with."""
        error, op, arg = 0, None, None
        try:
            op, arg = consumer.parse_control(data)
        except consumer.ControlError as exc:
            error = exc.code
        injected = False
        if error == 0 and op == "set_time" and self._cp_error_pending:
            self._cp_error_pending, injected = False, True
            error = consumer.CONTROL_ERRORS["bad_argument"]
        if error == 0:
            if op in ("start", "stop"):
                self.raw_on = op == "start"
            elif op == "set_feature_rate":
                self.feature_rate_hz = arg or 1
            elif op == "set_time":
                self.time_ms = arg
        self._record(
            now_ms, "control", hex=bytes(data).hex(), op=op, error=error, injected=injected
        )
        return error

    def _shaped(self, ev: Event) -> bytes:
        """The frame as sent now: the ``transmitting`` flag follows the raw gate."""
        name, data = self._names[ev.char], ev.data
        where = {"feature": (consumer.FEATURE_VERSION, 1), "status": (consumer.STATUS_VERSION, 3)}
        if name in where and data[0] == where[name][0]:
            i = where[name][1]
            flags = (data[i] | TRANSMITTING) if self.raw_on else (data[i] & ~TRANSMITTING & 0xFF)
            return data[:i] + bytes((flags,)) + data[i + 1 :]
        return data

    def _blackout(self, t_ms: int) -> bool:
        end = DISCONNECT_AT_MS + READVERTISE_AFTER_MS
        return self.scenario == "disconnect" and DISCONNECT_AT_MS <= t_ms < end

    def poll(self, now_ms: int) -> tuple[list[Event], list[str]]:
        """What is due at ``now_ms``: the notifications to send and radio actions to take.

        Actions: ``drop_link`` (disconnect the phone) and ``readvertise``. An event that is due
        but not deliverable (no phone, not subscribed, raw gated off, link down) is consumed.
        """
        t = self._t(now_ms)
        if t is None:
            return [], []
        actions: list[str] = []
        if self.scenario == "disconnect":
            if not self._dropped and t >= DISCONNECT_AT_MS:
                self._dropped = True
                actions.append("drop_link")
            if not self._readvertised and t >= DISCONNECT_AT_MS + READVERTISE_AFTER_MS:
                self._readvertised = True
                actions.append("readvertise")
        out: list[Event] = []
        while self._next < len(self.program) and self.program[self._next].t_ms <= t:
            ev = self.program[self._next]
            self._next += 1
            name = self._names[ev.char]
            gated = name in RAW_STREAM_NAMES.values() and not self.raw_on
            live = self.connected and ev.char in self._subs and not self._blackout(ev.t_ms)
            if live and not gated:
                data = self._shaped(ev)
                out.append(Event(ev.t_ms, ev.char, data))
                self._log({"t_ms": ev.t_ms, "event": "notify", "char": name, "hex": data.hex()})
        for action in actions:
            self._record(now_ms, action)
        return out, actions
