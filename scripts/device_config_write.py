#!/usr/bin/env python3
"""Write the whitelisted per-unit profile and commit it atomically.

The firmware accepts only the values its calibration record already owns
(ECG gain, PPG LED currents, IMU biases, fitted cell capacity) plus the
consumer feature rate, and only in factory mode. A whole profile can be sent
in one write; the host reads everything back and updates the manufacturing
record only when EVERY field matches. A mismatch leaves the host file
untouched and exits non-zero.

    uv run python scripts/device_config_write.py --address AA:BB:... \
        --serial SYN-... --led 1F,1F --gain 1100 --battery 1000
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

# Wire keys must stay in sync with the whitelist in ble_gatt_server.c.
PROFILE_KEYS = (
    "ecg_gain_x100",
    "ppg_led_red",
    "ppg_led_ir",
    "accel_bias_mg",
    "gyro_bias_mdps",
    "battery_mah",
)
# Payload key -> key in the device-config readback. The two formats use
# different short names; this map is the only place that knows the difference.
READBACK_KEYS = {
    "gain_x100": "ecg_gain_x100",
    "led_red": "ppg_led_red",
    "led_ir": "ppg_led_ir",
    "accel_bias_mg": "accel_bias_mg",
    "gyro_bias_mdps": "gyro_bias_mdps",
    "battery_mah": "battery_mah",
    "feature_rate_hz": "feature_rate_hz",
}


def config_uuid() -> str:
    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    return str(manifest["ble"]["feature_stream"]["device_config_uuid"])


def build_payload(args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.led:
        red, ir = (part.strip() for part in args.led.split(","))
        payload["led_red"] = int(red, 16)
        payload["led_ir"] = int(ir, 16)
    if args.gain is not None:
        payload["gain_x100"] = int(round(float(args.gain) * 100))
    if args.feature_rate is not None:
        payload["feature_rate_hz"] = int(args.feature_rate)
    if args.battery is not None:
        payload["battery_mah"] = int(args.battery)
    if args.accel_bias:
        payload["accel_bias_mg"] = [int(value) for value in args.accel_bias]
    if args.gyro_bias:
        payload["gyro_bias_mdps"] = [int(value) for value in args.gyro_bias]
    if not payload:
        raise ValueError(
            "nothing to write: use --led, --gain, --battery, --accel-bias, "
            "--gyro-bias or --feature-rate"
        )
    return payload


def expected_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The values the readback must hold after the write."""
    return {READBACK_KEYS[wire]: value for wire, value in payload.items() if wire in READBACK_KEYS}


def verify_readback(
    readback: dict[str, Any], expected: dict[str, Any], serial: str
) -> dict[str, Any]:
    """Fail closed on a missing field or a value the band did not accept.

    A successful write always leaves ``pending_reboot`` set: the NVS record is
    committed, but the drivers read it at boot. Reboot verification belongs to
    ``device_profile.py verify``, not to the write command — otherwise a
    correct write could never report its own success.
    """
    if str(readback.get("cal_serial", "")) != serial:
        raise ValueError(
            f"band reports cal_serial={readback.get('cal_serial')!r}, expected {serial!r}"
        )
    mismatches: list[str] = []
    for wire, value in expected.items():
        actual = readback.get(wire)
        if actual is None:
            mismatches.append(f"{wire}: missing from readback")
        elif isinstance(value, list):
            if [int(item) for item in actual] != [int(item) for item in value]:
                mismatches.append(f"{wire}: readback={actual!r}, expected={value!r}")
        elif int(actual) != int(value):
            mismatches.append(f"{wire}: readback={actual!r}, expected={value!r}")
    if mismatches:
        raise ValueError("readback mismatch: " + "; ".join(mismatches))
    return readback


def update_local_record(
    readback: dict[str, Any], serial: str, write: Callable[[Path, str], None] | None = None
) -> Path:
    """Commit the host record only when the band proves it holds this profile."""
    record = {"serial": serial}
    for key in PROFILE_KEYS:
        value = readback.get(key)
        if value is not None:
            record[key] = value
    record["cal_state"] = readback.get("cal_state")
    record["date"] = readback.get("cal_date", "")
    record["operator"] = readback.get("cal_operator", "")
    path = ROOT / "data" / "calibration" / f"{serial}.json"
    sink = write or (
        lambda p, text: (p.parent.mkdir(parents=True, exist_ok=True), p.write_text(text))
    )
    sink(path, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return path


async def _read(address: str) -> dict[str, Any]:
    """Read the full device-config snapshot without writing anything."""
    from bleak import BleakClient  # noqa: PLC0415 - only when a real device is addressed

    uuid = config_uuid()
    async with BleakClient(address) as client:
        raw = await client.read_gatt_char(uuid)
        return json.loads(bytes(raw).decode("ascii", errors="replace").strip("\x00 "))


async def _write(address: str, payload: dict[str, Any]) -> dict[str, Any]:
    from bleak import BleakClient  # noqa: PLC0415 - only when a real device is addressed

    uuid = config_uuid()
    wire = json.dumps(payload, separators=(",", ":"))
    async with BleakClient(address) as client:
        await client.write_gatt_char(uuid, wire.encode("ascii"), response=True)
        raw = await client.read_gatt_char(uuid)
        return json.loads(bytes(raw).decode("ascii", errors="replace").strip("\x00 "))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--led", help="red,IR hex codes, e.g. 1F,1F")
    parser.add_argument("--gain", type=float, help="ECG gain as a multiplier")
    parser.add_argument("--battery", type=int, help="fitted cell capacity in mAh")
    parser.add_argument("--accel-bias", nargs=3, type=int, metavar=("X", "Y", "Z"))
    parser.add_argument("--gyro-bias", nargs=3, type=int, metavar=("X", "Y", "Z"))
    parser.add_argument("--feature-rate", type=int, help="consumer feature Hz (1-5)")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the payload without touching a device"
    )
    args = parser.parse_args()
    try:
        payload = build_payload(args)
        if args.dry_run:
            print(json.dumps(payload, separators=(",", ":")))
            return 0
        readback = asyncio.run(_write(args.address, payload))
        verify_readback(readback, expected_from_payload(payload), args.serial)
        path = update_local_record(readback, args.serial)
        print(f"profile applied and host record updated: {path}")
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"device config write failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
