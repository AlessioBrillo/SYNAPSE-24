"""Synapse interface contract checker.

Verifies that the FIT developer data field IDs and the worker JobResult
payload contracts stay consistent across every place they are declared:

* firmware/esp32_band/fit/fit_writer.h            (C #defines)
* scripts/export_session_fit.py                    (Python hand-rolled writer)
* worker/hrv_worker.py                             (Python hand-rolled reader)
* contracts/fit_developer_fields.json              (canonical descriptor)
* contracts/schemas/worker_job_result.json         (payload JSON Schema)

Exits non-zero on any drift. A checker that cannot fail is worse than no
checker at all — see tools/check_contract.py and tests/test_contract_checker.py.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1]

FIT_DESCRIPTOR = DEFAULT_ROOT / "contracts" / "fit_developer_fields.json"
FIRMWARE_FIT_HEADER = DEFAULT_ROOT / "firmware" / "esp32_band" / "fit" / "fit_writer.h"
EXPORT_SCRIPT = DEFAULT_ROOT / "scripts" / "export_session_fit.py"
EXPORT_MODULE = DEFAULT_ROOT / "src" / "synapse24" / "export" / "__init__.py"
SDK_FIT_MODULE = DEFAULT_ROOT / "sdk" / "python" / "synapse_device" / "fit.py"
WORKER_SCRIPT = DEFAULT_ROOT / "worker" / "hrv_worker.py"
SCHEMAS_DIR = DEFAULT_ROOT / "contracts" / "schemas"

GATT_CONTRACT = DEFAULT_ROOT / "contracts" / "gatt_v1.json"
HARDWARE_YAML = DEFAULT_ROOT / "config" / "hardware.yaml"
FIRMWARE_RESEARCH_SERVICE = (
    DEFAULT_ROOT / "firmware" / "esp32_band" / "ble" / "ble_research_service.c"
)
TRANSPORT_PY = DEFAULT_ROOT / "sdk" / "python" / "synapse_device" / "transport.py"

# UUID v1 pattern (lowercase, with hyphens).
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# NimBLE 128-bit UUIDs are stored little-endian: the first init byte is the
# LAST byte of the canonical UUID string.
_LEADING_BYTE_RE = re.compile(r"BLE_UUID128_INIT\s*\(\s*0x([0-9a-fA-F]{2})")

# C macro name -> expected descriptor field id.
DEV_FIELD_MACROS = (
    "SYNAPSE_DEV_FIELD_STRESS",
    "SYNAPSE_DEV_FIELD_SQI",
    "SYNAPSE_DEV_FIELD_MAP",
    "SYNAPSE_DEV_FIELD_ECG_QUALITY",
    "SYNAPSE_DEV_FIELD_SLEEP_STAGE",
    "SYNAPSE_DEV_FIELD_RMSSD",
    "SYNAPSE_DEV_FIELD_SDNN",
    "SYNAPSE_DEV_FIELD_LF_HF",
)

_DEV_FIELD_RE = re.compile(r"(?:#define\s+)?(" + "|".join(DEV_FIELD_MACROS) + r")\s*[= ]\s*(\d+)")

# The canonical writer declares the fields as ``DevField(<id>, "<name>", ...)``.
_SDK_DEV_FIELD_RE = re.compile(r'DevField\(\s*(\d+)\s*,\s*"([a-z0-9_]+)"')

# Base type of a field in the descriptor -> the FIT base type of the SDK table.
_BASE_TYPE_BY_DESCRIPTOR_TYPE = {
    "uint8": "FIT_BASE_TYPE_UINT8",
    "uint16": "FIT_BASE_TYPE_UINT16",
    "uint32": "FIT_BASE_TYPE_UINT32",
}


class Report:
    """Collects interface violations."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    @property
    def ok(self) -> bool:
        return not self.failures


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _dev_field_ids_from_source(source: str, label: str) -> dict[str, int]:
    """Extract C macro field IDs from a firmware/header source string."""
    ids: dict[str, int] = {}
    for match in _DEV_FIELD_RE.finditer(source):
        macro = match.group(1)
        value = int(match.group(2))
        ids[macro] = value
    return ids


