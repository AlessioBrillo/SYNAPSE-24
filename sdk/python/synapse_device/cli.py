# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""``synapse`` CLI — the device layer of the Synapse Band host stack.

The SDK is the device layer (Apache-2.0): codec, transport, clock and the
XDF/FIT format readers and writers. It never imports the AGPL-licensed
``synapse24`` package, so every command here works on the *device* side of
the loop.

Usage::

    synapse discover                 # scan for pods (BLE adapter required)
    synapse info                     # static SDK facts, no pod, no guess
    synapse info --address AA:BB     # live POWER telemetry probe on a pod
    synapse record --source synthetic --out session.xdf --duration 30
    synapse record --source AA:BB    # real pod: wall-anchored pod stamps
    synapse inspect session.xdf      # streams of an XDF, records of a FIT

``info`` without ``--address`` prints only what the SDK itself knows
(streams, frame format, GATT surface) and NEVER invents a battery reading:
battery/current/power numbers come from a real POWER probe on a pod, and a
probe that gets no frame in time fails instead of printing a placeholder.

The analysis side of the loop lives in the host pipeline and has its own
entry points, so that the same job is never implemented twice:

===========================  =========================================
command                      equivalent in the host pipeline
===========================  =========================================
``synapse export``           ``synapse-export`` (XDF -> FIT)
``synapse index``            ``synapse-sessions index``
``synapse report``           ``synapse-report``
``synapse analyze``          ``synapse-worker --job <type>``
===========================  =========================================
"""

from __future__ import annotations

import argparse
import asyncio
import inspect as _inspect
import sys
import time
from pathlib import Path
from typing import cast

from . import __version__
from .codec import RawCodecError, RawStream, decode_frame
from .transport import (
    CHAR_POWER,
    MTU_TARGET,
    SERVICE_UUID,
    TransportError,
)
from .transport import (
    build_transport as _build_transport,  # seam: monkeypatched in tests
)

__all__ = ["main"]

# Channel semantics of the POWER telemetry frame (INA219, 1 Hz, S32 values
# at MILLI scale == mV / mA / mW / %). Owned by the firmware research
# service contract; the probe prints these labels, nothing else.
_POWER_PROBE_TIMEOUT_S = 3.0
_POWER_LABELS = ("voltage_mv", "current_ma", "power_mw", "battery_pct")

# Commands that moved to the host pipeline, with the replacement to run.
MOVED_COMMANDS = {
    "export": "synapse-export XDF FIT",
    "index": "synapse-sessions index DIRECTORY",
    "report": "synapse-report CATALOG SESSION_ID",
    "analyze": "python worker/hrv_worker.py --job '<job json>' --file FILE",
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synapse",
        description="Synapse Band v1 device-layer SDK CLI",
    )
    parser.add_argument("--version", action="version", version=f"synapse {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser("discover", help="discover Synapse pods (BLE / synthetic)")
    discover.add_argument("--timeout", type=float, default=5.0, help="scan timeout, seconds")

    info = sub.add_parser("info", help="device info, battery, sensors")
    info.add_argument("--address", help="BLE address of the pod (live POWER probe)")

    rec = sub.add_parser("record", help="record a session")
    rec.add_argument("--source", default="synthetic", help="synthetic | <ble-address>")
    rec.add_argument("--out", required=True, help="output XDF path")
    rec.add_argument("--duration", type=float, default=30.0, help="recording seconds")

    inspect = sub.add_parser("inspect", help="inspect an XDF or FIT session file")
    inspect.add_argument("file", help="XDF or FIT file")

    for name, replacement in MOVED_COMMANDS.items():
        moved = sub.add_parser(
            name,
            help=f"(moved) use `{replacement}`",
            description=f"This command moved to the host pipeline: `{replacement}`.",
        )
        moved.add_argument("args", nargs="*", help=argparse.SUPPRESS)
    return parser


def _cmd_discover(args: argparse.Namespace) -> int:
    try:
        from bleak import BleakScanner  # noqa: PLC0415
    except ImportError:
        print("bleak is not installed: pip install synapse-device[ble]")
        print("no BLE adapter available, nothing to discover")
        return 1

    try:
        scanned = BleakScanner.discover(timeout=args.timeout)
        # bleak >= 0.21 exposes an async scanner (a coroutine returning a
        # list); older releases return a dict synchronously.
        if _inspect.isawaitable(scanned):
            import asyncio  # noqa: PLC0415

            found_devices = list(asyncio.run(scanned))
        else:
            found_devices = list(scanned.values())
    except Exception as exc:  # pragma: no cover - depends on the host
        print(f"BLE scan failed: {exc}", file=sys.stderr)
        return 1

    found = [d for d in found_devices if "synapse" in (d.name or "").lower()]
    if not found:
        print("no Synapse pod found")
        return 0
    for device in found:
        print(f"{device.address}  {device.name}")
    return 0


def _info_static() -> int:
    """Print the facts the SDK knows without a pod. Nothing is invented."""
    from .codec import HEADER_SIZE, MAX_PAYLOAD  # noqa: PLC0415
    from .record import STREAM_META  # noqa: PLC0415

    print("Synapse Band v1 - static SDK facts (no pod probed)")
    print("  add --address <pod> to read live POWER telemetry from a pod")
    for stream, (name, type_, channels, _units) in STREAM_META.items():
        del stream
        print(f"  {name} ({type_}): {len(channels)} channels (pod-decided rate)")
    print(
        "  POWER telemetry channels: "
        + ", ".join(_POWER_LABELS)
        + " (1 Hz, INA219, S32 at milli scale)"
    )
    print(f"  frame header: {HEADER_SIZE} bytes, max payload {MAX_PAYLOAD} bytes (codec truth)")
    print(f"  GATT service: {SERVICE_UUID}, raw chars subscribed per session stream")
    print(f"  MTU: target {MTU_TARGET} bytes - the link's granted value is reported, not assumed")
    print("  battery: NOT probed here; numbers come from a pod, never from this command")
    return 0


async def _probe_power(address: str) -> tuple[int | None, tuple[int, ...]]:
    """Connect, negotiate the MTU and wait for one POWER frame from the pod.

    Raises ``TransportError`` on connect/subscribe failure and when no
    POWER frame arrives within ``_POWER_PROBE_TIMEOUT_S`` -- a probe that
    learns nothing says so instead of printing placeholders.

    Returns:
        ``(granted_mtu, power_values)``: the MTU the link granted (or None
        when the backend cannot negotiate) and the four power channels.

    """
    transport = _build_transport(address)
    await transport.connect(address)
    try:
        granted = await transport.request_mtu(MTU_TARGET)
        received: list[bytes] = []

        def _on_power(_uuid: str, frame: bytes) -> None:
            if not received:
                received.append(frame)

        await transport.subscribe(CHAR_POWER, _on_power)
        deadline = time.monotonic() + _POWER_PROBE_TIMEOUT_S
        try:
            while not received:
                if time.monotonic() >= deadline:
                    raise TransportError(
                        f"no POWER frame arrived within {_POWER_PROBE_TIMEOUT_S:.2f} s "
                        f"from {address}"
                    )
                await asyncio.sleep(0.02)
        finally:
            try:
                await transport.unsubscribe(CHAR_POWER)
            except Exception:  # noqa: BLE001 - unsubscribe is best effort
                pass

        header, values = decode_frame(received[0])
        if header.stream is not RawStream.POWER:
            raise TransportError(
                f"{address} delivered a {header.stream.name} frame to the POWER characteristic"
            )
        if len(values) < len(_POWER_LABELS):
            raise TransportError(
                f"POWER frame carries {len(values)} values, expected {len(_POWER_LABELS)}"
            )
        return granted, tuple(int(value) for value in values[: len(_POWER_LABELS)])
    finally:
        try:
            await transport.disconnect()
        except Exception:  # noqa: BLE001 - disconnect is best effort
            pass


def _cmd_info(args: argparse.Namespace) -> int:
    if not args.address:
        return _info_static()
    try:
        granted, values = asyncio.run(_probe_power(args.address))
    except (RawCodecError, TransportError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    granted_text = "unknown (backend does not negotiate)" if granted is None else f"{granted}"
    print(f"pod {args.address}: POWER telemetry (INA219, 1 Hz)")
    print(f"  MTU granted: {granted_text} (target {MTU_TARGET})")
    for label, value in zip(_POWER_LABELS, values, strict=True):
        print(f"  {label} = {value}")
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    from .record import (  # noqa: PLC0415
        BleRecordError,
        XdfRoundTripError,
        record_ble,
        record_synthetic,
    )

    try:
        if args.source.lower() == "synthetic":
            record_synthetic(Path(args.out), duration_s=args.duration)
            print(f"recorded {args.out} ({args.duration} s, synthetic)")
            return 0
        summary = record_ble(
            args.source,
            Path(args.out),
            duration_s=args.duration,
            transport=_build_transport(args.source),
        )
    except (BleRecordError, TransportError, XdfRoundTripError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    granted = summary["granted_mtu"]
    granted_text = "unknown (backend does not negotiate)" if granted is None else str(granted)
    frames = cast("dict[str, int]", summary["frames"])
    rates = cast("dict[str, float]", summary["rate_hz"])
    print(f"recorded {args.out} ({args.duration} s from {args.source})")
    print(f"  MTU granted: {granted_text} (target {MTU_TARGET})")
    for name, count in frames.items():
        rate = rates.get(name, 0.0)
        if rate > 0.0:
            print(f"  {name}: {count} frames, {rate:.3f} Hz measured")
        else:
            print(f"  {name}: {count} frames, rate unmeasured (irregular)")
    print(f"  dropped samples (loss counters carried by frames): {summary['dropped']}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    suffix = path.suffix.lower()
    if suffix == ".fit":
        from .fit import read_fit  # noqa: PLC0415

        parsed = read_fit(path)
        print(f"FIT: {path} ({path.stat().st_size} bytes)")
        print(f"  records: {parsed.n_records}")
        for record in parsed.records[:10]:
            print(f"    t={record['timestamp']} hr={record['heart_rate']} {record['dev']}")
        if parsed.n_records > 10:
            print(f"    ... {parsed.n_records - 10} more records")
        return 0
    if suffix in (".xdf", ".xdfz"):
        from .xdf import read_xdf  # noqa: PLC0415

        session = read_xdf(path)
        print(f"XDF: {path} ({path.stat().st_size} bytes)")
        print(f"  streams: {len(session.streams)}")
        for stream in session.streams:
            print(
                f"    {stream.name} ({stream.type_}) {len(stream.timestamps)} samples "
                f"@ {stream.sample_rate:.1f} Hz"
            )
        return 0
    print(f"error: unsupported file type: {path}", file=sys.stderr)
    return 1


def _cmd_moved(name: str, args: argparse.Namespace) -> int:
    replacement = MOVED_COMMANDS[name]
    print(
        f"`synapse {name}` moved to the host pipeline (the analysis side of the loop).\n"
        f"Use instead: {replacement}\n"
        "See docs/contracts/AVERYN_INTERFACE.md for the full interface.",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    command = args.command
    if command == "discover":
        return _cmd_discover(args)
    if command == "info":
        return _cmd_info(args)
    if command == "record":
        return _cmd_record(args)
    if command == "inspect":
        return _cmd_inspect(args)
    if command in MOVED_COMMANDS:
        return _cmd_moved(command, args)
    parser.error(f"unknown command {command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
