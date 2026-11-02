"""The wiring documents follow the firmware's pin map (MVP-1.3 PR-F1).

The bring-up documents are what a person wires a board from. They had drifted from the code to
pins that do not exist or that wedge the module (GPIO 27/34/36 are flash/PSRAM on the N8R8; the
guide even gave I2C as 21/22 while the SOP and the firmware said 8/9). A wrong pin in a wiring
table is not a typo: it is a dead board. So the documents are checked against the same pin map the
firmware tests assert, and no table row may use a reserved pin.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_firmware_first_light import _band_gpio_map

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "HARDWARE_BRINGUP_GUIDE.md"
SOP = ROOT / "docs" / "hardware" / "BRINGUP_SOP.md"
PROCUREMENT = ROOT / "docs" / "hardware" / "PROCUREMENT.md"

#: SPI flash (26-32) and octal PSRAM (33-37) on the DevKitC-1 N8R8.
RESERVED = set(range(26, 38))


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _table_rows(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("|")]


def test_no_wiring_table_row_uses_a_flash_or_psram_pin() -> None:
    for path in (GUIDE, SOP, PROCUREMENT):
        for row in _table_rows(_text(path)):
            for match in re.finditer(r"\bGPIO ?(\d+)\b", row):
                pin = int(match.group(1))
                warned = "not " in row or "n/c" in row
                assert pin not in RESERVED or warned, (
                    f"{path.name}: a table row wires GPIO {pin}: {row.strip()}"
                )


def test_the_guide_pinout_table_matches_the_firmware_pin_map() -> None:
    pins = _band_gpio_map()
    table = "\n".join(_table_rows(_text(GUIDE)))
    expected = {
        "ECG ADC": pins["ecg_adc"],
        "ECG lead-off": pins["ecg_leadoff"],
        "Battery divider": pins["bat_adc"],
        "I2C SDA": pins["i2c_sda"],
        "I2C SCL": pins["i2c_scl"],
        "PPG INT": pins["ppg_int"],
        "IMU INT": pins["imu_int"],
        "Wired Sync": pins["wired_sync"],
    }
    for signal, pin in expected.items():
        row = next((r for r in table.splitlines() if r.startswith(f"| {signal}")), None)
        assert row is not None, f"the pinout table lost its {signal} row"
        assert f"GPIO{pin}" in row, (
            f"{signal}: the firmware uses GPIO{pin}, the guide says: {row.strip()}"
        )


def test_the_sop_wiring_table_matches_the_firmware_pin_map() -> None:
    pins = _band_gpio_map()
    table = "\n".join(_table_rows(_text(SOP)))
    for signal, pin in (
        ("AD8232 OUTPUT", pins["ecg_adc"]),
        ("AD8232 LO+ (lead-off)", pins["ecg_leadoff"]),
        ("Battery divider", pins["bat_adc"]),
        ("MAX30102 INT", pins["ppg_int"]),
        ("ICM-20948 INT", pins["imu_int"]),
        ("Wired sync", pins["wired_sync"]),
    ):
        row = next((r for r in table.splitlines() if r.startswith(f"| {signal}")), None)
        assert row is not None, f"the SOP wiring table lost its {signal} row"
        assert re.search(rf"\b{pin}\b", row), (
            f"{signal}: the firmware uses GPIO {pin}, the SOP says: {row.strip()}"
        )
    sda = next(r for r in table.splitlines() if "SDA" in r)
    assert f"**{pins['i2c_sda']}**" in sda


def test_the_two_pod_bench_uses_the_real_sync_pin() -> None:
    pin = _band_gpio_map()["wired_sync"]
    assert f"GPIO {pin}" in _text(PROCUREMENT)
    assert f"GPIO {pin}" in _text(SOP)


def test_the_readiness_gate_does_not_claim_only_the_kit_is_missing() -> None:
    text = _text(ROOT / "docs" / "hardware" / "READINESS_GATE.md")
    assert "Only the physical kit and the bench measurements remain" not in text
    assert "has never run on a board" in text
