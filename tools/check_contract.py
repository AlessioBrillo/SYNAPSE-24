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
import json
import os
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
# The UUIDs of the contract are generated from hardware.yaml (see
# tools/generate_contracts.py): the firmware includes this header instead of
# carrying a second copy of the contract.
CONTRACT_H = FIRMWARE_DIR / "ble" / "synapse_contract_generated.h"
# Decision Q7: there is ONE codec implementation, and it lives in the device
# SDK; src/synapse24/hardware/raw_codec.py is a pure re-export of it. The
# contract must never read the shim again: a re-export defines nothing, so
# pointing here would let every constant drift silently.
RAW_CODEC_PY = ROOT / "sdk" / "python" / "synapse_device" / "codec.py"
BUILD_SCRIPT = ROOT / "scripts" / "build_and_flash_firmware.py"
BRINGUP_DOC = ROOT / "HARDWARE_BRINGUP.md"
# FIT developer-field contract and every source that mirrors it. The declared
# directory is scanned for its .c/.h sources; the rest are single files.
FIT_CONTRACT = ROOT / "contracts" / "fit_developer_fields.json"
FIT_WRITER_H = FIRMWARE_DIR / "fit" / "fit_writer.h"
FIT_SOURCES = (
    FIRMWARE_DIR / "fit",
    ROOT / "scripts" / "export_session_fit.py",
    ROOT / "worker" / "hrv_worker.py",
    ROOT / "sdk" / "python" / "synapse_device" / "fit.py",
)
# C source suffixes when a FIT source is a directory.
_FIT_SOURCE_SUFFIXES = {".c", ".h", ".cpp", ".hpp"}


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
    # translation unit, separate from the consumer feature-stream server, and
    # the UUID bytes themselves come from the generated contract header.
    ble_sources = ""
    for path in (GATT_H, RESEARCH_C, CONTRACT_H):
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
    # The firmware wraps BLE_UUID128_INIT across two lines, sometimes with a
    # trailing backslash (header macros). Normalise whitespace and drop
    # line-continuation backslashes before matching.
    cleaned = source.replace("\\", "")
    flattened = re.sub(r",\s+", ", ", cleaned)
    return hex_le in flattened


def check_raw_frame_contract(hw: dict, report: Report) -> None:
    """raw_frame.* must mirror the codec constants everywhere they are declared.

    The contract reads the SDK's canonical codec (decision Q7); the repo's
    ``raw_codec.py`` shim re-exports it, so a drift between the two is caught
    here against the one implementation that actually writes the bytes.
    """
    raw = hw.get("raw_frame", {})
    codec = RAW_CODEC_PY.read_text(encoding="utf-8") if RAW_CODEC_PY.is_file() else ""
    if not codec:
        report.check(False, f"the canonical codec is missing: {RAW_CODEC_PY}")
        return

    report.check(
        f"MAX_PAYLOAD = {raw.get('max_payload_bytes')}" in codec,
        f"codec.MAX_PAYLOAD does not match raw_frame.max_payload_bytes="
        f"{raw.get('max_payload_bytes')}",
    )
    report.check(
        f"HEADER_SIZE = {raw.get('header_bytes')}" in codec,
        f"codec.HEADER_SIZE does not match raw_frame.header_bytes={raw.get('header_bytes')}",
    )

    for name, value in raw.get("stream_ids", {}).items():
        expected = f"{name.upper()} = 0X{int(value):02X}"
        report.check(
            expected in codec.upper(),
            f"the canonical codec does not define RawStream.{name.upper()} = 0x{int(value):02X}",
        )

    for name, value in raw.get("sample_formats", {}).items():
        expected = f"{name.upper()} = 0X{int(value):02X}"
        report.check(
            expected in codec.upper(),
            f"the canonical codec does not define RawFormat.{name.upper()} = 0x{int(value):02X}",
        )


