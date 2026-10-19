"""Regression guard for the BLE GATT server duplication bug.

The firmware bug fixed in PR0 (feat/device-loop-v1) was that
``ble_gatt_server.c`` defined ``body_loc_access`` and ``batt_access`` twice
(with identical bodies), which makes every ``idf.py build`` fail with
redefinition errors. The bug had survived review because no host gate
compiled ``ble_gatt_server.c`` and the ESP-IDF build was never run on the
committed tree.

This test is compiler-free: it scans the source text and asserts each access
callback is defined exactly once. It runs in CI on every push so the
duplication cannot be reintroduced silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATT_SERVER = ROOT / "firmware" / "esp32_band" / "ble" / "ble_gatt_server.c"


def _count_static_definitions(source: str, name: str) -> int:
    """Count top-level ``static int <name>(`` definitions in the source."""
    import re

    pattern = rf"static\s+int\s+{name}\s*\("
    return len(re.findall(pattern, source))


def test_body_loc_access_defined_once() -> None:
    """body_loc_access must have exactly one definition."""
    source = GATT_SERVER.read_text(encoding="utf-8")
    count = _count_static_definitions(source, "body_loc_access")
    assert count == 1, f"body_loc_access defined {count} times (expected 1)"


def test_batt_access_defined_once() -> None:
    """batt_access must have exactly one definition."""
    source = GATT_SERVER.read_text(encoding="utf-8")
    count = _count_static_definitions(source, "batt_access")
    assert count == 1, f"batt_access defined {count} times (expected 1)"


def test_gatt_server_file_exists() -> None:
    """The gate must not silently pass on a missing file."""
    assert GATT_SERVER.is_file(), f"missing firmware file: {GATT_SERVER}"
