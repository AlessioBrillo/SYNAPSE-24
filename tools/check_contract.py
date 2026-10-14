#!/usr/bin/env python3
"""Cross-check the SYNAPSE-24 hardware contract.

``config/hardware.yaml`` is the single source of truth for sample rates, I2C
pins, BLE UUIDs, stream ids and LSL stream names. This tool verifies that the
firmware, the host validator, the documentation and the codec all agree with
it. Every check that exists here corresponds to an incoherence that has already
cost time in this project.

Run:
    uv run python tools/check_contract.py
    uv run python tools/check_contract.py --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
HARDWARE_YAML = ROOT / "config" / "hardware.yaml"
BRINGUP_YAML = ROOT / "config" / "hardware_bringup.yaml"
FIRMWARE_DIR = ROOT / "firmware" / "esp32_band"
SENSORS_C = FIRMWARE_DIR / "sensors" / "synapse_sensors.c"
GATT_H = FIRMWARE_DIR / "ble" / "ble_gatt_server.h"
RESEARCH_C = FIRMWARE_DIR / "ble" / "ble_research_service.c"
RAW_CODEC_PY = ROOT / "src" / "synapse24" / "hardware" / "raw_codec.py"
BUILD_SCRIPT = ROOT / "scripts" / "build_and_flash_firmware.py"
BRINGUP_DOC = ROOT / "HARDWARE_BRINGUP.md"

# MAX30102 sample-rate register -> Hz, from the datasheet.
PPG_RATE_REGISTERS = {0x02: 50, 0x03: 64, 0x04: 100, 0x05: 200, 0x06: 400, 0x07: 800}


@dataclass
class Report:
    """Outcome of the contract check."""

    failures: list[str] = field(default_factory=list)
    checks_run: int = 0

    def check(self, ok: bool, message: str) -> bool:
        self.checks_run += 1
        if not ok:
            self.failures.append(message)
        return ok


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _pod(config: dict, pod_id: str) -> dict:
    for pod in config.get("pods", []):
        if pod.get("pod_id") == pod_id:
            return pod
    raise KeyError(f"pod '{pod_id}' not found in hardware.yaml")


def _as_int(value: object) -> int | None:
    """Parse a YAML scalar that may be written as an int or a "0x.." string."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text, 0)
    except ValueError:
        return None


def check_sampling_rates(hw: dict, bringup: dict, report: Report) -> None:
    """PPG/IMU/ECG rates must agree across manifest, Kconfig and host outlets.

    Checks the declared rate against the MAX30102 register value that implies
    it, and against the LSL outlet the host actually creates.
    """
    pod = _pod(hw, "forearm_hub")
    meta = pod.get("metadata", {})
    rates = pod.get("sampling_rate", {})

    ppg_reg = _as_int(meta.get("ppg_config", {}).get("sample_rate_reg"))
    implied_ppg = PPG_RATE_REGISTERS.get(ppg_reg) if ppg_reg is not None else None
    report.check(
        implied_ppg == rates.get("ppg"),
        f"PPG rate mismatch: sampling_rate.ppg={rates.get('ppg')} but "
        f"ppg_config.sample_rate_reg=0x{ppg_reg:02x} implies {implied_ppg} Hz",
    )

    report.check(
        meta.get("imu_config", {}).get("accel_odr_hz") == rates.get("imu"),
        f"IMU rate mismatch: sampling_rate.imu={rates.get('imu')} but "
        f"imu_config.accel_odr_hz={meta.get('imu_config', {}).get('accel_odr_hz')}",
    )

    outlets = {
        stream["name"]: stream
        for stream in bringup.get("bringup", {}).get("lsl", {}).get("streams", [])
    }
    expected = {
        "SYNAPSE_ECG_T0": rates.get("ecg"),
        "SYNAPSE_PPG_T0": rates.get("ppg"),
        "SYNAPSE_ACC_T0": rates.get("imu"),
        "SYNAPSE_GYRO_T0": rates.get("imu"),
        "SYNAPSE_MAG_T0": rates.get("imu"),
    }
    for name, rate in expected.items():
        outlet = outlets.get(name)
        report.check(
            outlet is not None,
            f"LSL outlet '{name}' declared in hardware.yaml is missing from hardware_bringup.yaml",
        )
        if outlet is not None:
            report.check(
                float(outlet.get("sampling_rate", -1)) == float(rate),
                f"LSL outlet '{name}' declares {outlet.get('sampling_rate')} Hz but "
                f"hardware.yaml declares {rate} Hz",
            )