def check_consumer_service_contract(hw: dict, report: Report) -> None:
    """The consumer feature stream (Averyn integration) must be coherent.

    hardware.yaml declares the service/characteristic UUIDs, payload size and
    rate. The firmware must declare the same UUIDs (little-endian byte arrays)
    and a struct large enough to carry the payload. The deprecated 6-group UUID
    string from the strategy docs must not appear anywhere.
    """
    ble = hw.get("ble", {})
    fs = ble.get("feature_stream", {})
    service = fs.get("service_uuid")
    char = fs.get("characteristic_uuid")
    payload = fs.get("payload_bytes")
    rate = fs.get("rate_hz")
    rate_max = fs.get("rate_max_hz")

    report.check(
        service is not None and char is not None,
        "hardware.yaml ble.feature_stream must declare service_uuid and characteristic_uuid",
    )
    ble_sources = ""
    for path in (GATT_H, RESEARCH_C, CONTRACT_H):
        if path.is_file():
            ble_sources += path.read_text(encoding="utf-8") + "\n"
    report.check(
        service is not None and _uuid_bytes_in_source(ble_sources, service),
        f"firmware BLE sources do not declare consumer service UUID {service}",
    )
    report.check(
        char is not None and _uuid_bytes_in_source(ble_sources, char),
        f"firmware BLE sources do not declare consumer characteristic UUID {char}",
    )
    # The UUID bytes must live in the generated header only: a hardcoded copy
    # in a translation unit is exactly how the contract drifted before.
    for path in (GATT_H, RESEARCH_C):
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8")
        hardcoded = [
            uuid for uuid in (service, char) if uuid and _uuid_bytes_in_source(source, uuid)
        ]
        report.check(
            not hardcoded,
            f"{path.name} hardcodes the UUID bytes {hardcoded}: use the macros of "
            "synapse_contract_generated.h (tools/generate_contracts.py)",
        )
    report.check(
        service != _pod(hw, "forearm_hub").get("metadata", {}).get("research_service_uuid"),
        "consumer feature-stream service and research service must not share a UUID",
    )
    report.check(
        isinstance(payload, int) and payload >= 20,
        f"consumer payload_bytes must be an int >= 20, got {payload!r}",
    )
    report.check(
        isinstance(rate, int) and isinstance(rate_max, int) and 0 < rate <= rate_max <= 10,
        f"consumer rate must be 1..rate_max<=10 Hz, got rate={rate!r} max={rate_max!r}",
    )


