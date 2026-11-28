"""The live-onboarding path must not lie to a real band.

These tests pin the four properties that only show up with hardware: the
notification set is the one the firmware actually advertises, the XDF carries
the units the decoder produced, the session has a wall-clock anchor for later
export, and a refused attempt is still recorded in the append-only history.
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_phase1_entry import (  # noqa: E402
    NOTIFY_CHARACTERISTICS,
    LSLStreamManager,
    Phase1Validator,
    load_session_overrides,
    subscribable_characteristics,
)
from tests.test_validator_clock_gate import CONFIG, _logger  # noqa: E402


def _bringup() -> dict:
    return yaml.safe_load((ROOT / "config" / "hardware_bringup.yaml").read_text(encoding="utf-8"))


class _RecordingClient:
    def __init__(self) -> None:
        self.subscribed: list[str] = []

    async def start_notify(self, uuid: str, handler: object) -> None:
        self.subscribed.append(uuid)


def test_only_notify_characteristics_are_subscribed() -> None:
    cfg = _bringup()
    selected = subscribable_characteristics(cfg["bringup"])
    assert set(selected) == set(NOTIFY_CHARACTERISTICS)
    # A service UUID is not a characteristic; SYNC is READ|WRITE only.
    assert cfg["bringup"]["ble"]["characteristics"]["service"] not in selected.values()
    assert cfg["bringup"]["ble"]["characteristics"]["sync"] not in selected.values()


def test_missing_notify_uuid_is_refused() -> None:
    cfg = _bringup()
    del cfg["bringup"]["ble"]["characteristics"]["ecg"]
    with pytest.raises(ValueError, match="lacks notify UUIDs: ecg"):
        subscribable_characteristics(cfg["bringup"])


def test_subscribe_call_never_touches_service_or_sync() -> None:
    validator = Phase1Validator(CONFIG, _logger())
    client = _RecordingClient()
    validator.client = client  # type: ignore[assignment]
    asyncio.run(validator._subscribe_characteristics())
    assert set(client.subscribed) == set(
        subscribable_characteristics(_bringup()["bringup"]).values()
    )


def test_gyro_is_written_in_the_declared_dps_unit() -> None:
    # The research client decodes the wire to rad/s; the XDF header declares dps.
    converted = Phase1Validator._contract_units("SYNAPSE_GYRO_T0", [1.0])
    assert converted == pytest.approx([180.0 / math.pi])
    # ECG keeps its mV -> uV boundary conversion.
    assert Phase1Validator._contract_units("SYNAPSE_ECG_T0", [1.0]) == pytest.approx([1000.0])


def test_a_calibrated_cell_capacity_overrides_the_manifest_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    validator = Phase1Validator(CONFIG, _logger())
    validator.device_serial = "SYN-ABCDEF123456"
    record_path = tmp_path / "SYN-ABCDEF123456.json"
    record_path.write_text(
        json.dumps(
            {
                "serial": "SYN-ABCDEF123456",
                "battery_mah": 1500,
                "ecg_gain_x100": 110000,
                "ppg_led_red": 31,
                "ppg_led_ir": 31,
                "date": "2026-11-28",
                "operator": "A. Brillo",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "_calibration_source", lambda: str(record_path))
    expected = validator._expected_flashed_config()
    assert expected["battery_mah"] == 1500


def test_session_overlay_is_allowlisted(tmp_path: Path) -> None:
    """A failing run must not be able to rewrite its own rates or gates."""
    path = tmp_path / "session_overrides.yaml"
    path.write_text(yaml.safe_dump({"bringup": {"sampling_rate": {"ecg": 250}}}), encoding="utf-8")
    with pytest.raises(ValueError, match="may only contain 'validation'"):
        load_session_overrides(path)

    path.write_text(yaml.safe_dump({"validation": {"required_gates": []}}), encoding="utf-8")
    with pytest.raises(ValueError, match="manual_hr_bpm"):
        load_session_overrides(path)


def test_session_overlay_resolves_reference_relative_to_itself(tmp_path: Path) -> None:
    path = tmp_path / "session_overrides.yaml"
    path.write_text(
        yaml.safe_dump(
            {"validation": {"ground_truth_ecg_path": "refs/r_peaks.npz", "verified_by": "A. B"}}
        ),
        encoding="utf-8",
    )
    loaded = load_session_overrides(path)
    assert loaded["ground_truth_ecg_path"] == str(tmp_path / "refs" / "r_peaks.npz")
    assert loaded["verified_by"] == "A. B"


def test_manual_hr_in_overlay_requires_a_verifier(tmp_path: Path) -> None:
    path = tmp_path / "session_overrides.yaml"
    path.write_text(yaml.safe_dump({"validation": {"manual_hr_bpm": 68}}), encoding="utf-8")
    with pytest.raises(ValueError, match="requires verified_by"):
        load_session_overrides(path)


def test_validator_merges_the_overlay_into_validation_config(tmp_path: Path) -> None:
    path = tmp_path / "session_overrides.yaml"
    path.write_text(
        yaml.safe_dump({"validation": {"manual_hr_bpm": 71, "verified_by": "A. B"}}),
        encoding="utf-8",
    )
    validator = Phase1Validator(CONFIG, _logger(), session_overrides=path)
    assert validator.config["bringup"]["validation"]["manual_hr_bpm"] == 71
    assert validator.config["_session_overrides"] == str(path)
    # The tracked contract is untouched in memory and on disk.
    assert _bringup()["bringup"]["validation"]["manual_hr_bpm"] is None


def test_anchor_uses_the_injected_lsl_clock() -> None:
    """A rehearsal clock must anchor the same domain as its own XDF samples."""
    manager = LSLStreamManager()
    try:
        manager.create_outlets(
            [
                {
                    "name": "SYNAPSE_ANCHOR_TEST",
                    "type": "ECG",
                    "channel_count": 1,
                    "sampling_rate": 500,
                    "channel_format": "float32",
                    "source_id": "anchor-test",
                }
            ],
            clock_fn=lambda: 42.0,
        )
        assert manager._session_lsl_t0_s == 42.0
    finally:
        manager.close()


def test_validator_sees_the_session_anchor_captured_by_the_manager() -> None:
    validator = Phase1Validator(CONFIG, _logger())
    validator.lsl_manager._session_utc0_s = 1_735_689_600.5
    validator.lsl_manager._session_lsl_t0_s = 12.25
    assert validator._session_utc0_s == 1_735_689_600.5
    assert validator._session_lsl_t0_s == 12.25


def test_a_live_refusal_leaves_a_failed_attempt_in_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.validate_phase1_entry as v

    gate_report = tmp_path / "gate_report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_phase1_entry.py",
            "--live-hardware",
            "--gate-report",
            str(gate_report),
        ],
    )
    # main() parses argv and calls main_async; a live run without --manifest is
    # refused before any device is touched, but the attempt must be recorded.
    assert v.main() == 2
    history = (tmp_path / "history.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in history.splitlines() if line.strip()]
    assert rows
    assert rows[-1]["overall_pass"] is False
    detail = json.loads(Path(rows[-1]["gate_report"]).read_text(encoding="utf-8"))
    assert "manifest" in detail["failure"]