def check_i2c_pins(hw: dict, report: Report) -> None:
    """The shared I2C bus pins must match the firmware and the bringup doc.

    ESP32-S3 has no usable GPIO 21-25 on octal-PSRAM modules; the classic ESP32
    21/22 pair must never appear.
    """
    i2c = _pod(hw, "forearm_hub").get("metadata", {}).get("i2c", {})
    sda, scl = i2c.get("sda_gpio"), i2c.get("scl_gpio")

    src = SENSORS_C.read_text(encoding="utf-8") if SENSORS_C.is_file() else ""
    for role, pin in (("SDA", sda), ("SCL", scl)):
        report.check(
            pin is not None and f"{role.lower()}_gpio_num = {pin}" in src,
            f"firmware synapse_sensors.c does not set {role.lower()}_gpio_num = {pin} "
            f"(hardware.yaml declares {pin})",
        )
        report.check(
            isinstance(pin, int) and not 21 <= pin <= 25,
            f"GPIO {pin} is in the reserved 21-25 octal-PSRAM range on ESP32-S3",
        )

    doc = BRINGUP_DOC.read_text(encoding="utf-8") if BRINGUP_DOC.is_file() else ""
    report.check(
        f"GPIO {sda} (I2C SDA)" in doc or f"GPIO {sda}  (I2C SDA)" in doc,
        f"HARDWARE_BRINGUP.md does not document I2C SDA on GPIO {sda}",
    )


def check_research_service_uuids(hw: dict, report: Report) -> None:
    """The research service must exist in the firmware, separate from the consumer one.

    The research service is the dataset path and must never reuse the UUIDs of
    the consumer feature stream, or a researcher's client would silently
    subscribe to the wrong service.
    """
    meta = _pod(hw, "forearm_hub").get("metadata", {})
    service = meta.get("research_service_uuid")
    chars = meta.get("research_characteristics", {})
    feature = hw.get("ble", {}).get("feature_stream", {}).get("service_uuid")

    report.check(
        service is not None and bool(chars),
        "hardware.yaml must declare the research service and its characteristics",
    )
    report.check(
        service != feature,
        "the research service and the consumer feature stream must use different UUIDs",
    )

    # Both BLE sources are searched: the research service lives in its own
    # translation unit, separate from the consumer feature-stream server.
    ble_sources = ""
    for path in (GATT_H, RESEARCH_C):
        if path.is_file():
            ble_sources += path.read_text(encoding="utf-8") + "\n"
    for name, uuid in chars.items():
        report.check(
            uuid is not None and _uuid_bytes_in_source(ble_sources, uuid),
            f"the firmware BLE sources do not declare research characteristic '{name}' ({uuid})",
        )


def _uuid_bytes_in_source(source: str, uuid_str: str) -> bool:
    """True if the source contains the UUID as a little-endian byte array.

    NimBLE stores 128-bit UUIDs as the full reversal of the canonical
    big-endian form: UUID A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C5D appears as
    BLE_UUID128_INIT(0x5D, 0x4C, ..., 0xB2, 0xA1). This mirrors the existing
    convention in firmware/esp32_band/ble/ble_gatt_server.h.
    """
    parts = [int(p, 16) for p in uuid_str.split("-")]
    if len(parts) != 5:
        return False
    be = parts[0].to_bytes(4, "big") + parts[1].to_bytes(2, "big") + parts[2].to_bytes(2, "big")
    be += parts[3].to_bytes(2, "big") + parts[4].to_bytes(6, "big")
    hex_le = ", ".join(f"0x{b:02X}" for b in reversed(be))
    # The firmware wraps BLE_UUID128_INIT across two lines, so normalise the
    # whitespace before matching.
    flattened = re.sub(r",\s+", ", ", source)
    return hex_le in flattened


