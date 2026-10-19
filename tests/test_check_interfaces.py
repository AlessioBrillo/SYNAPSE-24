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
WORKER_SCRIPT = ROOT / "worker" / "hrv_worker.py"
SCHEMAS_DIR = ROOT / "contracts" / "schemas"


@pytest.fixture(scope="module")
def descriptor() -> dict:
    return ci._load_json(FIT_DESCRIPTOR)  # noqa: SLF001


def test_current_interfaces_are_clean(descriptor: dict) -> None:
    """The repository as committed must pass its own interface check."""
    report = ci.Report()
    ci.check_field_ids_consistent(report, FIRMWARE_HEADER, EXPORT_SCRIPT, WORKER_SCRIPT, descriptor)
    ci.check_descriptor_valid(report, FIT_DESCRIPTOR)
    ci.check_schemas_valid(report, SCHEMAS_DIR)
    assert not report.failures, "; ".join(report.failures)


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
    """Changing a field ID in export_session_fit.py must fail."""
    source = EXPORT_SCRIPT.read_text(encoding="utf-8")
    broken = source.replace(
        "SYNAPSE_DEV_FIELD_SQI = 1",
        "SYNAPSE_DEV_FIELD_SQI = 9",
    )
    broken_script = tmp_path / "export_session_fit.py"
    broken_script.write_text(broken, encoding="utf-8")
    report = ci.Report()
    ci.check_export_script_field_ids(report, broken_script, descriptor)
    assert any("diverge from descriptor" in f for f in report.failures)


def test_worker_field_id_drift_is_detected(
    descriptor: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing a field ID in worker/hrv_worker.py must fail."""
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
