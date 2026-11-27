#!/usr/bin/env python3
"""The single bench command to configure and verify a band's per-unit profile.

Rate, range, ODR, pins and UUIDs stay compilation-time values from
``config/hardware.yaml``. What this command owns is the per-unit part: ECG
gain, PPG LED currents, IMU biases and fitted cell capacity. The consumer
feature rate is a session-only setting applied through the control point /
config write at run time; it is not a persisted per-unit profile field.

    uv run python scripts/device_profile.py example
    uv run python scripts/device_profile.py configure SYN-A1B2C3D4E5F6 \
        --address AA:BB:CC:DD:EE:FF --profile profile.json
    uv run python scripts/device_profile.py verify SYN-A1B2C3D4E5F6 \
        --address AA:BB:CC:DD:EE:FF

``configure`` writes one atomic profile and reads every field back; it does
NOT commit the host record. ``verify`` (after the reboot that applies the
NVS record) compares the live readback with the contract and with that host
record, commits the record only on a complete match, and refuses a profile
that is stored but not yet applied.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
# Running this file directly puts scripts/ (not the repo root) on sys.path.
sys.path.insert(0, str(ROOT))

from scripts import device_config_write as dcw  # noqa: E402

EXAMPLE = {
    "gain_x100": 110000,
    "led_red": 31,
    "led_ir": 31,
    "accel_bias_mg": [0, 0, 0],
    "gyro_bias_mdps": [0, 0, 0],
    "battery_mah": 1000,
}


def contract_fields() -> dict[str, Any]:
    """The compile-time fields a live band must report, from the contract."""
    hw = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    bring = yaml.safe_load((ROOT / "config" / "hardware_bringup.yaml").read_text(encoding="utf-8"))
    pod = next(p for p in hw["pods"] if p.get("status") == "active" and p.get("is_hub"))
    meta = pod["metadata"]
    return {
        "ecg_hz": int(pod["sampling_rate"]["ecg"]),
        "ppg_hz": int(pod["sampling_rate"]["ppg"]),
        "imu_hz": int(pod["sampling_rate"]["imu"]),
        "ecg_adc_ch": int(meta["ecg_config"]["adc_channel"]),
        "i2c_sda": int(meta["i2c"]["sda_gpio"]),
        "i2c_scl": int(meta["i2c"]["scl_gpio"]),
        "battery_mah": int(bring["bringup"]["power_budget"]["hub_battery_mah"]),
    }


def _require_int(key: str, value: Any, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer, got {value!r}")  # noqa: TRY004 - CLI error text
    if not low <= value <= high:
        raise ValueError(f"{key} out of range {low}..{high}: {value}")


def _require_bias(key: str, value: Any, limit: int) -> None:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{key} must be a list of three integers")
    for item in value:
        _require_int(key, item, -32768, 32767)  # the firmware whitelist reads int16
        if not -limit <= int(item) <= limit:
            raise ValueError(f"{key} out of range -{limit}..{limit}: {item}")


def load_profile(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("profile must be a JSON object")  # noqa: TRY004 - CLI error text
    unknown = sorted(set(payload) - set(EXAMPLE) - {"feature_rate_hz"})
    if unknown:
        raise ValueError(f"unknown profile fields: {unknown}")
    if not payload:
        raise ValueError("empty profile")
    # Mirror the firmware whitelist on the host: an out-of-range value must
    # fail before any file or device is touched, with a clear reason.
    if "gain_x100" in payload:
        _require_int("gain_x100", payload["gain_x100"], 1, 1_000_000)
    for key in ("led_red", "led_ir"):
        if key in payload:
            _require_int(key, payload[key], 0, 255)
    if "battery_mah" in payload:
        _require_int("battery_mah", payload["battery_mah"], 100, 10_000)
    if "accel_bias_mg" in payload:
        _require_bias("accel_bias_mg", payload["accel_bias_mg"], 2000)
    if "gyro_bias_mdps" in payload:
        _require_bias("gyro_bias_mdps", payload["gyro_bias_mdps"], 20_000)
    if "feature_rate_hz" in payload:
        _require_int("feature_rate_hz", payload["feature_rate_hz"], 1, 5)
    return payload


def diff_fields(readback: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, value in expected.items():
        actual = readback.get(key)
        if actual is None:
            problems.append(f"{key}: missing from readback")
        elif isinstance(value, list):
            if [int(item) for item in actual] != [int(item) for item in value]:
                problems.append(f"{key}: band={actual!r}, expected={value!r}")
        elif isinstance(value, str):
            if str(actual) != value:
                problems.append(f"{key}: band={actual!r}, expected={value!r}")
        elif int(actual) != int(value):
            problems.append(f"{key}: band={actual!r}, expected={value!r}")
    return problems


def _print_report(label: str, problems: list[str], readback: dict[str, Any]) -> int:
    print(f"{label}: {'PASS' if not problems else 'FAIL'}")
    for problem in problems:
        print(f"  {problem}")
    print(json.dumps(readback, indent=2, sort_keys=True))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("example", help="print a profile template")
    for name in ("configure", "verify"):
        p = sub.add_parser(name)
        p.add_argument("serial", help="DIS serial SYN- + 12 hex")
        p.add_argument("--address", required=True, help="BLE address of the band")
        if name == "configure":
            p.add_argument("--profile", type=Path, required=True)
            p.add_argument(
                "--dry-run", action="store_true", help="print the payload without a device"
            )
    args = parser.parse_args(argv)

    try:
        if args.command == "example":
            print(json.dumps(EXAMPLE, indent=2, sort_keys=True))
            return 0

        serial = dcw.validate_serial(args.serial)
        readback: dict[str, Any]
        expected: dict[str, Any]
        if args.command == "configure":
            payload = load_profile(args.profile)
            if args.dry_run:
                print(json.dumps(payload, separators=(",", ":")))
                return 0
            readback = asyncio.run(dcw._write(args.address, payload))
            expected = dcw.expected_from_payload(payload)
            problems = diff_fields(readback, {"cal_serial": serial, **expected})
            code = _print_report("profile write", problems, readback)
            if not problems:
                print(
                    "profile written to NVS; run 'verify' after the reboot to commit the host record"
                )
            return code

        readback = asyncio.run(dcw._read(args.address))
        expected = dict(contract_fields())
        record_path = ROOT / "data" / "calibration" / f"{serial}.json"
        problems: list[str] = []
        if not record_path.exists():
            problems.append("host calibration record is missing: nothing to verify against")
        else:
            record = json.loads(record_path.read_text(encoding="utf-8"))
            missing = [key for key in dcw.PROFILE_KEYS if key not in record]
            if missing:
                problems.append(f"host calibration record is incomplete: missing {missing}")
            else:
                for key in dcw.PROFILE_KEYS:
                    expected[key] = record[key]
                for key in dcw.PROVENANCE_KEYS:
                    if key in record:
                        expected[key] = record[key]
        problems.extend(diff_fields(readback, {"cal_serial": serial, **expected}))
        if readback.get("pending_reboot"):
            problems.append("pending_reboot: the band still runs the previous profile")
        if problems:
            return _print_report("profile verify", problems, readback)
        code = _print_report("profile verify", [], readback)
        path = dcw.update_local_record(readback, serial)
        print(f"host record committed to data/calibration/{path.name}")
        return code
    except (ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"device profile failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
