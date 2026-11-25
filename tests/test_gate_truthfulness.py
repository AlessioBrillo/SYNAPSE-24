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

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_entry import (  # noqa: E402
    LSLStreamManager,
    Phase1Validator,
    _overall_pass,
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
            "v": 1,
            "ecg_hz": 500,
            "ppg_hz": 50,
            "imu_hz": 50,
            "ecg_gain_x100": 60000,  # the stale sdkconfig gain trap
            "ppg_led_red": 31,
            "ppg_led_ir": 31,
            "battery_mah": 1000,
        }
        gate = validator._flashed_config_gate()
        assert gate.status == "FAIL"
        assert "ecg_gain_x100" in str(gate.value)

    def test_matching_config_passes(self) -> None:
        validator = _validator()
        validator.live_hardware = True
        validator.device_config_readback = {
            "v": 1,
            "ecg_hz": 500,
            "ppg_hz": 50,
            "imu_hz": 50,
            "ecg_gain_x100": 110000,
            "ppg_led_red": 31,
            "ppg_led_ir": 31,
            "battery_mah": 1000,
        }
        assert validator._flashed_config_gate().status == "PASS"

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
