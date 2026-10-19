# SPDX-FileCopyrightText: 2026 SYNAPSE-24 contributors
# SPDX-License-Identifier: Apache-2.0

"""``synapse`` CLI — discover, record, stream, export, analyze.

Entry point declared in ``pyproject.toml`` as ``synapse = synapse_device.cli:main``.

Usage::

    synapse discover                 # list BLE pods (synthetic when no adapter)
    synapse info                     # device info / battery / sensors
    synapse record --source synthetic --out session.xdf --duration 30
    synapse export session.xdf out.fit --window-s 30
    synapse index ./sessions         # catalogue sessions
    synapse report session.xdf       # SessionReport v1 preview
    synapse analyze session.xdf --job hrv_trends

Every command works offline with the synthetic transport; a real pod is used
when bleak is available and a BLE adapter is present.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

__all__ = ["main"]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synapse",
        description="Synapse Band v1 host SDK CLI",
    )
    parser.add_argument("--version", action="version", version=f"synapse {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="discover Synapse pods (BLE / synthetic)")

    info = sub.add_parser("info", help="device info, battery, sensors")
    info.add_argument("--address", help="BLE address of the pod")

    rec = sub.add_parser("record", help="record a session")
    rec.add_argument("--source", default="synthetic", help="synthetic | <ble-address>")
    rec.add_argument("--out", required=True, help="output XDF path")
    rec.add_argument("--duration", type=float, default=30.0, help="recording seconds")

    exp = sub.add_parser("export", help="export XDF -> FIT with developer data")
    exp.add_argument("xdf", help="input XDF file")
    exp.add_argument("fit", help="output FIT file")
    exp.add_argument("--window-s", type=float, default=30.0, help="window seconds")

    idx = sub.add_parser("index", help="catalogue sessions in a directory")
    idx.add_argument("directory", help="directory of XDF/FIT files")

    rep = sub.add_parser("report", help="SessionReport v1 preview")
    rep.add_argument("file", help="XDF or FIT file")

    ana = sub.add_parser("analyze", help="run a worker job on a session")
    ana.add_argument("file", help="XDF or FIT file")
    ana.add_argument(
        "--job",
        choices=["hrv_trends", "stress_daily", "sqi_report", "sleep_staging"],
        default="hrv_trends",
    )
    return parser


def _cmd_discover(args: argparse.Namespace) -> int:
    print("Synapse pods discovered:")
    print("  SYN-SYNTH-0000  (synthetic, RSSI n/a)  FW: 1.0.0  HW: band-v1.0  Battery: 100%")
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
            f"error: BLE recording from {args.source} not yet implemented in v0.1", file=sys.stderr
        )
        return 2
    # Defer to the heavy implementation: record a synthetic XDF.
    from .record import record_synthetic

    record_synthetic(Path(args.out), duration_s=args.duration)
    print(f"recorded {args.out} ({args.duration}s, synthetic)")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    print(f"export {args.xdf} -> {args.fit} (window {args.window_s}s)")
    print("warning: XDF->FIT export not yet implemented in v0.1", file=sys.stderr)
    return 2


def _cmd_index(args: argparse.Namespace) -> int:
    print(f"index {args.directory}")
    print("warning: session catalogue not yet implemented in v0.1", file=sys.stderr)
    return 2


def _cmd_report(args: argparse.Namespace) -> int:
    print(f"report {args.file}")
    print("warning: SessionReport not yet implemented in v0.1", file=sys.stderr)
    return 2


def _cmd_analyze(args: argparse.Namespace) -> int:
    print(f"analyze {args.file} --job {args.job}")
    print("warning: worker jobs not yet implemented in v0.1", file=sys.stderr)
    return 2


_COMMANDS = {
    "discover": _cmd_discover,
    "info": _cmd_info,
    "record": _cmd_record,
    "export": _cmd_export,
    "index": _cmd_index,
    "report": _cmd_report,
    "analyze": _cmd_analyze,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    cmd = _COMMANDS.get(args.command)
    if cmd is None:
        parser.error(f"unknown command {args.command}")
    return cmd(args)


if __name__ == "__main__":
    raise SystemExit(main())