# The strategy input's malformed 6-group UUID ("SYNAPSE-BAND", ASCII) —
# written as e.g. 53594E41 followed by dash-separated hex groups — is not a
# valid UUID and UUID.fromString/Java throws on it. It must never reappear in
# a source that builds or parses a BLE filter; the contract's UUIDs come from
# config/hardware.yaml (via tools/generate_contracts.py) only.
_STALE_UUID_PREFIX = "53594E41"
_STALE_UUID_RE = re.compile(rf"\b{_STALE_UUID_PREFIX}(?:-[0-9A-Fa-f]{{2,12}}){{2,}}\b")
# The tree-wide scan skips archives and build/cache material. The strategy
# directory is exempt because it archives the original external input for
# cross-reference; re-entry anywhere that ships code is a violation.
_STALE_SCAN_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules", "SYNAPSE-AVERYN-STRATEGY"}
_STALE_SCAN_SKIP_HIDDEN = (".",)
_STALE_SCAN_SUFFIXES = {
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".cfg",
    ".cmake",
    ".conf",
    ".gradle",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".kt",
    ".kconfig",
    ".md",
    ".py",
    ".pyi",
    ".sh",
    ".toml",
    ".ts",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def check_stale_uuid_gone_from_sources(scan_root: Path, report: Report) -> None:
    """Scan every text source under ``scan_root`` for the malformed UUID.

    The old check read a fixed list of four files, which is why the prefix
    could come back through a fifth. The scan now walks the whole tree. The
    literal ``0x53594E41 / 0x53594E41u`` application version ("SYNA") is not a
    UUID: only dash-separated multi-group forms match.
    """
    hits: list[str] = []
    for dirpath, dirnames, filenames in os.walk(scan_root):
        dirnames[:] = [
            name
            for name in dirnames
            if not name.startswith(_STALE_SCAN_SKIP_HIDDEN) and name not in _STALE_SCAN_SKIP_DIRS
        ]
        folder = Path(dirpath)
        for name in filenames:
            path = folder / name
            if path.suffix.lower() not in _STALE_SCAN_SUFFIXES:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if _STALE_UUID_RE.search(text):
                try:
                    hits.append(str(path.relative_to(ROOT)))
                except ValueError:
                    hits.append(str(path))
    report.check(
        not hits,
        f"deprecated 6-group UUID prefix {_STALE_UUID_PREFIX}-... still present in: "
        f"{', '.join(hits)} (the contract's UUIDs come from config/hardware.yaml; "
        "see tools/generate_contracts.py)",
    )


# Not a field name: the contract-id -> wire definition-number mapping macro
# that fit_writer.h derives from the ids (it is a function of the contract,
# never a third spelling of a field).
_DERIVED_FIELD_MACROS = {"SYNAPSE_DEV_FIELD_DEFINITION_NUM"}


def check_fit_developer_names(
    report: Report,
    contract: Path | None = None,
    writer: Path | None = None,
    sources: list[Path] | tuple[Path, ...] | None = None,
) -> None:
    """FIT ``SYNAPSE_DEV_FIELD_*`` literals must match the contract, both ways.

    ``contracts/fit_developer_fields.json`` is the single source of truth for
    the developer fields' ids and names (field IDs are immutable once a
    consumer imports files). Every literal in a FIT-relevant source must be
    one of the contract's ``c_name`` values, and every canonical name must be
    present in ``fit_writer.h`` because that header is the wiring both
    exporters mirror. A check this strict is the point: the field ids and
    names cannot be renegotiated by writing a third copy in code.
    """
    contract = contract if contract is not None else FIT_CONTRACT
    writer = writer if writer is not None else FIT_WRITER_H
    sources = sources if sources is not None else FIT_SOURCES

    try:
        descriptor = json.loads(contract.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        report.check(False, f"cannot read {contract} ({FIT_CONTRACT.name}): {exc}")
        return
    if not isinstance(descriptor, dict):
        report.check(False, f"{FIT_CONTRACT.name} must be a JSON object with a 'fields' list")
        return
    fields = descriptor.get("fields", [])
    canonical = {
        field_desc.get("c_name")
        for field_desc in fields
        if isinstance(field_desc, dict) and field_desc.get("c_name")
    }
    report.check(bool(canonical), f"{FIT_CONTRACT.name} declares no c_name values")
    declared_count = descriptor.get("num_fields")
    report.check(
        canonical and len(canonical) == declared_count,
        f"{FIT_CONTRACT.name} declares num_fields={declared_count!r} but "
        f"{len(canonical)} c_name values",
    )

    checked_paths: list[Path] = []
    for entry in sources:
        source_dir_or_file_path = Path(entry)
        if source_dir_or_file_path.is_dir():
            checked_paths.extend(
                path
                for path in sorted(source_dir_or_file_path.rglob("*"))
                if path.is_file() and path.suffix.lower() in _FIT_SOURCE_SUFFIXES
            )
        elif source_dir_or_file_path.is_file():
            checked_paths.append(source_dir_or_file_path)
        else:
            report.check(False, f"FIT source is missing: {source_dir_or_file_path}")

    for path in checked_paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            report.check(False, f"cannot read FIT source {path}: {exc}")
            continue
        used = set(re.findall(r"SYNAPSE_DEV_FIELD_\w+", text))
        unknown = sorted(used - canonical - _DERIVED_FIELD_MACROS)
        report.check(
            not unknown,
            f"{path.name} uses FIT fields not in {FIT_CONTRACT.name}: {', '.join(unknown)} "
            "(add them to the contract, named exactly once)",
        )

    if not writer.is_file():
        report.check(False, f"the FIT writer header is missing: {writer}")
        return
    header = writer.read_text(encoding="utf-8")
    missing = sorted(name for name in canonical if f"#define {name}" not in header)
    report.check(
        not missing,
        f"{writer.name} does not define: {', '.join(missing)} "
        f"(the contract {FIT_CONTRACT.name} and the header are one table)",
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
    parser.add_argument(
        "--fit-contract",
        type=Path,
        default=None,
        help="Path to the FIT developer-fields contract (default: contracts/fit_developer_fields.json)",
    )
    parser.add_argument(
        "--fit-writer",
        type=Path,
        default=None,
        help="Path to the firmware FIT writer header (default: firmware/esp32_band/fit/fit_writer.h)",
    )
    parser.add_argument(
        "--fit-sources",
        type=Path,
        action="append",
        default=None,
        help="Files/directories holding SYNAPSE_DEV_FIELD_* literals; repeatable",
    )
    parser.add_argument(
        "--stale-scan-root",
        type=Path,
        default=None,
        help="Root of the tree scanned for the stale 6-group UUID (default: repo root)",
    )
    args = parser.parse_args()

    hw = _load(args.hardware_yaml)
    bringup = _load(args.bringup_yaml)
    report = Report()

    check_sampling_rates(hw, bringup, report)
    check_i2c_pins(hw, report)
    check_research_service_uuids(hw, report)
    check_consumer_service_contract(hw, report)
    check_raw_frame_contract(hw, report)
    check_canonical_firmware_paths(report)
    check_no_legacy_rate_literals(report)
    check_stale_uuid_gone_from_sources(args.stale_scan_root or ROOT, report)
    check_fit_developer_names(
        report,
        contract=args.fit_contract,
        writer=args.fit_writer,
        sources=args.fit_sources if args.fit_sources else None,
    )

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