def check_firmware_field_ids(report: Report, path: Path, descriptor: dict[str, Any]) -> None:
    """fit_writer.h #define IDs must match the canonical descriptor."""
    source = path.read_text(encoding="utf-8")
    found = _dev_field_ids_from_source(source, "firmware fit_writer.h")
    expected = {f["c_name"]: f["id"] for f in descriptor["fields"]}
    if found != expected:
        report.fail(
            "firmware fit_writer.h field IDs diverge from descriptor: "
            f"found {found}, expected {expected}"
        )
    missing = set(expected) - set(found)
    if missing:
        report.fail(f"firmware fit_writer.h missing field macros: {sorted(missing)}")
    extra = set(found) - set(expected)
    if extra:
        report.fail(f"firmware fit_writer.h has undeclared field macros: {sorted(extra)}")


def check_export_script_field_ids(report: Report, path: Path, descriptor: dict[str, Any]) -> None:
    """export_session_fit.py field ID constants must match the descriptor."""
    source = path.read_text(encoding="utf-8")
    found = _dev_field_ids_from_source(source, "export script")
    expected = {f["c_name"]: f["id"] for f in descriptor["fields"]}
    if found != expected:
        report.fail(
            "scripts/export_session_fit.py field IDs diverge from descriptor: "
            f"found {found}, expected {expected}"
        )


def check_worker_field_ids(report: Report, path: Path, descriptor: dict[str, Any]) -> None:
    """worker/hrv_worker.py field ID constants must match the descriptor."""
    source = path.read_text(encoding="utf-8")
    found = _dev_field_ids_from_source(source, "worker hrv_worker.py")
    expected = {f["c_name"]: f["id"] for f in descriptor["fields"]}
    if found != expected:
        report.fail(
            "worker/hrv_worker.py field IDs diverge from descriptor: "
            f"found {found}, expected {expected}"
        )


def check_sdk_field_table(report: Report, path: Path, descriptor: dict[str, Any]) -> None:
    """The SDK's developer-field table must match the canonical descriptor.

    The SDK is the single writer of the FIT format: its field table is the
    one every other component uses, so a drift between it and the contract
    would put a different value in the same field for every consumer.
    """
    source = path.read_text(encoding="utf-8")
    found = {int(field_id): name for field_id, name in _SDK_DEV_FIELD_RE.findall(source)}
    expected = {int(f["id"]): f["name"] for f in descriptor["fields"]}
    if found != expected:
        report.fail(
            "sdk/python/synapse_device/fit.py developer fields diverge from the "
            f"descriptor: found {found}, expected {expected}"
        )
    for field in descriptor["fields"]:
        base_type = _BASE_TYPE_BY_DESCRIPTOR_TYPE.get(field["type"])
        if base_type is None:
            continue
        if field.get("unit") == "enum" or "enum" in field:
            # An enumerated field is a one-byte value: the writer may declare
            # it with the enum base type (the FIT profile's choice) or with
            # the plain uint8 one — the wire format is identical.
            alternatives = [base_type, "FIT_BASE_TYPE_ENUM"]
        else:
            alternatives = [base_type]
        if not any(
            re.search(rf'DevField\(\s*{field["id"]}\s*,\s*"{field["name"]}"\s*,\s*{alt}\b', source)
            for alt in alternatives
        ):
            report.fail(
                f"SDK developer field {field['name']} (id {field['id']}) does not use "
                f"{' or '.join(alternatives)} as declared by the descriptor"
            )


def check_export_uses_the_canonical_writer(
    report: Report, script: Path, module: Path, descriptor: dict[str, Any]
) -> None:
    """The export path must not re-implement the FIT encoding.

    The script is a thin wrapper and the module owns the analysis; both must
    take the developer-field table from the SDK, so the field ids of the
    contract cannot drift into a second definition.
    """
    script_source = script.read_text(encoding="utf-8")
    if "synapse24.export" not in script_source:
        report.fail(
            "scripts/export_session_fit.py must delegate to synapse24.export "
            "(one implementation of the export, not two)"
        )
    module_source = module.read_text(encoding="utf-8")
    if "synapse_device.fit" not in module_source:
        report.fail("src/synapse24/export must use the SDK FIT writer (synapse_device.fit)")
    if "DEV_FIELD_BY_NAME" not in module_source:
        report.fail(
            "src/synapse24/export must take the developer-field table from the SDK "
            "(DEV_FIELD_BY_NAME), not from local constants"
        )
    for field in descriptor["fields"]:
        if field["name"] not in module_source:
            report.fail(
                f"src/synapse24/export does not mention the developer field "
                f"{field['name']} of the contract"
            )


