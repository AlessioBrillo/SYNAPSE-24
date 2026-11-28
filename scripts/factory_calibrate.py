#!/usr/bin/env python3
"""Write and verify the per-unit calibration record over the USB serial CLI.

The band must be flashed with CONFIG_SYNAPSE_FACTORY_MODE=y. This script is the
host half of the firmware shell in firmware/esp32_band/main/synapse_factory_cli.c:

    uv run python scripts/factory_calibrate.py --port COM3 --dump
    uv run python scripts/factory_calibrate.py --port COM3 \
        --gain-json data/live_validation/gain.json --led 1F,1F --operator "A. Brillo"

A calibration is manufacturing data: the JSON record is written under
data/calibration/<serial>.json (gitignored), never into config/hardware.yaml.
Kconfig remains the default for an uncalibrated band.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.calibration_record import canonical_record  # noqa: E402

CAL_DIR = ROOT / "data" / "calibration"
SERIAL_RE = re.compile(r"^SYN-[0-9A-F]{12}$")


class Transport(Protocol):
    def send(self, line: str) -> str:
        """Send one line and return the device's reply."""


@dataclass
class FakeTransport:
    """In-memory transport for offline tests and the dry-run mode."""

    record: dict[str, object] | None = None
    replies: list[str] | None = None

    def send(self, line: str) -> str:  # noqa: PLR0911 - a command shell is a dispatch table
        if self.record is None:
            self.record = {
                "serial": "SYN-000000000000",
                "source": "defaults",
                "ecg_gain_x100": 110000,
                "ecg_midrail_mv": 0,
                "ppg_led_red": 31,
                "ppg_led_ir": 31,
                "accel_bias_mg": [0, 0, 0],
                "gyro_bias_mdps": [0, 0, 0],
                "date": "",
                "operator": "",
                "instrument": "",
                "fw_version": "",
                "gain_uncertainty_pct_x100": 0,
                "noise_std_uv_x10": 0,
                "battery_capacity_mah": 0,
            }
        parts = line.strip().split()
        if not parts:
            return "ERR empty"
        if parts == ["help"]:
            return "SYNAPSE factory calibration"
        if parts[:2] != ["cal", "dump"] and parts[0] != "cal":
            return f"ERR unknown command: {parts[0]}"
        if parts[1] == "dump":
            return json.dumps(self.record)
        if parts[1] == "erase":
            return "OK erased"
        if parts[1] == "save":
            return "OK saved"
        key = parts[1]
        values = parts[2:]
        if key == "gain":
            self.record["ecg_gain_x100"] = int(values[0])
        elif key == "midrail":
            self.record["ecg_midrail_mv"] = int(values[0])
        elif key == "led":
            self.record["ppg_led_red"] = int(values[0], 16)
            self.record["ppg_led_ir"] = int(values[1], 16)
        elif key == "accel_bias":
            self.record["accel_bias_mg"] = [int(v) for v in values]
        elif key == "gyro_bias":
            self.record["gyro_bias_mdps"] = [int(v) for v in values]
        elif key == "instrument":
            self.record["instrument"] = values[0]
        elif key == "fw":
            self.record["fw_version"] = values[0]
        elif key == "uncertainty":
            self.record["gain_uncertainty_pct_x100"] = int(values[0])
        elif key == "noise":
            self.record["noise_std_uv_x10"] = int(values[0])
        elif key == "battery":
            self.record["battery_capacity_mah"] = int(values[0])
        elif key == "date":
            self.record["date"] = values[0]
        elif key == "operator":
            self.record["operator"] = " ".join(values)
        else:
            return f"ERR bad command: {key}"
        return f"OK {key}"


