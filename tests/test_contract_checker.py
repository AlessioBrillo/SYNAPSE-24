"""Tests for tools/check_contract.py.

The contract checker is the mechanism that stopped the sample-rate, I2C-pin and
BLE-UUID drift that had accumulated between config/hardware.yaml, the firmware
and the host validator. These tests pin the checker itself: a checker that
cannot fail is worse than no checker at all.
"""

from __future__ import annotations

import json
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


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


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
    cc.check_no_legacy_rate_literals(report)
    cc.check_stale_uuid_gone_from_sources(ROOT, report)
    cc.check_fit_developer_names(report)
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


class TestNoLegacyRateLiterals:
    """The 64/100 Hz literals must stay out of the host rate consumers."""

    def test_current_tree_is_clean(self) -> None:
        report = cc.Report()
        cc.check_no_legacy_rate_literals(report)
        assert not report.failures, "; ".join(report.failures)

    def test_reintroduced_ppg_literal_fails(self, tmp_path: Path, monkeypatch) -> None:
        """A stale `ppg_sampling_rate: int = 64` must fail, not silently pass."""
        broken = tmp_path / "esp32_tier0.py"
        broken.write_text(
            "from synapse24.hardware.esp32_tier0 import forearm_hub_normative_rates\n"
            "class Config:\n"
            "    ppg_sampling_rate: int = 64\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            cc,
            "_RATE_LITERAL_TARGETS",
            [(broken, "host Tier 0 adapter", "forearm_hub_normative_rates")],
        )
        report = cc.Report()
        cc.check_no_legacy_rate_literals(report)
        assert any("legacy rate literal" in f for f in report.failures)

    def test_missing_manifest_derivation_fails(self, tmp_path: Path, monkeypatch) -> None:
        """A file with no literals but no manifest link is still drifting."""
        clean = tmp_path / "esp32_tier0.py"
        clean.write_text("class Config:\n    ppg_sampling_rate: int = 50\n", encoding="utf-8")
        monkeypatch.setattr(
            cc,
            "_RATE_LITERAL_TARGETS",
            [(clean, "host Tier 0 adapter", "forearm_hub_normative_rates")],
        )
        report = cc.Report()
        cc.check_no_legacy_rate_literals(report)
        assert any("derive rates from the manifest" in f for f in report.failures)

    def test_validator_gate_rate_check_fails(self, tmp_path: Path, monkeypatch) -> None:
        """A hardcoded nominal in a gate check would fail a conforming pod."""
        broken = tmp_path / "validate_phase1_entry.py"
        broken.write_text(
            "def gates():\n    ok = abs(imu_rate - 100) / 100 <= 0.01\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            cc,
            "_RATE_LITERAL_TARGETS",
            [(broken, "Phase 1 validator", "_nominal_rates")],
        )
        report = cc.Report()
        cc.check_no_legacy_rate_literals(report)
        assert any("hardcoded nominal rate" in f for f in report.failures)

    def test_sampling_rate_dict_form_fails(self, tmp_path: Path, monkeypatch) -> None:
        """`sampling_rate={"ppg": 64, ...}` is the form the coordinator carried."""
        broken = tmp_path / "tier1_coordinator.py"
        broken.write_text(
            'cfg = SensorPodConfig(\n    sampling_rate={"ppg": 64, "ecg": 250, "imu": 100}\n)\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(
            cc,
            "_RATE_LITERAL_TARGETS",
            [(broken, "tier 1 pod coordinator", "forearm_hub_normative_rates")],
        )
        report = cc.Report()
        cc.check_no_legacy_rate_literals(report)
        assert any("sampling_rate structure" in f for f in report.failures)

    def test_percentage_is_not_a_rate_literal(self, tmp_path: Path, monkeypatch) -> None:
        """`== 100.0` is a percentage; flagging it would train readers to ignore."""
        ok = tmp_path / "live_lsl_sync.py"
        ok.write_text(
            "from synapse24.hardware.esp32_tier0 import forearm_hub_normative_rates\n"
            "def passes(results):\n"
            "    return all(s['residual'].get('within_10ms_pct', 0.0) == 100.0 for s in results)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            cc,
            "_RATE_LITERAL_TARGETS",
            [(ok, "live 2-pod LSL sync gate", "forearm_hub_normative_rates")],
        )
        report = cc.Report()
        cc.check_no_legacy_rate_literals(report)
        assert not report.failures, "; ".join(report.failures)

    def test_contract_exempt_marker_is_honoured(self, tmp_path: Path, monkeypatch) -> None:
        """A value the manifest does not declare is an explicit, marked parameter."""
        ok = tmp_path / "tier1_coordinator.py"
        ok.write_text(
            "from synapse24.hardware.esp32_tier0 import forearm_hub_normative_rates\n"
            "def register(pod_id: str, imu_rate_hz: int = 100):  # "
            "contract-exempt: not in hardware.yaml\n"
            "    pass\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(
            cc,
            "_RATE_LITERAL_TARGETS",
            [(ok, "tier 1 pod coordinator", "forearm_hub_normative_rates")],
        )
        report = cc.Report()
        cc.check_no_legacy_rate_literals(report)
        assert not report.failures, "; ".join(report.failures)


