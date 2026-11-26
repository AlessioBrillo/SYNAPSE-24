"""One status file: the others must point at it, not keep a stale copy."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_status_is_the_source_of_truth() -> None:
    status = _text("docs/hardware/STATUS.md")
    assert "MVP-2.6" in status
    assert "G1" in status
    assert (ROOT / "docs/increments/MVP2_6_READINESS_GOVERNANCE.md").is_file()


def test_other_documents_point_to_status() -> None:
    for path in (
        "README.md",
        "Roadmap.md",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "compliance/COMPLIANCE.md",
    ):
        assert "docs/hardware/STATUS.md" in _text(path), path


def test_no_document_still_names_the_github_actions_file_as_the_gate() -> None:
    compliance = _text("compliance/COMPLIANCE.md")
    assert ".woodpecker/ci.yml" in compliance
    assert "| `tests/`, `.github/workflows/ci.yml`" not in compliance


def test_the_tester_playbook_and_matrix_exist() -> None:
    playbook = _text("docs/onboarding/TESTER_PLAYBOOK.md")
    matrix = _text("docs/testing/G1_TEST_MATRIX.md")
    assert "--manifest" in playbook
    for test_id in ("T01", "T02", "T05", "T11", "T12"):
        assert test_id in matrix