class SerialTransport:
    """Real USB serial transport; imported lazily so --dry-run needs no port."""

    def __init__(self, port: str, baud: int = 115200, timeout: float = 2.0) -> None:
        import serial  # noqa: PLC0415 - optional until a real port is used

        self._port = port
        self._baud = baud
        self._timeout = timeout
        self._conn = serial.Serial(port, baudrate=baud, timeout=timeout)

    def send(self, line: str) -> str:
        self._conn.write((line.strip() + "\r\n").encode("ascii"))
        chunks: list[str] = []
        while True:
            raw = self._conn.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").strip()
            if text.startswith(("OK", "ERR", "{")):
                chunks.append(text)
                break
            if chunks or len(chunks) > 50:
                break
        return "\n".join(chunks)

    def reboot(self) -> None:
        """Restart the band so a written per-unit profile is actually applied.

        The firmware answers ``OK reboot`` and then calls ``esp_restart()``.
        Reopen the port afterwards so the next calibration command reaches the
        shell of the booted band, not the dead handle of the previous one.
        """
        import time  # noqa: PLC0415 - only when a real reboot is requested

        import serial  # noqa: PLC0415 - already imported by __init__, kept local for clarity

        reply = self.send("cal reboot")
        if not reply.startswith("OK"):
            raise RuntimeError(f"cal reboot refused: {reply!r}")
        time.sleep(2.0)
        self._conn.close()
        self._conn = serial.Serial(self._port, baudrate=self._baud, timeout=self._timeout)

    def close(self) -> None:
        self._conn.close()


def build_commands(args: argparse.Namespace) -> list[str]:  # noqa: PLR0915 - flat CLI dispatch table
    """Translate CLI options into firmware shell lines (pure, testable)."""
    commands: list[str] = []
    if args.gain is not None:
        if not 1 <= int(args.gain) <= 1_000_000:
            raise ValueError(f"gain must be 1..1000000 (x100), got {args.gain}")
        commands.append(f"cal gain {args.gain}")
    elif args.gain_json:
        payload = json.loads(Path(args.gain_json).read_text(encoding="utf-8"))
        x100 = payload.get("sdkconfig_ecg_gain")
        if x100 is None:
            raise ValueError(f"{args.gain_json} has no sdkconfig_ecg_gain")
        commands.append(f"cal gain {int(x100)}")
    if args.midrail is not None:
        commands.append(f"cal midrail {args.midrail}")
    if args.led:
        red, ir = (part.strip() for part in args.led.split(","))
        if not (1 <= int(red, 16) <= 255 and 1 <= int(ir, 16) <= 255):
            raise ValueError("LED codes must be 01..FF each (0 keeps the compiled default)")
        commands.append(f"cal led {red} {ir}")
    for flag, values, limit in (
        ("--accel-bias", args.accel_bias, 2000),
        ("--gyro-bias", args.gyro_bias, 20_000),
    ):
        if not values:
            continue
        ints = [int(value) for value in values]
        if any(not -limit <= value <= limit for value in ints):
            raise ValueError(f"{flag} values must be within -{limit}..{limit}")
    if args.accel_bias:
        commands.append("cal accel_bias " + " ".join(args.accel_bias))
    if args.gyro_bias:
        commands.append("cal gyro_bias " + " ".join(args.gyro_bias))
    if args.imu_json:
        payload = json.loads(Path(args.imu_json).read_text(encoding="utf-8"))
        accel = payload.get("accel_bias_mg")
        gyro = payload.get("gyro_bias_mdps")
        if not accel or not gyro:
            raise ValueError(f"{args.imu_json} needs accel_bias_mg and gyro_bias_mdps")
        commands.append("cal accel_bias " + " ".join(str(int(v)) for v in accel))
        commands.append("cal gyro_bias " + " ".join(str(int(v)) for v in gyro))
    if args.instrument:
        commands.append(f"cal instrument {args.instrument}")
    if args.fw:
        commands.append(f"cal fw {args.fw}")
    if args.uncertainty is not None:
        commands.append(f"cal uncertainty {int(round(float(args.uncertainty) * 100))}")
    if args.noise_uv is not None:
        commands.append(f"cal noise {int(round(float(args.noise_uv) * 10))}")
    battery = getattr(args, "battery", None)
    if battery is not None:
        # Same bounds as the BLE whitelist and device_profile.py: the USB shell
        # must not be able to persist a value that the radio path would refuse.
        if int(battery) < 100 or int(battery) > 10_000:
            raise ValueError(f"battery must be 100..10000 mAh, got {battery}")
        commands.append(f"cal battery {int(battery)}")
    if args.date:
        commands.append(f"cal date {args.date}")
    elif not args.dry_run:
        commands.append(f"cal date {datetime.now(UTC).date().isoformat()}")
    if args.operator:
        commands.append(f"cal operator {args.operator}")
    if commands:
        commands.append("cal save")
    commands.append("cal dump")
    return commands


