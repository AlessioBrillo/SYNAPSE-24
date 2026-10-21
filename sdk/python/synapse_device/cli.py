# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""``synapse`` CLI — the device layer of the Synapse Band host stack.

The SDK is the device layer (Apache-2.0): codec, transport, clock and the
XDF/FIT format readers and writers. It never imports the AGPL-licensed
``synapse24`` package, so every command here works on the *device* side of
the loop.

Usage::

    synapse discover                 # scan for pods (BLE adapter required)
    synapse info                     # device info / battery / sensors
    synapse record --source synthetic --out session.xdf --duration 30
    synapse inspect session.xdf      # streams of an XDF, records of a FIT

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
import inspect
import sys
from pathlib import Path

from . import __version__

__all__ = ["main"]

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
    info.add_argument("--address", help="BLE address of the pod")

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
        if inspect.isawaitable(scanned):
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


def _cmd_info(args: argparse.Namespace) -> int:
    print("Device: SYN-SYNTH-0000")
    print("  Hardware: band-v1.0")
    print("  Firmware: 1.0.0")
    print("  Sensors: ECG(AD8232), PPG(MAX30102), IMU(ICM-20948), Temp(TMP117), Power(INA219)")
    print("  Battery: 100% (Charging: No)")
    print("  Cloud: Disconnected (offline mode)")
    return 0


def _cmd_record(args: argparse.Namespace) -> int:
    if args.source != "synthetic":
        print(
            f"error: BLE recording from {args.source} is not implemented yet; "
            "use the synthetic source or the host pipeline",
            file=sys.stderr,
        )
        return 2
    from .record import record_synthetic

    record_synthetic(Path(args.out), duration_s=args.duration)
    print(f"recorded {args.out} ({args.duration}s, synthetic)")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    path = Path(args.file)
    if not path.is_file():
        print(f"error: file not found: {path}", file=sys.stderr)
        return 1
    suffix = path.suffix.lower()
    if suffix == ".fit":
        from .fit import read_fit

        parsed = read_fit(path)
        print(f"FIT: {path} ({path.stat().st_size} bytes)")
        print(f"  records: {parsed.n_records}")
        for record in parsed.records[:10]:
            print(f"    t={record['timestamp']} hr={record['heart_rate']} {record['dev']}")
        if parsed.n_records > 10:
            print(f"    ... {parsed.n_records - 10} more records")
        return 0
    if suffix in (".xdf", ".xdfz"):
        from .xdf import read_xdf

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
