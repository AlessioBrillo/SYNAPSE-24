"""Tests for tools/check_interfaces.py.

The interface checker pins the FIT developer field IDs and worker payload
contracts across firmware, export script, worker and canonical descriptor.
A checker that cannot fail is worse than no checker at all.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "tools"))

import check_interfaces as ci  # noqa: E402

FIT_DESCRIPTOR = ROOT / "contracts" / "fit_developer_fields.json"
FIRMWARE_HEADER = ROOT / "firmware" / "esp32_band" / "fit" / "fit_writer.h"
EXPORT_SCRIPT = ROOT / "scripts" / "export_session_fit.py"
EXPORT_MODULE = ROOT / "src" / "synapse24" / "export" / "__init__.py"
SDK_FIT_MODULE = ROOT / "sdk" / "python" / "synapse_device" / "fit.py"
WORKER_SCRIPT = ROOT / "worker" / "hrv_worker.py"
SCHEMAS_DIR = ROOT / "contracts" / "schemas"


@pytest.fixture(scope="module")
def descriptor() -> dict:
    return ci._load_json(FIT_DESCRIPTOR)  # noqa: SLF001


def test_current_interfaces_are_clean(descriptor: dict) -> None:
    """The repository as committed must pass its own interface check."""
    report = ci.Report()
    ci.check_field_ids_consistent(
        report,
        FIRMWARE_HEADER,
        EXPORT_SCRIPT,
        WORKER_SCRIPT,
        descriptor,
        EXPORT_MODULE,
        SDK_FIT_MODULE,
    )
    ci.check_descriptor_valid(report, FIT_DESCRIPTOR)
    ci.check_schemas_valid(report, SCHEMAS_DIR)
    assert not report.failures, "; ".join(report.failures)


def test_sdk_field_table_drift_is_detected(descriptor: dict, tmp_path: Path) -> None:
    """A renamed developer field in the SDK writer must fail.

    The SDK is the single writer of the FIT format: a field that does not
    match the contract would put a different value in the same field for
    every consumer.
    """
    source = SDK_FIT_MODULE.read_text(encoding="utf-8")
    broken = source.replace('"synapse_sqi"', '"synapse_quality"')
    broken_module = tmp_path / "fit.py"
    broken_module.write_text(broken, encoding="utf-8")
    report = ci.Report()
    ci.check_sdk_field_table(report, broken_module, descriptor)
    assert any("diverge from the descriptor" in f for f in report.failures)


def test_export_must_delegate_to_the_canonical_writer(descriptor: dict, tmp_path: Path) -> None:
    """A second implementation of the export must fail the interface check."""
    broken_script = tmp_path / "export_session_fit.py"
    broken_script.write_text("# a private FIT writer\n", encoding="utf-8")
    report = ci.Report()
    ci.check_export_uses_the_canonical_writer(report, broken_script, EXPORT_MODULE, descriptor)
    assert any("must delegate" in f for f in report.failures)


def test_worker_must_use_the_canonical_reader(tmp_path: Path) -> None:
    """A hand-rolled FIT parser in the worker must fail the interface check."""
    broken_worker = tmp_path / "hrv_worker.py"
    broken_worker.write_text(
        "def _parse_dev_fields(data, pos, end):\n    return {}, pos\n",
        encoding="utf-8",
    )
    report = ci.Report()
    ci.check_worker_uses_the_canonical_reader(report, broken_worker)
    assert any("single implementation" in f for f in report.failures)


def test_firmware_fit_field_id_drift_is_detected(
    descriptor: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing a firmware #define field ID must fail."""
    source = FIRMWARE_HEADER.read_text(encoding="utf-8")
    broken = source.replace(
        "#define SYNAPSE_DEV_FIELD_SQI          1",
        "#define SYNAPSE_DEV_FIELD_SQI          9",
    )
    broken_header = tmp_path / "fit_writer.h"
    broken_header.write_text(broken, encoding="utf-8")
    report = ci.Report()
    ci.check_firmware_field_ids(report, broken_header, descriptor)
    assert any("diverge from descriptor" in f for f in report.failures)


