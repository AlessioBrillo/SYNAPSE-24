"""Contract 2.4 writes are narrow and only committed after readback."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "device_config_write", ROOT / "scripts" / "device_config_write.py"
)
assert _spec is not None
assert _spec.loader is not None
dcw = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = dcw
_spec.loader.exec_module(dcw)


def _args(**kwargs: object) -> argparse.Namespace:
    values = {"led": None, "gain": None, "feature_rate": None}
    values.update(kwargs)
    return argparse.Namespace(**values)


def test_only_whitelisted_fields_are_written() -> None:
    assert json.loads(dcw.build_payload(_args(led="1F,20"))) == {"led_red": 31, "led_ir": 32}
    assert json.loads(dcw.build_payload(_args(gain=1100))) == {"gain_x100": 110000}
    assert json.loads(dcw.build_payload(_args(feature_rate=2))) == {"feature_rate_hz": 2}
    with pytest.raises(ValueError, match="nothing to write"):
        dcw.build_payload(_args())


def test_local_record_is_not_touched_on_serial_mismatch() -> None:
    written: list[tuple[Path, str]] = []
    sink = lambda path, text: written.append((path, text))  # noqa: E731
    with pytest.raises(ValueError, match="cal_serial"):
        dcw.update_local_record({"cal_serial": "SYN-OTHER"}, "SYN-MINE", sink)
    assert written == []


def test_local_record_is_updated_after_a_matching_readback() -> None:
    written: list[tuple[Path, str]] = []
    sink = lambda path, text: written.append((path, text))  # noqa: E731
    path = dcw.update_local_record(
        {
            "cal_serial": "SYN-MINE",
            "ecg_gain_x100": 112300,
            "ppg_led_red": 31,
            "ppg_led_ir": 32,
            "cal_state": "nvs",
            "cal_date": "2026-11-26",
            "cal_operator": "tester",
        },
        "SYN-MINE",
        sink,
    )
    assert len(written) == 1
    assert path.name == "SYN-MINE.json"
    payload = json.loads(written[0][1])
    assert payload["ecg_gain_x100"] == 112300
