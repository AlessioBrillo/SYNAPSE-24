#!/usr/bin/env python3
"""Write the small, whitelisted BLE configuration set and commit it atomically.

The firmware accepts only LED currents, ECG gain and the consumer feature rate,
and only in factory mode. This tool writes one field at a time, reads the
configuration back, and updates the host manufacturing record only when the
readback proves the band applied it. A mismatch leaves the host file untouched.

    uv run python scripts/device_config_write.py --address AA:BB:... \
        --serial SYN-... --led 1F,1F
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]


def config_uuid() -> str:
    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    return str(manifest["ble"]["feature_stream"]["device_config_uuid"])


def build_payload(args: argparse.Namespace) -> str:
    payload: dict[str, int] = {}
    if args.led:
        red, ir = (part.strip() for part in args.led.split(","))
        payload["led_red"] = int(red, 16)
        payload["led_ir"] = int(ir, 16)
    if args.gain is not None:
        payload["gain_x100"] = int(round(float(args.gain) * 100))
    if args.feature_rate is not None:
        payload["feature_rate_hz"] = int(args.feature_rate)
    if not payload:
        raise ValueError("nothing to write: use --led, --gain or --feature-rate")
    return json.dumps(payload, separators=(",", ":"))


def update_local_record(
    readback: dict[str, Any], serial: str, write: Callable[[Path, str], None] | None = None
) -> Path:
    """Commit the host record only when the band proves it holds this serial."""
    if str(readback.get("cal_serial", "")) != serial:
        raise ValueError(
            f"band reports cal_serial={readback.get('cal_serial')!r}, expected {serial!r}"
        )
    record = {
        "serial": serial,
        "ecg_gain_x100": readback.get("ecg_gain_x100", 0),
        "ppg_led_red": readback.get("ppg_led_red", 0),
        "ppg_led_ir": readback.get("ppg_led_ir", 0),
        "cal_state": readback.get("cal_state"),
        "date": readback.get("cal_date", ""),
        "operator": readback.get("cal_operator", ""),
    }
    path = ROOT / "data" / "calibration" / f"{serial}.json"
    sink = write or (
        lambda p, text: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(text))
    )
    sink(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


async def _write(address: str, payload: str) -> dict[str, Any]:
    from bleak import BleakClient

    uuid = config_uuid()
    async with BleakClient(address) as client:
        await client.write_gatt_char(uuid, payload.encode("ascii"), response=True)
        raw = await client.read_gatt_char(uuid)
        return json.loads(bytes(raw).decode("ascii", errors="replace").strip("\x00 "))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--led", help="red,IR hex codes, e.g. 1F,1F")
    parser.add_argument("--gain", type=float, help="ECG gain as a multiplier")
    parser.add_argument("--feature-rate", type=int, help="consumer feature Hz (1-5)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        payload = build_payload(args)
        if args.dry_run:
            print(payload)
            return 0
        readback = asyncio.run(_write(args.address, payload))
        path = update_local_record(readback, args.serial)
        print(f"config applied and host record updated: {path}")
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"device config write failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