def parse_dump(reply: str) -> dict[str, object]:
    for raw_line in reversed(reply.splitlines()):
        stripped = raw_line.strip()
        if stripped.startswith("{"):
            record = json.loads(stripped)
            if not SERIAL_RE.match(str(record.get("serial", ""))):
                raise ValueError(f"device returned an invalid serial: {record.get('serial')!r}")
            return record
    raise ValueError(f"no calibration JSON in device reply: {reply!r}")


def write_record(record: dict[str, object]) -> Path:
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    path = CAL_DIR / f"{record['serial']}.json"
    serial_port = path.open("w", encoding="utf-8")
    with serial_port as handle:
        # The band dumps `battery_capacity_mah`/`instrument`/...; the host
        # record must use the canonical names device_profile.verify expects.
        json.dump(canonical_record(record), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0915 - CLI argument surface
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM3", help="USB serial port of the band")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--dry-run", action="store_true", help="offline fake transport")
    parser.add_argument("--gain", type=int, help="measured gain as x100 (e.g. 110000)")
    parser.add_argument("--gain-json", help="output of scripts/g1_measure.py gain")
    parser.add_argument("--midrail", type=int, help="measured mid-rail in mV (metadata only)")
    parser.add_argument("--led", help="red,IR LED codes in hex, e.g. 1F,1F")
    parser.add_argument("--accel-bias", nargs=3, metavar=("X", "Y", "Z"))
    parser.add_argument("--gyro-bias", nargs=3, metavar=("X", "Y", "Z"))
    parser.add_argument("--imu-json", help="output of scripts/calibrate_imu.py")
    parser.add_argument("--instrument", help="serial/ID of the generator or meter used")
    parser.add_argument("--fw", help="firmware version used for the measurement")
    parser.add_argument("--uncertainty", type=float, help="gain uncertainty in percent")
    parser.add_argument("--noise-uv", type=float, help="shorted-input ECG noise in uV")
    parser.add_argument("--battery", type=int, help="fitted cell capacity in mAh")
    parser.add_argument("--date", help="YYYY-MM-DD")
    parser.add_argument("--operator", help="who performed the calibration")
    parser.add_argument("--dump", action="store_true", help="only print the current record")
    parser.add_argument("--erase", action="store_true", help="drop the NVS record")
    parser.add_argument(
        "--reboot", action="store_true", help="restart the band after saving so the profile applies"
    )
    args = parser.parse_args(argv)

    transport: Transport
    if args.dry_run:
        transport = FakeTransport()
    else:
        serial_transport = SerialTransport(args.port, args.baud)
        transport = serial_transport

    try:
        commands = (
            ["cal dump"] if args.dump else ["cal erase"] if args.erase else build_commands(args)
        )
        reply = ""
        for command in commands:
            reply = transport.send(command)
            if reply.startswith("ERR"):
                print(f"factory command failed: {command}: {reply}", file=sys.stderr)
                return 1
        record = parse_dump(reply) if not args.erase else {"serial": "erased"}
        if args.reboot:
            if not isinstance(transport, SerialTransport):
                print("--reboot needs a real serial port (not --dry-run)", file=sys.stderr)
                return 1
            transport.reboot()
            # The drivers read the per-unit record only at boot. Read it again
            # from the booted band so the host record reflects what is applied.
            record = parse_dump(transport.send("cal dump"))
        if args.erase:
            print("calibration record erased: the band uses Kconfig defaults")
            return 0
        if args.dump:
            # A dump is read-only evidence: bench_suite.py parses the JSON line
            # from stdout. Never write a host file just to look at the record —
            # only a real write command creates data/calibration/<serial>.json.
            print(json.dumps(record, sort_keys=True))
        elif args.dry_run:
            print(json.dumps(record, indent=2, sort_keys=True))
        else:
            path = write_record(record)
            print(f"calibration written to {path.relative_to(ROOT)}")
        return 0
    except (ValueError, OSError) as exc:
        print(f"factory calibration failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if isinstance(transport, SerialTransport):
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
