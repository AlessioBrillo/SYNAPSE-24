"""The generated contract artifacts must be in sync with their source.

The UUIDs, the stream ids and the rates live in ``config/hardware.yaml``
(and the FIT field ids in ``contracts/fit_developer_fields.json``). The
firmware and the host read them from the generated artifacts, so a stale
artifact is a silent contract drift: the pod would advertise a UUID the host
does not know.
"""

from __future__ import annotations

import ast
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

# Every path generate_contracts.main() writes. Regenerating into the checkout
# from a test mutates committed artifacts (and races other tests), so all six
# are redirected to tmp_path before main() is called in-process.
OUTPUT_NAMES = (
    "C_HEADER",
    "PY_MODULE",
    "GATT_V2",
    "RAW_FRAME_V1",
    "SENSOR_CONFIG_JSON",
    "SENSOR_CONFIG_MD",
)


def _isolate_outputs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Point all generator outputs into tmp_path and return their new paths."""
    isolated: dict[str, Path] = {}
    for name in OUTPUT_NAMES:
        original = getattr(gc, name)
        assert original.is_absolute(), f"{name} must be an absolute path"
        redirected = tmp_path / original.name
        isolated[name] = redirected
        monkeypatch.setattr(gc, name, redirected)
    return isolated


def test_every_written_artifact_is_isolated() -> None:
    """A new generated output must be added to OUTPUT_NAMES, not written to the checkout."""
    tree = ast.parse((ROOT / "tools" / "generate_contracts.py").read_text(encoding="utf-8"))
    main = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    artifacts = next(
        node
        for node in ast.walk(main)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "artifacts" for target in node.targets
        )
    )
    assert isinstance(artifacts.value, ast.Dict)
    written = {key.id for key in artifacts.value.keys if isinstance(key, ast.Name)}
    assert written == set(OUTPUT_NAMES), (
        f"generate_contracts.main() writes {sorted(written)}; isolate them in OUTPUT_NAMES"
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
    originals = {name: getattr(gc, name) for name in OUTPUT_NAMES}
    committed = {
        name: path.read_bytes() if path.is_file() else b"" for name, path in originals.items()
    }
    isolated = _isolate_outputs(monkeypatch, tmp_path)
    isolated["PY_MODULE"].write_text("# stale\n", encoding="utf-8")

    assert gc.main(["--check"]) == 1
    assert "CONTRACT DRIFT" in capsys.readouterr().err
    # Nothing was written to the checkout: the committed artifacts are the
    # reference the --check subprocess above compares against.
    assert {
        name: path.read_bytes() if path.is_file() else b"" for name, path in originals.items()
    } == committed

    # Writing regenerates the artifact in tmp_path, and the check then passes.
    assert gc.main([]) == 0
    assert all(path.is_file() for path in isolated.values())
    capsys.readouterr()
    assert gc.main(["--check"]) == 0
    assert capsys.readouterr().out.strip().endswith("CONTRACT ARTIFACTS OK")


@pytest.mark.parametrize("name", OUTPUT_NAMES)
def test_check_detects_drift_in_every_artifact(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each generated artifact is covered by the --check gate, not just the .py."""
    isolated = _isolate_outputs(monkeypatch, tmp_path)
    assert gc.main([]) == 0
    isolated[name].write_text("# stale\n", encoding="utf-8")

    assert gc.main(["--check"]) == 1
    captured = capsys.readouterr()
    assert "CONTRACT DRIFT" in captured.err
    assert isolated[name].name in captured.err


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
