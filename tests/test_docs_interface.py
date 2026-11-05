"""The Averyn interface document says what the code does (MVP-1.3 PR-F2).

The previous version described a flow that does not exist (Averyn importing sessions from the Data
Plane under its own source label, pages that were never built, a WebSocket query token the server
refuses). A document that other teams build against is part of the contract, so it is checked
against the running application, the contract files and the numbers it quotes.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "contracts" / "AVERYN_INTERFACE.md"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _documented_endpoints() -> set[tuple[str, str]]:
    rows = re.findall(r"^\| `(/[^`]*)` \| (GET|POST) \|", _text(), re.MULTILINE)
    return {("/api/v1" + path, method.lower()) for path, method in rows}


def test_the_endpoint_table_and_the_application_list_the_same_v1_routes() -> None:
    pytest.importorskip("fastapi")
    from backend.main import app

    served = {
        (path, method)
        for path, item in app.openapi()["paths"].items()
        if path.startswith("/api/v1/")
        for method in item
    }
    documented = _documented_endpoints()
    assert documented, "the endpoint table could not be parsed"
    assert documented - served == set(), "documented but not served"
    assert served - documented == set(), "served but not documented"


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


def test_the_numbers_the_document_quotes_match_the_contracts() -> None:
    text = _text()
    manifest = yaml.safe_load((ROOT / "config" / "hardware.yaml").read_text(encoding="utf-8"))
    v2 = json.loads((ROOT / "contracts" / "gatt_v2.json").read_text(encoding="utf-8"))
    v1_bytes = (ROOT / "contracts" / "gatt_v1.json").read_bytes().replace(b"\r\n", b"\n")
    sha = hashlib.sha256(v1_bytes).hexdigest()

    assert f"**{manifest['ble']['feature_stream']['payload_bytes']} byte**" in text  # the v1 frame
    assert f"Feature v2* (notify, {v2['feature']['bytes']} B)" in text
    assert f"Status* (read + notify, {v2['status']['bytes']} B)" in text
    assert f"{v2['feature']['bytes'] + 3}" in text  # the MTU the v2 feature needs (frame + 3)
    assert f"`{sha[:8]}…{sha[-4:]}`" in text, "the pinned sha256 quoted in the document is stale"


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


def test_the_security_statements_hold_in_the_application() -> None:
    """The document says the 501 stubs are behind the token, and that a query token is refused."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from backend import main as main_module
    from backend.main import app

    text = _text()
    assert "401" in text
    assert "prima di arrivare al 501" in text
    import os

    os.environ["SYNAPSE_API_TOKEN"] = "doc-check"
    try:
        main_module._reset_services()
        client = TestClient(app)
        assert client.get("/api/live/pod-1").status_code == 401  # token before 501
        assert (
            client.get("/api/live/pod-1", headers={"Authorization": "Bearer doc-check"}).status_code
            == 501
        )
    finally:
        os.environ.pop("SYNAPSE_API_TOKEN", None)
        main_module._reset_services()
