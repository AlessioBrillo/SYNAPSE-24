"""A Phase-0 gate with no executed assertions must never be reported green."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import validate_phase0_exit as p0  # noqa: E402


def test_an_all_skipped_gate_is_unverified_not_a_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        p0, "GATE_TESTS", {"data": {"threshold": "n/a", "tests": [], "provenance": "x"}}
    )
    monkeypatch.setattr(p0, "run_gate_tests", lambda _name, _tests: (False, 0, 0, 3))

    gates, summaries, overall = p0._run_gate_tests()

    assert gates[0].value == "UNVERIFIED"
    assert gates[0].passed is False
    assert summaries["data"]["skipped_count"] == 3
    assert overall is False
