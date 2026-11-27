"""The Phase 1 gate must not pass on evidence it never measured.

The SOP promises gates for HR, RMSSD, PPG quality, measured power and the
flashed configuration. Until these tests, ``overall_pass`` could be green with
all of those absent: a missing T1 pod, a disabled Cerelog and a missing POWER
stream were counted as passes. This file pins the new contract:

* unavailable gates are N/A and never count as PASS;
* a G1 live run *requires* the cardiac and POWER evidence;
* the pod's boot-time configuration is compared with the manifest;
* LSL identities carry the device serial discovered over DIS.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_entry import (  # noqa: E402
    LSLStreamManager,
    Phase1Validator,
    _overall_pass,
    append_gate_history,
    load_session_manifest,
    merge_bench_values,
)
from tests.test_validator_clock_gate import (  # noqa: E402
    CONFIG,
    SyntheticPodTransport,
    _logger,
)


def _validator() -> Phase1Validator:
    return Phase1Validator(
        CONFIG,
        _logger(),
        transport=SyntheticPodTransport(offset_ms=0.0, duration_s=1.0),
    )


_FULL_READBACK = {
    "v": 1,
    "ecg_hz": 500,
    "ppg_hz": 50,
    "imu_hz": 50,
    "ecg_gain_x100": 110000,
    "ppg_led_red": 31,
    "ppg_led_ir": 31,
    "battery_mah": 1000,
    "ecg_adc_ch": 0,
    "ecg_lead_off": 15,
    "ppg_addr": 87,
    "ppg_sr": 0,
    "ppg_pw": 3,
    "ppg_adc": 3,
    "imu_addr": 104,
    "accel_g": 8,
    "gyro_dps": 500,
    "accel_hz": 102,
    "gyro_hz": 100,
    "i2c_port": 0,
    "i2c_sda": 8,
    "i2c_scl": 9,
    "sync_gpio": 10,
    "ina_addr": 64,
    "ina_shunt": 100,
    "power_profile": 1,
    "cal_serial": "",
    "cal_state": "none",
    "cal_date": "",
    "cal_operator": "",
}


class TestGateStates:
    """N/A is a verdict, not a hidden pass."""

    def test_unavailable_gates_are_not_applicable_not_passing(self) -> None:
        validator = _validator()
        gates = validator._evaluate_gates({}, {}, {}, {})
        by_name = {gate.name: gate for gate in gates}

        for name in (
            "tier1_sync_1ms",
            "eeg_quality_tier1",
            "flashed_config_matches_contract",
            "hr_accuracy",
            "rmssd_accuracy",
            "ppg_sqi",
            "ppg_map",
        ):
            assert by_name[name].status == "N/A", name
            assert by_name[name].passed is False, name
            assert by_name[name].required is False, name

        # No vacuous pass: N/A gates never enter the required set.
        assert _overall_pass(gates) is False  # completeness gates have no samples

    def test_live_profile_requires_the_missing_evidence(self) -> None:
        validator = _validator()
        validator.live_hardware = True
        gates = validator._evaluate_gates({}, {}, {}, {})
        by_name = {gate.name: gate for gate in gates}

        for name in (
            "hr_accuracy",
            "rmssd_accuracy",
            "ppg_sqi",
            "ppg_map",
            "power_telemetry_measured",
            "flashed_config_matches_contract",
        ):
            assert by_name[name].required is True, name
            assert by_name[name].passed is False, name
        assert by_name["tier1_sync_1ms"].required is False
        assert _overall_pass(gates) is False

    def test_cardiac_gates_pass_when_the_evidence_exists(self) -> None:
        validator = _validator()
        validator.config["bringup"]["validation"]["manual_hr_bpm"] = 72.0
        validator._duration_s = 60.0
        validator._quality_cache = {
            "hr_bpm": 72.4,
            "rmssd_ms": 40.0,
            "ppg_sqi": 0.72,
            "ppg_map": 0.11,
            "rmssd_mae_ms": 2.1,
            "detected_peaks": None,
        }
        gates = {gate.name: gate for gate in validator._evaluate_gates({}, {}, {}, {})}
        for name in ("hr_accuracy", "rmssd_accuracy", "ppg_sqi", "ppg_map"):
            assert gates[name].status == "PASS", (name, gates[name].details)

    def test_bad_cardiac_values_fail_their_gates(self) -> None:
        validator = _validator()
        validator.config["bringup"]["validation"]["manual_hr_bpm"] = 72.0
        validator._duration_s = 60.0
        validator._quality_cache = {
            "hr_bpm": 90.0,
            "rmssd_ms": 40.0,
            "ppg_sqi": 0.12,
            "ppg_map": 0.95,
            "rmssd_mae_ms": 9.0,
            "detected_peaks": None,
        }
        gates = {gate.name: gate for gate in validator._evaluate_gates({}, {}, {}, {})}
        for name in ("hr_accuracy", "rmssd_accuracy", "ppg_sqi", "ppg_map"):
            assert gates[name].status == "FAIL", name

    def test_short_recording_does_not_claim_a_sustained_ppg_gate(self) -> None:
        validator = _validator()
        validator._duration_s = 2.0
        validator._quality_cache = {
            "hr_bpm": None,
            "rmssd_ms": None,
            "ppg_sqi": 0.9,
            "ppg_map": 0.0,
            "rmssd_mae_ms": None,
            "detected_peaks": None,
        }
        gates = {gate.name: gate for gate in validator._evaluate_gates({}, {}, {}, {})}
        assert gates["ppg_sqi"].status == "N/A"
        assert "30" in gates["ppg_sqi"].details


class TestFlashedConfig:
    """The host can prove what the pod actually booted with."""

    def test_stale_gain_is_a_failure(self) -> None:
        validator = _validator()
        validator.live_hardware = True
        validator.device_config_readback = {
            **_FULL_READBACK,
            "ecg_gain_x100": 60000,  # the stale sdkconfig gain trap
        }
        gate = validator._flashed_config_gate()
        assert gate.status == "FAIL"
        assert "ecg_gain_x100" in str(gate.value)

    def test_matching_config_passes(self) -> None:
        validator = _validator()
        validator.live_hardware = True
        validator.device_config_readback = dict(_FULL_READBACK)
        assert validator._flashed_config_gate().status == "PASS"

    def test_missing_field_fails_instead_of_being_ignored(self) -> None:
        validator = _validator()
        validator.live_hardware = True
        readback = dict(_FULL_READBACK)
        del readback["accel_g"]
        validator.device_config_readback = readback
        gate = validator._flashed_config_gate()
        assert gate.status == "FAIL"
        assert "accel_g" in str(gate.value)

    def test_calibration_serial_mismatch_fails(self) -> None:
        validator = _validator()
        validator.live_hardware = True
        validator.device_serial = "SYN-A1B2C3D4E5F6"
        validator.config["bringup"]["ble"]["serial"] = "SYN-A1B2C3D4E5F6"
        readback = dict(_FULL_READBACK)
        readback["cal_serial"] = "SYN-000000000000"
        validator.device_config_readback = readback
        gate = validator._flashed_config_gate()
        assert gate.status == "FAIL"
        assert "cal_serial" in str(gate.value)

    def test_no_readback_on_live_run_fails(self) -> None:
        validator = _validator()
        validator.live_hardware = True
        gate = validator._flashed_config_gate()
        assert gate.status == "FAIL"
        assert "did not report" in gate.details


class TestSerialIdentity:
    """A recording must be traceable to the band that produced it."""

    def test_source_ids_carry_the_serial(self) -> None:
        validator = _validator()
        validator.device_serial = "SYN-A1B2C3D4E5F6"
        validator._create_lsl_outlets()
        for name, cfg in validator.lsl_manager.stream_configs.items():
            rendered = LSLStreamManager.render_source_id(cfg, validator.device_serial)
            assert name in validator.lsl_manager.outlets
            assert validator.device_serial in rendered
            assert "{serial}" not in rendered

    def test_synthetic_runs_are_explicitly_tagged(self) -> None:
        cfg = {"source_id": "synapse_ecg_t0_{serial}"}
        assert LSLStreamManager.render_source_id(cfg) == "synapse_ecg_t0_synthetic"


class TestSessionGovernance:
    """No live recording without consent, and no overwritten history."""

    def _manifest(self, tmp_path: Path, **overrides: object) -> Path:
        payload = {
            "subject_id": "S001",
            "consent": True,
            "condition": "seated",
            "device_serial": "SYN-A1B2C3D4E5F6",
            "operator": "tester",
            "date": "2026-11-26",
        }
        payload.update(overrides)
        path = tmp_path / "manifest.json"
        path.write_text(json.dumps(payload))
        return path

    def test_valid_manifest_is_accepted(self, tmp_path: Path) -> None:
        manifest = load_session_manifest(self._manifest(tmp_path))
        assert manifest is not None
        assert manifest["subject_id"] == "S001"

    def test_missing_consent_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="consent"):
            load_session_manifest(self._manifest(tmp_path, consent=False))

    def test_a_real_name_is_not_a_pseudonym(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="pseudonym"):
            load_session_manifest(self._manifest(tmp_path, subject_id="Mario Rossi"))

    def test_a_bench_manifest_needs_no_subject_or_consent(self, tmp_path: Path) -> None:
        payload = {
            "kind": "bench",
            "condition": "shorted input, off body",
            "device_serial": "SYN-A1B2C3D4E5F6",
            "operator": "tester",
            "date": "2026-11-27",
        }
        path = tmp_path / "bench.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        manifest = load_session_manifest(path)
        assert manifest is not None
        assert manifest["kind"] == "bench"
        assert manifest["consent"] is None

    def test_a_bench_manifest_cannot_claim_a_consent(self, tmp_path: Path) -> None:
        payload = {
            "kind": "bench",
            "condition": "bench",
            "device_serial": "SYN-A1B2C3D4E5F6",
            "operator": "tester",
            "date": "2026-11-27",
            "consent": True,
        }
        path = tmp_path / "bench.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="consent"):
            load_session_manifest(path)

    def test_a_bench_profile_drops_the_human_reference_gates(self) -> None:
        validator = _validator()
        validator.live_hardware = True
        human = validator._required_gates()
        validator.session_kind = "bench"
        bench = validator._required_gates()
        assert "hr_accuracy" in human
        assert "rmssd_accuracy" in human
        assert "hr_accuracy" not in bench
        assert "rmssd_accuracy" not in bench
        assert "flashed_config_matches_contract" in bench

    def test_bench_values_need_a_verifier_and_only_name_hand_rows(self, tmp_path: Path) -> None:
        report = {
            "verified_by": None,
            "measurements": {
                "idle_current_ma": {"value": None, "source": "bench"},
                "hr_mean_bpm": {"value": 70, "source": "synapse24.signal_quality.ecg"},
            },
        }
        values = tmp_path / "values.json"
        values.write_text(json.dumps({"idle_current_ma": 83.2}), encoding="utf-8")
        with pytest.raises(ValueError, match="verified-by"):
            merge_bench_values(report, values)
        report["verified_by"] = "maintainer"
        merge_bench_values(report, values)
        assert report["measurements"]["idle_current_ma"]["value"] == 83.2
        values.write_text(json.dumps({"hr_mean_bpm": 0}), encoding="utf-8")
        with pytest.raises(ValueError, match="only contain"):
            merge_bench_values(report, values)

    def test_history_is_append_only(self, tmp_path: Path) -> None:
        class Report:
            run_id = "run-one"
            timestamp = "2026-11-26T00:00:00Z"
            overall_pass = False

        path = tmp_path / "gate_report.json"
        append_gate_history(path, {"rehearsal": False}, Report())
        Report.run_id = "run-two"
        append_gate_history(path, {"rehearsal": False}, Report())
        lines = (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert (tmp_path / "gate_report_run-one.json").exists()
        assert (tmp_path / "gate_report_run-two.json").exists()


class TestEvidenceNeverFabricated:
    """Regressions for the fail-open paths found in review."""

    def test_rmssd_mae_needs_two_peaks_on_both_sides(self) -> None:
        assert Phase1Validator._rmssd_mae_safe([], [], 500) is None
        assert Phase1Validator._rmssd_mae_safe([100], [100], 500) is None
        value = Phase1Validator._rmssd_mae_safe([100, 200, 300], [100, 210, 300], 500)
        assert value is not None
        assert value > 0

    def test_sustained_ppg_with_no_qualifying_window_is_unknown(self, monkeypatch) -> None:
        validator = _validator()
        validator._duration_s = 60.0

        class SparseSpool:
            @staticmethod
            def read_all(name: str):  # noqa: ANN001, ARG004
                return np.ones((10, 2)), np.arange(10, dtype=np.float64)

        monkeypatch.setattr(validator.lsl_manager, "spool", SparseSpool())
        assert validator._ppg_quality_sustained(np.ones(10), 50, 30.0) == (None, None)

    def test_recorded_xdf_zero_drop_compares_with_pushed_counts(self, tmp_path) -> None:
        from synapse24.utils import verify_xdf_roundtrip

        path = tmp_path / "short.xdf"
        data = np.arange(100, dtype=np.float32).reshape(-1, 1)
        stamps = np.arange(100, dtype=np.float64) / 50.0 + 1000.0
        written = verify_xdf_roundtrip(
            [
                {
                    "name": "SYNAPSE_PPG_T0",
                    "type": "PPG",
                    "data": data,
                    "timestamps": stamps,
                    "sampling_rate": 50,
                }
            ],
            path,
        )
        assert written["total_dropped"] == 0

        validator = _validator()
        validator.lsl_manager._sample_counts["SYNAPSE_PPG_T0"] = 300
        proof = validator._verify_recorded_xdf(path)
        assert proof["all_streams_valid"] is False
        assert proof["total_expected"] == 300
        assert proof["total_recovered"] == 100
        assert proof["total_dropped"] == 200

    def test_gate_report_keeps_na_distinct_from_fail(self) -> None:
        validator = _validator()
        validator._quality_cache = {
            "hr_bpm": None,
            "rmssd_ms": None,
            "ppg_sqi": None,
            "ppg_map": None,
            "rmssd_mae_ms": None,
            "detected_peaks": None,
        }
        report = validator._collect_results({"sample_counts": {}}, {})
        gates = validator._build_gate_report(report)["gates"]
        assert gates
        assert all({"status", "required"}.issubset(gate) for gate in gates)
        na = next(gate for gate in gates if gate["name"] == "hr_accuracy")
        assert na["status"] == "N/A"
        assert na["passed"] is False
        assert na["required"] is False

    def test_run_resets_per_session_evidence(self) -> None:
        validator = _validator()
        validator._quality_cache = {"stale": True}
        validator.device_config_readback = {"stale": True}
        asyncio.run(validator.run(dry_run=True, duration_s=1.0))
        assert validator._quality_cache is None
        assert validator.device_config_readback == {}

    def test_rehearsal_documentation_does_not_claim_live_hardware(self) -> None:
        text = (ROOT / "docs" / "hardware" / "BRINGUP_SOP.md").read_text(encoding="utf-8")
        command = next(
            line
            for line in text.splitlines()
            if "--soak-rehearsal" in line and "validate_phase1_entry" in line
        )
        assert "--live-hardware" not in command
