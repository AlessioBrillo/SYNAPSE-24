"""The Averyn interface document says what the code does (MVP-1.3 PR-F2).

The previous version described a flow that does not exist (Averyn importing sessions from the Data
Plane as ``synapse-data-plane``, pages that were never built, a WebSocket query token the server
refuses). A document that other teams build against is part of the contract, so it is checked
against the running application and the files it names.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "contracts" / "AVERYN_INTERFACE.md"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_every_endpoint_in_the_table_exists_in_the_application() -> None:
    pytest.importorskip("fastapi")
    from backend.main import app

    paths = app.openapi()["paths"]
    rows = re.findall(r"^\| `(/[^`]*)` \| (GET|POST) \|", _text(), re.MULTILINE)
    assert len(rows) == 6, rows
    for path, method in rows:
        full = "/api/v1" + path
        assert full in paths, f"{full} is documented but the application has no such route"
        assert method.lower() in paths[full], f"{method} {full} is documented but not served"


def test_every_contract_file_the_document_names_exists() -> None:
    named = set(re.findall(r"`(contracts/[A-Za-z0-9_./-]+\.json)`", _text()))
    assert {
        "contracts/gatt_v1.json",
        "contracts/gatt_v2.json",
        "contracts/vectors/consumer_v2.json",
        "contracts/fit_developer_fields.json",
        "contracts/schemas/worker_job_result.json",
        "contracts/schemas/session_report.json",
    } <= named
    for name in sorted(named):
        assert (ROOT / name).is_file(), f"the document names {name}, which does not exist"


@pytest.mark.parametrize(
    "stale",
    [
        'source: "synapse-data-plane"',  # ADR-0026: Averyn never calls the Data Plane
        "/synapse/readiness",  # pages that were never built
        "Readiness/Sleep/Stress/HRV già calcolati",  # no readiness score exists anywhere
        "oppure nel query parameter",  # the server refuses a query token
        "0=wake 1=light 2=deep",  # the old sleep-stage meaning
    ],
)
def test_the_document_no_longer_makes_the_claims_the_code_contradicts(stale: str) -> None:
    assert stale not in _text()


def test_the_document_states_the_things_that_are_true_now() -> None:
    text = _text()
    for needle in (
        'new WebSocket(url, ["bearer", token])',
        "**404**",
        "**501**",
        "23 byte",
        "non chiama mai il Data Plane",
        "0=W 1=N1 2=N2 3=N3 4=REM",
        "secondi Unix",
    ):
        assert needle in text, f"the document lost: {needle}"
