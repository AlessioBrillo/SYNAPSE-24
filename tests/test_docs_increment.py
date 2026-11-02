"""The MVP-1.3 closing document keeps its honesty (a closure that overstates is worse than none)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "increments" / "MVP1_3_FIRST_LIGHT.md"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_closure_says_what_was_not_run() -> None:
    text = _text()
    for needle in (
        "pronto per l'hardware",
        "NON eseguito, e non va dato per fatto",
        "Nessuna esecuzione su scheda",
        "check Woodpecker",
        "uno skip non è un pass",
        "Debito dichiarato",
    ):
        assert needle in text, f"the closure lost: {needle}"


def test_the_closure_lists_every_pull_request_of_the_increment() -> None:
    text = _text()
    for number in range(55, 65):
        assert f"| #{number} |" in text, f"PR #{number} is missing from the closure"


def test_the_previous_increment_points_at_the_renumbering() -> None:
    previous = (ROOT / "docs" / "increments" / "MVP1_2_DEVICE_PATH.md").read_text(encoding="utf-8")
    assert "MVP1_3_FIRST_LIGHT.md" in previous
    assert "MVP-1.4" in previous