def check_worker_uses_the_canonical_reader(report: Report, path: Path) -> None:
    """The worker must decode FIT with the SDK reader, not a private parser.

    A second FIT parser is a second interpretation of the same bytes: it is
    how the worker ended up reading empty windows out of real files.
    """
    source = path.read_text(encoding="utf-8")
    if "synapse_device.fit" not in source:
        report.fail("worker/hrv_worker.py must read FIT with the SDK reader (synapse_device.fit)")
    for forbidden in ("_parse_dev_fields", "_parse_definition"):
        if forbidden in source:
            report.fail(
                f"worker/hrv_worker.py still defines {forbidden}: the FIT decoding "
                "must have a single implementation"
            )


def check_descriptor_valid(report: Report, path: Path) -> None:
    """The FIT descriptor must be well-formed and declare exactly num_fields."""
    try:
        data = _load_json(path)
    except (json.JSONDecodeError, OSError) as exc:
        report.fail(f"FIT descriptor {path} is not valid JSON: {exc}")
        return
    fields = data.get("fields", [])
    if len(fields) != data.get("num_fields", -1):
        report.fail(
            f"FIT descriptor num_fields ({data.get('num_fields')}) != "
            f"declared fields ({len(fields)})"
        )
    ids = [f["id"] for f in fields]
    if ids != list(range(len(ids))):
        report.fail("FIT descriptor field IDs are not a contiguous 0..N-1 sequence")
    names = [f["name"] for f in fields]
    if len(names) != len(set(names)):
        report.fail("FIT descriptor has duplicate field names")
    c_names = [f["c_name"] for f in fields]
    if len(c_names) != len(set(c_names)):
        report.fail("FIT descriptor has duplicate C macro names")


def _uuid_strings(text: str) -> set[str]:
    """Extract all v1 UUID strings from a text blob, normalised to lowercase."""
    return {m.group(0).lower() for m in _UUID_RE.finditer(text)}


def _research_service_uuids(gatt: dict[str, Any]) -> set[str]:
    """The set of UUIDs that belong to the research service (service + chars)."""
    chars = gatt["services"]["research"]["characteristics"]
    uuids = {gatt["services"]["research"]["uuid"].lower()}
    uuids |= {v.lower() for v in chars.values()}
    return uuids


def check_gatt_uuids_consistent(report: Report, gatt_contract: Path) -> None:
    """GATT UUIDs are consistent across contract, manifest, firmware, transport.

    A drift here means the host cannot find the pod's raw-stream
    characteristics — the whole dataset path breaks silently.

    Other services (e.g. the consumer feature stream) legitimately declare
    their own UUIDs in the same files, so each source is filtered to the
    research-service UUIDs before comparison.
    """
    gatt = _load_json(gatt_contract)
    reference = _research_service_uuids(gatt)

    def _firmware_leading_bytes(text: str) -> set[str]:
        """First init byte of each BLE_UUID128_INIT, as two lowercase hex digits."""
        return {m.group(1).lower() for m in _LEADING_BYTE_RE.finditer(text)}

    # Text sources: hardware manifest + host transport (both carry UUID strings).
    for label, path in (
        ("config/hardware.yaml", HARDWARE_YAML),
        ("sdk/python/synapse_device/transport.py", TRANSPORT_PY),
    ):
        if not path.is_file():
            continue
        uuids = _uuid_strings(path.read_text(encoding="utf-8")) & reference
        missing = reference - uuids
        if missing:
            report.fail(
                f"GATT research-service UUID drift in {label}: canonical UUIDs missing: "
                f"{sorted(missing)}"
            )

    # Firmware source: UUIDs are little-endian byte arrays; compare the leading
    # (least-significant) byte of each canonical UUID against the init bytes.
    if FIRMWARE_RESEARCH_SERVICE.is_file():
        fw_bytes = _firmware_leading_bytes(FIRMWARE_RESEARCH_SERVICE.read_text(encoding="utf-8"))
        missing = [u for u in sorted(reference) if u[-2:] not in fw_bytes]
        if missing:
            report.fail(
                "GATT research-service UUID drift in "
                "firmware/esp32_band/ble/ble_research_service.c: canonical UUIDs "
                f"missing (by trailing byte): {missing}"
            )