def check_raw_frame_contract(hw: dict, report: Report) -> None:
    """raw_frame.* must mirror the codec constants on both sides."""
    raw = hw.get("raw_frame", {})
    codec = RAW_CODEC_PY.read_text(encoding="utf-8") if RAW_CODEC_PY.is_file() else ""

    report.check(
        f"MAX_PAYLOAD = {raw.get('max_payload_bytes')}" in codec,
        f"raw_codec.MAX_PAYLOAD does not match raw_frame.max_payload_bytes="
        f"{raw.get('max_payload_bytes')}",
    )
    report.check(
        f"HEADER_SIZE = {raw.get('header_bytes')}" in codec,
        f"raw_codec.HEADER_SIZE does not match raw_frame.header_bytes={raw.get('header_bytes')}",
    )

    for name, value in raw.get("stream_ids", {}).items():
        expected = f"{name.upper()} = 0X{int(value):02X}"
        report.check(
            expected in codec.upper(),
            f"raw_codec.py does not define RawStream.{name.upper()} = 0x{int(value):02X}",
        )

    for name, value in raw.get("sample_formats", {}).items():
        expected = f"{name.upper()} = 0X{int(value):02X}"
        report.check(
            expected in codec.upper(),
            f"raw_codec.py does not define RawFormat.{name.upper()} = 0x{int(value):02X}",
        )


def check_canonical_firmware_paths(report: Report) -> None:
    """Build tooling must target esp32_band, the canonical firmware."""
    script = BUILD_SCRIPT.read_text(encoding="utf-8") if BUILD_SCRIPT.is_file() else ""
    report.check(
        "esp32_band" in script,
        "scripts/build_and_flash_firmware.py must build firmware/esp32_band",
    )
    report.check(
        "firmware/esp32_tier0" not in script,
        "scripts/build_and_flash_firmware.py still targets the archived esp32_tier0",
    )


# Files that previously carried stale 64/100 Hz literals. Each is checked for
# both the literal and for the presence of the manifest-derived replacement,
# so the drift cannot silently return.
_RATE_LITERAL_TARGETS = [
    (
        ROOT / "src" / "synapse24" / "hardware" / "esp32_tier0.py",
        "host Tier 0 adapter",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "src" / "synapse24" / "acquisition" / "live_lsl_sync.py",
        "live 2-pod LSL sync gate",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "src" / "synapse24" / "acquisition" / "tier1_coordinator.py",
        "tier 1 pod coordinator",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "scripts" / "validate_phase1_entry.py",
        "Phase 1 validator",
        "_nominal_rates",
    ),
    (
        ROOT / "scripts" / "validate_live_sync.py",
        "live sync validation script",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "tests" / "test_tier0_live_loop.py",
        "Tier 0 live-loopback test",
        "forearm_hub_normative_rates",
    ),
    (
        ROOT / "tests" / "test_lsl_live_sync.py",
        "live 2-pod sync test",
        "forearm_hub_normative_rates",
    ),
]

_FORBIDDEN_RATE_LITERALS = [
    # A rate-valued field assigned a legacy literal, with or without a type
    # annotation: ppg_sampling_rate: int = 64, imu_fs=100, accel_sampling_rate=100.
    (
        r"(?:rate|fs)\w*\s*(?::\s*[A-Za-z_\[\]| ]+)?\s*=\s*(?:64|100)\b",
        "legacy rate literal",
    ),
    # A sampling_rate structure still carrying a legacy value: {"ppg": 64, ...}.
    # A percentage (== 100.0) is not a rate and must not match this.
    ("__SAMPLING_RATE_DICT__", "legacy rate literal in a sampling_rate structure"),
    (r"int\(64\s*\*", "hardcoded 64 Hz PPG expectation"),
    (r"abs\([a-z_]+_rate\s*-\s*\d+\)", "hardcoded nominal rate in a gate check"),
]

