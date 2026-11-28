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
    Phase1Validator,
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