def test_export_script_field_id_drift_is_detected(
    descriptor: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local copy of the field IDs in the export script must fail.

    The script is a thin wrapper: the field table lives in the SDK, and a
    second copy of the ids in the script is exactly the drift the checker
    exists to catch.
    """
    source = EXPORT_SCRIPT.read_text(encoding="utf-8")
    broken = source.replace(
        "from synapse24.export import main  # noqa: E402",
        "SYNAPSE_DEV_FIELD_SQI = 1\nfrom synapse24.export import main  # noqa: E402",
    )
    broken_script = tmp_path / "export_session_fit.py"
    broken_script.write_text(broken, encoding="utf-8")
    report = ci.Report()
    ci.check_export_script_field_ids(report, broken_script, descriptor)
    assert any("diverge from descriptor" in f for f in report.failures)


def test_worker_field_id_drift_is_detected(
    descriptor: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A local copy of the field IDs in the worker must fail.

    The worker takes the ids from the SDK: a private copy in the worker is
    the drift the checker exists to catch.
    """
    source = WORKER_SCRIPT.read_text(encoding="utf-8")
    broken = source.replace(
        "SYNAPSE_DEV_FIELD_SQI = 1",
        "SYNAPSE_DEV_FIELD_SQI = 9",
    )
    broken_worker = tmp_path / "hrv_worker.py"
    broken_worker.write_text(broken, encoding="utf-8")
    report = ci.Report()
    ci.check_worker_field_ids(report, broken_worker, descriptor)
    assert any("diverge from descriptor" in f for f in report.failures)


def test_descriptor_duplicate_ids_fail(
    descriptor: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duplicate field IDs in the descriptor must fail."""
    broken = {
        "num_fields": 8,
        "fields": [
            descriptor["fields"][0],
            {**descriptor["fields"][0], "id": descriptor["fields"][0]["id"]},
        ],
    }
    broken_path = tmp_path / "fit_developer_fields.json"
    broken_path.write_text(json.dumps(broken), encoding="utf-8")
    report = ci.Report()
    ci.check_descriptor_valid(report, broken_path)
    assert any("contiguous" in f or "duplicate" in f for f in report.failures)


def test_cli_reports_success() -> None:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "tools" / "check_interfaces.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "INTERFACES OK" in proc.stdout


def test_cli_exit_code_non_zero_on_violation(tmp_path: Path) -> None:
    """A zero exit code would let CI pass over a divergent interface."""
    bad_descriptor = tmp_path / "fit_developer_fields.json"
    bad_descriptor.write_text(
        json.dumps(
            {
                "num_fields": 8,
                "fields": [
                    {
                        "id": 0,
                        "name": "synapse_stress",
                        "c_name": "SYNAPSE_DEV_FIELD_STRESS",
                        "type": "uint8",
                        "unit": "enum",
                        "invalid": 255,
                        "enum": {"0": "baseline"},
                        "description": "x",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "tools" / "check_interfaces.py"),
            "--fit-descriptor",
            str(bad_descriptor),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode != 0
    assert "INTERFACE VIOLATIONS" in proc.stdout


def test_gatt_contract_is_clean() -> None:
    """The committed GATT contract must pass its own drift check."""
    report = ci.Report()
    ci.check_gatt_uuids_consistent(report, ci.GATT_CONTRACT)
    assert not report.failures, "; ".join(report.failures)


def test_gatt_research_uuid_drift_is_detected(tmp_path: Path) -> None:
    """Changing the research service UUID must fail the interface check."""
    broken = tmp_path / "gatt_v1.json"
    data = json.loads(ci.GATT_CONTRACT.read_text(encoding="utf-8"))
    data["services"]["research"]["uuid"] = "00000000-0000-0000-0000-000000000000"
    broken.write_text(json.dumps(data), encoding="utf-8")
    report = ci.Report()
    ci.check_gatt_uuids_consistent(report, broken)
    assert any("UUID drift" in f for f in report.failures)
