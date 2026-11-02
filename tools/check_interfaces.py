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
import hashlib
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
GATT_CONTRACT_V2 = DEFAULT_ROOT / "contracts" / "gatt_v2.json"
HARDWARE_YAML = DEFAULT_ROOT / "config" / "hardware.yaml"

# sha256 of contracts/gatt_v1.json (LF endings) as Averyn pinned it. v1 is frozen: add v2
# characteristics, never edit v1. Bump this only together with a new contract version and
# the consumers' re-pin, never to make this check pass.
FROZEN_V1_SHA256 = "0c6b9e38fccbf5510ea82741ed5b40c2b2338ce08fe47a7e5655f75a6b0688a9"
TRANSPORT_PY = DEFAULT_ROOT / "sdk" / "python" / "synapse_device" / "transport.py"
CONTRACTS_PY = DEFAULT_ROOT / "src" / "synapse24" / "contracts_generated.py"
CONTRACT_H = DEFAULT_ROOT / "firmware" / "esp32_band" / "ble" / "synapse_contract_generated.h"

# UUID v1 pattern (lowercase, with hyphens).
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
# The generated contract header carries every UUID as a full canonical string
# (`#define SYNAPSE_CONTRACT_UUID_<NAME>_STR "..."`): that is what the firmware is compared with,
# whole, not by one byte.
_HEADER_UUID_RE = re.compile(r'_STR\s+"([0-9a-fA-F]{8}-[0-9a-fA-F-]{27})"')

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

    # Text sources: hardware manifest + host transport + the generated contract
    # artifacts (all carry the canonical UUID strings).
    for label, path in (
        ("config/hardware.yaml", HARDWARE_YAML),
        ("sdk/python/synapse_device/transport.py", TRANSPORT_PY),
        ("src/synapse24/contracts_generated.py", CONTRACTS_PY),
    ):
        if not path.is_file():
            report.fail(f"GATT UUID source {label} is missing: it cannot be checked")
            continue
        uuids = _uuid_strings(path.read_text(encoding="utf-8")) & reference
        missing = reference - uuids
        if missing:
            report.fail(
                f"GATT research-service UUID drift in {label}: canonical UUIDs missing: "
                f"{sorted(missing)}"
            )

    # Firmware: the UUIDs live only in the generated header (the sources carry no second copy),
    # so the whole canonical string of every research UUID must be there.
    if not CONTRACT_H.is_file():
        report.fail(f"{CONTRACT_H} is missing: the firmware UUIDs cannot be checked")
        return
    in_header = {u.lower() for u in _HEADER_UUID_RE.findall(CONTRACT_H.read_text(encoding="utf-8"))}
    missing = sorted(reference - in_header)
    if missing:
        report.fail(
            "GATT research-service UUID drift in the firmware contract header "
            f"(synapse_contract_generated.h): canonical UUIDs missing: {missing}"
        )


def check_gatt_v2_consistent(report: Report, gatt_v2: Path, gatt_v1: Path) -> None:
    """Consumer contract v2: its UUIDs reach every consumer, and v1 stays frozen."""
    if not gatt_v2.is_file():
        report.fail(f"{gatt_v2} is missing: run tools/generate_contracts.py")
        return
    if not gatt_v1.is_file():
        report.fail(f"{gatt_v1} is missing: the frozen v1 contract cannot be checked")
        return
    v2 = _load_json(gatt_v2)

    v1_sha = hashlib.sha256(gatt_v1.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    if v1_sha != FROZEN_V1_SHA256:
        report.fail(
            "contracts/gatt_v1.json changed (sha256 "
            f"{v1_sha[:12]}..., pinned {FROZEN_V1_SHA256[:12]}...): v1 is frozen, "
            "add a v2 characteristic instead"
        )
    if v2.get("v1_frozen", {}).get("sha256") != FROZEN_V1_SHA256:
        report.fail("gatt_v2.json does not record the frozen v1 sha256: regenerate it")

    new = {u.lower() for u in v2["characteristics"].values()}
    v1_chars = _load_json(gatt_v1)["services"]
    taken = {u.lower() for u in v1_chars["feature_stream"]["characteristics"].values()}
    taken |= {u.lower() for u in v1_chars["research"]["characteristics"].values()}
    if len(new) != len(v2["characteristics"]) or new & taken:
        report.fail("a v2 characteristic UUID is duplicated or reuses a v1 UUID")

    for label, path in (
        ("synapse_contract_generated.h", CONTRACT_H),
        ("src/synapse24/contracts_generated.py", CONTRACTS_PY),
        ("config/hardware.yaml", HARDWARE_YAML),
    ):
        if not path.is_file():
            report.fail(f"{label} is missing: the v2 UUIDs cannot be checked")
            continue
        missing = sorted(new - {u.lower() for u in _uuid_strings(path.read_text(encoding="utf-8"))})
        if missing:
            report.fail(f"consumer v2 UUID drift in {label}: missing {missing}")


def check_bleak_isolated(report: Report) -> None:
    """Bleak imports live only in the SDK transport.

    One BLE library surface, one place to review its use (decision Q7): every
    client goes through ``synapse_device.transport``'s injected protocol, so a
    bleak version bump or misbehaviour cannot hide in the AGPL packages.
    """
    pattern = re.compile(r"^\s*(?:import bleak\b|from bleak\b)", re.MULTILINE)
    src_dir = DEFAULT_ROOT / "src"
    for path in sorted(src_dir.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        match = pattern.search(source)
        if match:
            line = source[: match.start()].count("\n") + 1
            report.fail(
                f"{path.relative_to(DEFAULT_ROOT)} imports bleak directly (line {line}): "
                "the BLE library must stay behind synapse_device.transport "
                "(make.py: one import surface, injected everywhere else)"
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
    check_worker_field_ids(report, worker_script, descriptor)
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
    parser.add_argument(
        "--gatt-contract-v2",
        type=Path,
        default=GATT_CONTRACT_V2,
        help="Path to contracts/gatt_v2.json",
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
    check_gatt_v2_consistent(report, args.gatt_contract_v2, args.gatt_contract)

    check_bleak_isolated(report)

    if report.ok:
        print("INTERFACES OK")
        return 0
    print("INTERFACE VIOLATIONS")
    for failure in report.failures:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
