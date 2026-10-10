"""Tests for tools/check_contract.py.

The contract checker is the mechanism that stopped the sample-rate, I2C-pin and
BLE-UUID drift that had accumulated between config/hardware.yaml, the firmware
and the host validator. These tests pin the checker itself: a checker that
cannot fail is worse than no checker at all.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(ROOT / "tools"))

import check_contract as cc  # noqa: E402


def _edit_forearm(data: dict, mutate) -> dict:
    """Apply `mutate` to the forearm_hub entry, returning a new config dict."""

    def apply(pod: dict) -> dict:
        return mutate(pod) if pod.get("pod_id") == "forearm_hub" else pod

    return {**data, "pods": [apply(pod) for pod in data.get("pods", [])]}


def _bringup_with_ppg(ppg_hz: int) -> dict:
    with (ROOT / "config" / "hardware_bringup.yaml").open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    for stream in data["bringup"]["lsl"]["streams"]:
        if stream["name"] == "SYNAPSE_PPG_T0":
            stream["sampling_rate"] = ppg_hz
    return data


@pytest.fixture(scope="module")
def hw() -> dict:
    with (ROOT / "config" / "hardware.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


@pytest.fixture(scope="module")
def bringup() -> dict:
    with (ROOT / "config" / "hardware_bringup.yaml").open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_current_contract_is_clean(hw: dict, bringup: dict) -> None:
    """The repository as committed must pass its own contract check."""
    report = cc.Report()
    cc.check_sampling_rates(hw, bringup, report)
    cc.check_i2c_pins(hw, report)
    cc.check_research_service_uuids(hw, report)
    cc.check_raw_frame_contract(hw, report)
    cc.check_canonical_firmware_paths(report)
    assert not report.failures, "; ".join(report.failures)


def test_uuid_little_endian_conversion_matches_nimble() -> None:
    """NimBLE stores the full reversal of the canonical big-endian form."""
    source = (
        "BLE_UUID128_INIT(0x70, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C, "
        "0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1)"
    )
    assert cc._uuid_bytes_in_source(source, "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70")
    assert not cc._uuid_bytes_in_source(source, "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C79")


def test_uuid_match_survives_line_wrapping() -> None:
    """BLE_UUID128_INIT is written across two lines in the firmware."""
    source = (
        "static const ble_uuid128_t X =\n"
        "    BLE_UUID128_INIT(0x71, 0x4C, 0x3B, 0x2A, 0x1F, 0x0E, 0x9D, 0x8C,\n"
        "                     0x7B, 0x4A, 0xF6, 0xE5, 0xD4, 0xC3, 0xB2, 0xA1);\n"
    )
    assert cc._uuid_bytes_in_source(source, "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C71")


def test_sampling_rate_split_is_detected(hw: dict) -> None:
    """Drift between the manifest and the host LSL outlets must fail."""

    def mutate(pod: dict) -> dict:
        return {**pod, "sampling_rate": {**pod["sampling_rate"], "ppg": 64}}

    report = cc.Report()
    cc.check_sampling_rates(_edit_forearm(hw, mutate), _bringup_with_ppg(64), report)
    assert any("PPG rate mismatch" in f for f in report.failures)


def test_ppg_rate_register_must_match_declared_rate(hw: dict) -> None:
    """Declaring 50 Hz while the MAX30102 register says 64 Hz must fail."""

    def mutate(pod: dict) -> dict:
        ppg = {**pod["metadata"]["ppg_config"], "sample_rate_reg": "0x03"}
        return {**pod, "metadata": {**pod["metadata"], "ppg_config": ppg}}

    report = cc.Report()
    cc.check_sampling_rates(_edit_forearm(hw, mutate), {}, report)
    assert any("PPG rate mismatch" in f for f in report.failures)


def test_reserved_i2c_pins_are_rejected(hw: dict) -> None:
    """GPIO 21-25 are unusable on ESP32-S3 octal-PSRAM modules."""

    def mutate(pod: dict) -> dict:
        return {**pod, "metadata": {**pod["metadata"], "i2c": {"sda_gpio": 22, "scl_gpio": 23}}}

    report = cc.Report()
    cc.check_i2c_pins(_edit_forearm(hw, mutate), report)
    assert any("reserved 21-25" in f for f in report.failures)


def test_research_and_consumer_services_must_differ(hw: dict) -> None:
    report = cc.Report()
    cc.check_research_service_uuids(hw, report)
    assert not report.failures

    merged = {
        **hw,
        "ble": {"feature_stream": {"service_uuid": "A1B2C3D4-E5F6-4A7B-8C9D-0E1F2A3B4C70"}},
    }
    report = cc.Report()
    cc.check_research_service_uuids(merged, report)
    assert any("must use different UUIDs" in f for f in report.failures)


def test_raw_codec_constant_drift_is_detected(hw: dict) -> None:
    """If raw_frame.max_payload_bytes stops matching raw_codec.MAX_PAYLOAD, fail."""
    broken = {**hw, "raw_frame": {**hw["raw_frame"], "max_payload_bytes": 185}}
    report = cc.Report()
    cc.check_raw_frame_contract(broken, report)
    assert any("MAX_PAYLOAD" in f for f in report.failures)


def test_cli_reports_success() -> None:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "tools" / "check_contract.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "CONTRACT OK" in proc.stdout


def test_cli_exit_code_is_non_zero_on_violation(tmp_path: Path) -> None:
    """A zero exit code would let CI pass over a divergent contract."""
    with (ROOT / "config" / "hardware.yaml").open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    data = _edit_forearm(
        data,
        lambda pod: {
            **pod,
            "metadata": {**pod["metadata"], "i2c": {"sda_gpio": 22, "scl_gpio": 21}},
        },
    )
    bad = tmp_path / "hardware.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")

    proc = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "tools" / "check_contract.py"),
            "--hardware-yaml",
            str(bad),
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert proc.returncode != 0
    assert "CONTRACT VIOLATIONS" in proc.stdout
