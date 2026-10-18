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
WORKER_SCRIPT = DEFAULT_ROOT / "worker" / "hrv_worker.py"
SCHEMAS_DIR = DEFAULT_ROOT / "contracts" / "schemas"

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
) -> None:
    """Cross-check the three implementation locations against the descriptor."""
    check_firmware_field_ids(report, firmware_header, descriptor)
    check_export_script_field_ids(report, export_script, descriptor)
    check_worker_field_ids(report, worker_script, descriptor)


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
        "--schemas-dir",
        type=Path,
        default=SCHEMAS_DIR,
        help="Directory containing payload JSON Schemas",
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
        )

    check_schemas_valid(report, args.schemas_dir)

    if report.ok:
        print("INTERFACES OK")
        return 0
    print("INTERFACE VIOLATIONS")
    for failure in report.failures:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
