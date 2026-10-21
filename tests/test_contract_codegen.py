"""The generated contract artifacts must be in sync with their source.

The UUIDs, the stream ids and the rates live in ``config/hardware.yaml``
(and the FIT field ids in ``contracts/fit_developer_fields.json``). The
firmware and the host read them from the generated artifacts, so a stale
artifact is a silent contract drift: the pod would advertise a UUID the host
does not know.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import generate_contracts as gc  # noqa: E402

from synapse24.contracts_generated import (  # noqa: E402
    FIT_DEV_FIELDS,
    STREAM_IDS,
    STREAM_RATES_HZ,
    UUIDS,
)


def test_committed_artifacts_are_in_sync_with_the_source() -> None:
    """``--check`` must pass on the committed tree."""
    proc = subprocess.run(  # noqa: S603 - fixed interpreter and script
        [sys.executable, str(ROOT / "tools" / "generate_contracts.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_drift_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A stale artifact must be reported, not silently accepted."""
    stale = tmp_path / "contracts_generated.py"
    stale.write_text("# stale\n", encoding="utf-8")
    monkeypatch.setattr(gc, "PY_MODULE", stale)
    monkeypatch.setattr(gc, "C_HEADER", tmp_path / "synapse_contract_generated.h")

    assert gc.main(["--check"]) == 1
    assert "CONTRACT DRIFT" in capsys.readouterr().err

    # Writing regenerates the artifact, and the check then passes.
    assert gc.main([]) == 0
    assert gc.main(["--check"]) == 0


def test_generated_uuids_match_the_manifest() -> None:
    """The generated UUIDs are the ones the manifest declares."""
    import yaml

    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    pod = next(p for p in manifest["pods"] if p["pod_id"] == "forearm_hub")
    meta = pod["metadata"]
    feature = manifest["ble"]["feature_stream"]

    assert UUIDS["RESEARCH_SERVICE"] == meta["research_service_uuid"].lower()
    assert UUIDS["CONSUMER_SERVICE"] == feature["service_uuid"].lower()
    for name, value in meta["research_characteristics"].items():
        assert UUIDS[f"RESEARCH_CHAR_{name.upper()}"] == value.lower()


def test_generated_stream_ids_and_rates_match_the_manifest() -> None:
    import yaml

    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    assert STREAM_IDS["ecg"] == manifest["raw_frame"]["stream_ids"]["ecg"]
    pod = next(p for p in manifest["pods"] if p["pod_id"] == "forearm_hub")
    assert STREAM_RATES_HZ["ecg"] == pod["sampling_rate"]["ecg"]
    assert STREAM_RATES_HZ["ppg"] == pod["sampling_rate"]["ppg"]


def test_generated_fit_fields_match_the_descriptor() -> None:
    import json

    descriptor = json.loads(
        (ROOT / "contracts" / "fit_developer_fields.json").read_text(encoding="utf-8")
    )
    expected = [(field["name"], field["id"]) for field in descriptor["fields"]]
    assert list(FIT_DEV_FIELDS) == expected


def test_firmware_uses_the_generated_contract() -> None:
    """The firmware must not carry a second copy of the UUIDs."""
    for path in (
        ROOT / "firmware" / "esp32_band" / "ble" / "ble_research_service.c",
        ROOT / "firmware" / "esp32_band" / "ble" / "ble_gatt_server.c",
        ROOT / "firmware" / "esp32_band" / "ble" / "ble_gatt_server.h",
    ):
        source = path.read_text(encoding="utf-8")
        assert "BLE_UUID128_INIT(0x" not in source, (
            f"{path.name} hardcodes a UUID: use the macros of synapse_contract_generated.h"
        )


def test_the_generated_header_declares_every_uuid() -> None:
    header = (ROOT / "firmware" / "esp32_band" / "ble" / "synapse_contract_generated.h").read_text(
        encoding="utf-8"
    )
    for name in UUIDS:
        assert f"SYNAPSE_CONTRACT_UUID_{name}_BYTES" in header
        assert f'SYNAPSE_CONTRACT_UUID_{name}_STR "{UUIDS[name]}"' in header


def _env() -> dict[str, str]:
    import os

    return dict(os.environ)
