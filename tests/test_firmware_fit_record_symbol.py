"""Regression guard for the FIT writer symbol mismatch bug.

The firmware bug fixed in PR0 (feat/device-loop-v1) was that
``synapse_fit_write_from_feature()`` called ``synapse_fit_write_record()`` but
the function was defined as ``static write_record()`` — a name mismatch that
made every ``idf.py build`` fail with an implicit-declaration error.

This test is compiler-free: it scans ``fit_writer.c`` and asserts that the
internal record writer is defined and called under the same name, so the
mismatch cannot be reintroduced silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIT_WRITER = ROOT / "firmware" / "esp32_band" / "fit" / "fit_writer.c"


def test_fit_write_record_defined_and_called() -> None:
    """synapse_fit_write_record must be defined and called with one name."""
    source = FIT_WRITER.read_text(encoding="utf-8")

    defs = source.count("static esp_err_t synapse_fit_write_record(")
    calls = source.count("synapse_fit_write_record(")

    assert FIT_WRITER.is_file(), f"missing firmware file: {FIT_WRITER}"
    assert defs == 1, f"synapse_fit_write_record defined {defs} times (expected exactly 1)"
    assert calls >= 2, (
        f"synapse_fit_write_record called {calls} times (expected definition + "
        "at least one call site)"
    )


def test_no_stale_write_record_name() -> None:
    """The old, misspelled name ``write_record`` must not remain as a definition."""
    source = FIT_WRITER.read_text(encoding="utf-8")
    assert "static esp_err_t write_record(" not in source, (
        "stale static definition 'write_record' still present; expected 'synapse_fit_write_record'"
    )