def check_schemas_valid(report: Report, schemas_dir: Path) -> None:
    """Each payload schema file must be valid JSON with required structure."""
    if not schemas_dir.is_dir():
        report.fail(f"Schemas directory {schemas_dir} does not exist")
        return
    for schema_path in sorted(schemas_dir.glob("*.json")):
        try:
            data = _load_json(schema_path)
        except (json.JSONDecodeError, OSError) as exc:
            report.fail(f"Schema {schema_path.name} is not valid JSON: {exc}")
            continue
        if data.get("$schema") is None:
            report.fail(f"Schema {schema_path.name} missing $schema")
        if "payloads" not in data and "title" not in data:
            report.fail(f"Schema {schema_path.name} has no recognizable structure")


def check_field_ids_consistent(
    report: Report,
    firmware_header: Path,
    export_script: Path,
    worker_script: Path,
    descriptor: dict[str, Any],
    export_module: Path | None = None,
    sdk_fit_module: Path | None = None,
) -> None:
    """Cross-check every implementation location against the descriptor.

    The firmware declares the fields as C macros; the SDK declares them in
    its writer (the single implementation); the export and the worker must
    both take them from the SDK instead of keeping a private copy.
    """
    check_firmware_field_ids(report, firmware_header, descriptor)
    if sdk_fit_module is not None and sdk_fit_module.is_file():
        check_sdk_field_table(report, sdk_fit_module, descriptor)
    if export_module is not None and export_module.is_file():
        check_export_uses_the_canonical_writer(report, export_script, export_module, descriptor)
    else:
        check_export_script_field_ids(report, export_script, descriptor)
    check_worker_uses_the_canonical_reader(report, worker_script)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check Synapse interface contracts (FIT fields + worker payloads)."
    )
    parser.add_argument(
        "--fit-descriptor",
        type=Path,
        default=FIT_DESCRIPTOR,
        help="Path to contracts/fit_developer_fields.json",
    )
    parser.add_argument(
        "--firmware-fit-header",
        type=Path,
        default=FIRMWARE_FIT_HEADER,
        help="Path to firmware/esp32_band/fit/fit_writer.h",
    )
    parser.add_argument(
        "--export-script",
        type=Path,
        default=EXPORT_SCRIPT,
        help="Path to scripts/export_session_fit.py",
    )
    parser.add_argument(
        "--worker",
        type=Path,
        default=WORKER_SCRIPT,
        help="Path to worker/hrv_worker.py",
    )
    parser.add_argument(
        "--export-module",
        type=Path,
        default=EXPORT_MODULE,
        help="Path to src/synapse24/export/__init__.py",
    )
    parser.add_argument(
        "--sdk-fit-module",
        type=Path,
        default=SDK_FIT_MODULE,
        help="Path to sdk/python/synapse_device/fit.py",
    )
    parser.add_argument(
        "--schemas-dir",
        type=Path,
        default=SCHEMAS_DIR,
        help="Directory containing payload JSON Schemas",
    )
    parser.add_argument(
        "--gatt-contract",
        type=Path,
        default=GATT_CONTRACT,
        help="Path to contracts/gatt_v1.json",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    report = Report()

    descriptor: dict[str, Any] | None = None
    try:
        descriptor = _load_json(args.fit_descriptor)
    except (json.JSONDecodeError, OSError) as exc:
        report.fail(f"Cannot load FIT descriptor {args.fit_descriptor}: {exc}")

    if descriptor is not None:
        check_descriptor_valid(report, args.fit_descriptor)
        check_field_ids_consistent(
            report,
            args.firmware_fit_header,
            args.export_script,
            args.worker,
            descriptor,
            args.export_module,
            args.sdk_fit_module,
        )

    check_schemas_valid(report, args.schemas_dir)

    check_gatt_uuids_consistent(report, args.gatt_contract)

    if report.ok:
        print("INTERFACES OK")
        return 0
    print("INTERFACE VIOLATIONS")
    for failure in report.failures:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