class TestFitDeveloperNames:
    """FIT ``SYNAPSE_DEV_FIELD_*`` literals must come from the JSON contract.

    check_interfaces.py pins the eight declared ids; this companion closes the
    other hole: a NINTH literal written in code and never declared anywhere.
    """

    def _descriptor(self) -> dict:
        return json.loads(
            (ROOT / "contracts" / "fit_developer_fields.json").read_text(encoding="utf-8")
        )

    def _contract_copy(self, tmp_path: Path) -> Path:
        contract = tmp_path / "fit_developer_fields.json"
        contract.write_text(json.dumps(self._descriptor()), encoding="utf-8")
        return contract

    def _writer_with_canonical_defines(self, tmp_path: Path) -> Path:
        canonical = [f["c_name"] for f in self._descriptor()["fields"]]
        header = "\n".join(f"#define {name} 0" for name in canonical)
        writer = tmp_path / "fit_writer.h"
        writer.write_text(header + "\n", encoding="utf-8")
        return writer

    def test_current_tree_matches_the_contract(self) -> None:
        report = cc.Report()
        cc.check_fit_developer_names(report)
        assert not report.failures, "; ".join(report.failures)

    def test_unknown_field_literal_fails(self, tmp_path: Path) -> None:
        """A field name outside the contract is an undeclared field id."""
        source = _write_text(tmp_path / "some_source.py", "FIELD = SYNAPSE_DEV_FIELD_TUNING_KNOT\n")
        report = cc.Report()
        cc.check_fit_developer_names(
            report,
            contract=cc.FIT_CONTRACT,
            writer=tmp_path / "never_reached_header.h",
            sources=[source],
        )
        assert any("SYNAPSE_DEV_FIELD_TUNING_KNOT" in f for f in report.failures)

    def test_derived_mapping_macro_is_not_a_field(self, tmp_path: Path) -> None:
        """The id -> definition-number mapping helper is not a third spelling."""
        source = _write_text(
            tmp_path / "helper.py",
            "def num(cid: int) -> int:\n    return SYNAPSE_DEV_FIELD_DEFINITION_NUM(cid)\n",
        )
        report = cc.Report()
        cc.check_fit_developer_names(
            report,
            contract=self._contract_copy(tmp_path),
            writer=self._writer_with_canonical_defines(tmp_path),
            sources=[source],
        )
        assert not report.failures, "; ".join(report.failures)

    def test_writer_missing_canonical_define_fails(self, tmp_path: Path) -> None:
        """A c_name removed from fit_writer.h is a wire break, not a refactor."""
        source = _write_text(tmp_path / "clean.py", "nothing here\n")
        header = "#define SYNAPSE_DEV_FIELD_SQI 1\n"  # one define short
        writer = _write_text(tmp_path / "fit_writer.h", header)
        report = cc.Report()
        cc.check_fit_developer_names(
            report,
            contract=self._contract_copy(tmp_path),
            writer=writer,
            sources=[source],
        )
        assert any(
            "does not define" in f and "SYNAPSE_DEV_FIELD_STRESS" in f for f in report.failures
        )

    def test_corrupt_contract_json_fails(self, tmp_path: Path) -> None:
        contract = _write_text(tmp_path / "fit_developer_fields.json", "{not json")
        report = cc.Report()
        cc.check_fit_developer_names(
            report,
            contract=contract,
            writer=tmp_path / "writer.h",
            sources=[],
        )
        assert any("cannot read" in f for f in report.failures)

    def test_descriptor_count_mismatch_fails(self, tmp_path: Path) -> None:
        """num_fields must equal the number of declared c_names."""
        descriptor = self._descriptor()
        descriptor["num_fields"] = 99
        contract = _write_text(tmp_path / "fit_developer_fields.json", json.dumps(descriptor))
        source = _write_text(tmp_path / "clean.py", "ok\n")
        report = cc.Report()
        cc.check_fit_developer_names(
            report,
            contract=contract,
            writer=self._writer_with_canonical_defines(tmp_path),
            sources=[source],
        )
        assert any("declares num_fields=99" in f for f in report.failures)


class TestStaleUuidScan:
    """The malformed 6-group UUID must not re-enter through any new source."""

    # Built by concatenation so the test file itself never carries the
    # dash-groups form (the scan reads everything, including these tests).
    _STALE = "53594E41" + "-" + "5053" + "-" + "452D" + "-" + "4241" + "-" + "4E44" + "-" + "0"
    # Application- version literal: "SYNA" as a uint32, no dash groups, never
    # matched by the scan (fit_writer.h legitimately prints it).

    def test_current_tree_is_clean(self) -> None:
        report = cc.Report()
        cc.check_stale_uuid_gone_from_sources(ROOT, report)
        assert not report.failures, "; ".join(report.failures)

    def test_dash_grouped_reintroduction_fails(self, tmp_path: Path) -> None:
        """A literal with the prefix + dash groups in any shipped file fails."""
        src = tmp_path / "maker.py"
        src.write_text(f'FILTER = "{self._STALE}"\n', encoding="utf-8")
        report = cc.Report()
        cc.check_stale_uuid_gone_from_sources(tmp_path, report)
        assert any(str(src.name) in f for f in report.failures)

    def test_application_version_uint_is_not_matched(self, tmp_path: Path) -> None:
        """0x53594E41 is 'SYNA' without dashes; flagging it would be noise."""
        src = tmp_path / "version.py"
        src.write_text("APP_VERSION = 0x53594E41  # 'SYNA'\n", encoding="utf-8")
        report = cc.Report()
        cc.check_stale_uuid_gone_from_sources(tmp_path, report)
        assert not report.failures, "; ".join(report.failures)

    def test_strategy_archive_is_exempt(self, tmp_path: Path) -> None:
        """The archived strategy docs carry the original input; pure shipping."""
        archive = tmp_path / "SYNAPSE-AVERYN-STRATEGY" / "integration.md"
        archive.parent.mkdir()
        archive.write_text(f'val X = UUID.fromString("{self._STALE}")\n', encoding="utf-8")
        report = cc.Report()
        cc.check_stale_uuid_gone_from_sources(tmp_path, report)
        assert not report.failures, "; ".join(report.failures)