# A line carrying this marker is exempt from the literal check. It exists for
# values the manifest genuinely does not declare (e.g. the Cerelog head-pod
# IMU rate), where an explicit parameter is the honest representation — the
# alternative is either a hidden literal or an invented manifest entry.
_CONTRACT_EXEMPT = "contract-exempt"


def _legacy_rate_reason(line: str) -> str | None:
    """Why this line carries a stale rate literal, or None if it does not.

    Percentages (``== 100.0``) and battery states are deliberately not rate
    literals: matching them would train the reader to ignore the checker.
    """
    if _CONTRACT_EXEMPT in line:
        return None
    if "sampling_rate" in line and re.search(r"\b(?:64|100)\b", line):
        return "legacy rate literal in a sampling_rate structure"
    for pattern, description in _FORBIDDEN_RATE_LITERALS:
        if pattern == "__SAMPLING_RATE_DICT__":
            continue
        if re.search(pattern, line):
            return description
    return None


def check_no_legacy_rate_literals(report: Report) -> None:
    """The stale 64/100 Hz rates must not reappear in host code or tests.

    hardware.yaml declares PPG 50 Hz / IMU 50 Hz. Those literals used to live
    in the Tier 0 adapter, the synthetic helper, the live-loopback test, the
    pod coordinator and the validator's own gate thresholds, so each of them
    asserted the wrong rates — and the validator would have failed a
    *conforming* pod on the bench. Every checked file must derive the rate
    from the manifest, except lines explicitly marked ``contract-exempt``.
    """
    for path, label, derivation_marker in _RATE_LITERAL_TARGETS:
        if not path.is_file():
            report.check(False, f"{label} is missing: {path}")
            continue
        text = path.read_text(encoding="utf-8")
        report.check(
            derivation_marker in text,
            f"{label} ({path.name}) must derive rates from the manifest via '{derivation_marker}'",
        )
        for pattern, description in _FORBIDDEN_RATE_LITERALS:
            hits = [
                f"{path.name}:{lineno}"
                for lineno, line in enumerate(text.splitlines(), 1)
                if _legacy_rate_reason(line) == description
            ]
            report.check(
                not hits,
                f"{description} found in {label} ({', '.join(hits)}); "
                f"rates come from config/hardware.yaml only",
            )
        # The dict-form check reports through the sampling_rate branch above;
        # surface any remaining sampling_rate lines carrying a stale value.
        dict_hits = [
            f"{path.name}:{lineno}"
            for lineno, line in enumerate(text.splitlines(), 1)
            if "sampling_rate" in line and re.search(r"\b(?:64|100)\b", line)
        ]
        report.check(
            not dict_hits,
            f"legacy rate literal in a sampling_rate structure found in {label} "
            f"({', '.join(dict_hits)}); rates come from config/hardware.yaml only",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--hardware-yaml",
        type=Path,
        default=HARDWARE_YAML,
        help="Path to the hardware manifest to check (default: config/hardware.yaml)",
    )
    parser.add_argument(
        "--bringup-yaml",
        type=Path,
        default=BRINGUP_YAML,
        help="Path to the bringup overrides (default: config/hardware_bringup.yaml)",
    )
    args = parser.parse_args()

    hw = _load(args.hardware_yaml)
    bringup = _load(args.bringup_yaml)
    report = Report()

    check_sampling_rates(hw, bringup, report)
    check_i2c_pins(hw, report)
    check_research_service_uuids(hw, report)
    check_raw_frame_contract(hw, report)
    check_canonical_firmware_paths(report)
    check_no_legacy_rate_literals(report)

    if args.verbose:
        print(f"ran {report.checks_run} contract checks")

    if report.failures:
        print(f"CONTRACT VIOLATIONS ({len(report.failures)}):")
        for failure in report.failures:
            print(f"  - {failure}")
        print()
        print("config/hardware.yaml is the single source of truth. Fix the")
        print("divergent source, or update hardware.yaml together with all")
        print("consumers, so the contract never drifts again.")
        return 1

    print(f"CONTRACT OK: {report.checks_run} checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
