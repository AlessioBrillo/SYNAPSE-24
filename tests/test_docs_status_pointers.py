"""One status file: the others must point at it, not keep a stale copy."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_status_is_the_source_of_truth() -> None:
    """The latest increment comes from STATUS.md, not from a pinned literal."""
    status = _text("docs/hardware/STATUS.md")
    # STATUS.md writes the label in bold: "**Latest increment:** MVP-2.7 ...",
    # so skip the markdown asterisks before reading the increment id.
    match = re.search(r"Latest increment:\s*\**\s*(\S+)", status)
    assert match is not None, "STATUS.md must declare 'Latest increment:'"
    increment = match.group(1).strip("*")
    assert increment.startswith("MVP-"), increment
    assert "G1" in status

    prefix = "MVP" + increment.removeprefix("MVP-").replace(".", "_")
    increments = sorted((ROOT / "docs/increments").glob(f"{prefix}_*.md"))
    assert increments, f"no docs/increments/{prefix}_*.md for {increment}"


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
