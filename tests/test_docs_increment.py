"""The MVP-1.3 closing document keeps its honesty (a closure that overstates is worse than none).

The keywords are ASCII on purpose: a typographic change to the prose must not fail a docs-only edit.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "increments" / "MVP1_3_FIRST_LIGHT.md"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_closure_says_what_was_not_run() -> None:
    text = _text()
    for needle in (
        "NON eseguito",
        "Nessuna esecuzione su scheda",
        "Woodpecker",
        "Debito dichiarato",
        'non "verificato"',
    ):
        assert needle in text, f"the closure lost: {needle}"
    assert "hardware" in text
    assert "skip" in text


def test_the_closure_lists_every_pull_request_of_the_increment() -> None:
    rows = {int(number) for number in re.findall(r"^\| #(\d+) \|", _text(), flags=re.MULTILINE)}
    assert rows == set(range(55, 66)), sorted(rows)


def test_the_previous_increment_points_at_the_renumbering() -> None:
    previous = (ROOT / "docs" / "increments" / "MVP1_2_DEVICE_PATH.md").read_text(encoding="utf-8")
    assert "MVP1_3_FIRST_LIGHT.md" in previous
    assert "MVP-1.4" in previous


def test_the_gate_surfaces_the_reason_of_every_skipped_c_host_test() -> None:
    """A skip is not a pass: the host C gate step must print why a test skipped (-rs)."""
    for path in (ROOT / ".woodpecker" / "ci.yml", ROOT / "scripts" / "run_gate_locally.sh"):
        step = next(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if "test_research_service_host" in line
        )
        assert " -rs " in step, f"{path.name}: the host C gate no longer prints skip reasons"
