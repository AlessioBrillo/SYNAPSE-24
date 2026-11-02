"""Backend hardening (MVP-1.3 PR-E2a): what the audit found and the happy-path tests did not.

* an unknown session answered HTTP 200 with an ``{"error": ...}`` body;
* the API leaked the server's absolute paths (session record, report, status, WebSocket);
* ``/ws/live`` could not be authenticated from a browser (header-only token);
* ``/api/config`` read ``config/hardware.yaml`` from the working directory;
* the server bound to every interface by default, with the API open when no token is set;
* the container image did not install the HTTP server it starts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend import main as main_module
from backend.main import app

try:
    from fastapi.testclient import TestClient  # type: ignore
except ImportError:  # pragma: no cover
    TestClient = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(TestClient is None, reason="fastapi is not installed")


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from tests.test_backend_api import _write_minimal_fit, _write_minimal_xdf

    monkeypatch.setenv("SYNAPSE_SESSIONS_DIR", str(tmp_path))
    monkeypatch.delenv("SYNAPSE_API_TOKEN", raising=False)
    _write_minimal_xdf(tmp_path / "session_ecg.xdf")
    _write_minimal_fit(tmp_path / "session_sqi.fit")
    main_module._reset_services()
    return TestClient(app)


def _first_session(client: TestClient) -> dict:
    return client.get("/api/v1/sessions").json()["sessions"][0]


# --- real 404s ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/v1/sessions/nope", "/api/v1/sessions/nope/report", "/api/quality/nope"],
)
def test_an_unknown_session_is_a_404_not_a_200_with_an_error_body(
    client: TestClient, path: str
) -> None:
    response = client.get(path)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


# --- no server path leaves the API ------------------------------------------------------


def test_the_session_record_shows_a_relative_path(client: TestClient, tmp_path: Path) -> None:
    session = client.get(f"/api/v1/sessions/{_first_session(client)['session_id']}").json()
    assert not Path(session["path"]).is_absolute()
    assert str(tmp_path) not in str(session)
    assert (tmp_path / session["path"]).is_file()


def test_the_report_does_not_leak_the_absolute_path(client: TestClient, tmp_path: Path) -> None:
    report = client.get(f"/api/v1/sessions/{_first_session(client)['session_id']}/report").json()
    assert not Path(report["raw_ref"]["path"]).is_absolute()
    assert str(tmp_path) not in str(report)


def test_the_status_neither_leaks_the_directory_nor_invents_a_tier(
    client: TestClient, tmp_path: Path
) -> None:
    status = client.get("/api/status").json()
    assert "sessions_dir" not in status
    assert status["current_tier"] is None  # software-only mode has no acquisition tier
    assert str(tmp_path) not in str(status)


def test_a_job_accepts_the_relative_path_the_api_returned(client: TestClient) -> None:
    session = next(
        s for s in client.get("/api/v1/sessions").json()["sessions"] if s["format"] == "fit"
    )
    path = client.get(f"/api/v1/sessions/{session['session_id']}").json()["path"]
    response = client.post(
        "/api/v1/jobs/run", json={"computation_type": "sqi_report", "file_path": path}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


@pytest.mark.parametrize("candidate", ["../outside.fit", "sub/../../outside.fit"])
def test_a_relative_path_cannot_climb_out_of_the_sessions_dir(
    client: TestClient, tmp_path: Path, candidate: str
) -> None:
    (tmp_path.parent / "outside.fit").write_bytes(b"x")
    response = client.post(
        "/api/v1/jobs/run", json={"computation_type": "sqi_report", "file_path": candidate}
    )
    assert response.json()["error"] == "path outside allowed roots"


# --- a browser can authenticate the live socket -----------------------------------------


def test_the_websocket_accepts_the_bearer_subprotocol(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_API_TOKEN", "s3cr3t")
    with client.websocket_connect("/ws/live", subprotocols=["bearer", "s3cr3t"]) as ws:
        assert ws.accepted_subprotocol == "bearer"
        message = ws.receive_json()
    assert message["type"] == "status_update"
    assert "sessions_dir" not in message["data"]


def test_the_websocket_still_accepts_the_authorization_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SYNAPSE_API_TOKEN", "s3cr3t")
    with client.websocket_connect("/ws/live", headers={"Authorization": "Bearer s3cr3t"}) as ws:
        assert ws.receive_json()["type"] == "status_update"


@pytest.mark.parametrize(
    ("url", "subprotocols"),
    [
        ("/ws/live", ["bearer", "wrong"]),
        ("/ws/live", []),
        ("/ws/live?token=s3cr3t", []),  # a query token would end up in proxy logs: refused
    ],
)
def test_the_websocket_refuses_a_missing_or_wrong_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, url: str, subprotocols: list[str]
) -> None:
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setenv("SYNAPSE_API_TOKEN", "s3cr3t")
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        client.websocket_connect(url, subprotocols=subprotocols),
    ):
        pass
    assert exc.value.code == 1008


# --- deployment ------------------------------------------------------------------------


def test_the_hardware_manifest_is_found_from_any_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = main_module._read_hardware_config()
    assert "error" not in config
    assert "ble" in config


def test_the_server_binds_to_loopback_unless_told_otherwise() -> None:
    source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    call = source[source.index("uvicorn.run(") :].splitlines()[0]
    assert "0.0.0.0" not in call  # noqa: S104 - asserting its absence
    assert 'os.environ.get(HOST_ENV, "127.0.0.1")' in call
    assert main_module.HOST_ENV == "SYNAPSE_API_HOST"


def test_the_image_installs_the_server_it_starts() -> None:
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")
    for package in ("fastapi", "uvicorn[standard]"):
        pin = re.search(rf"^{re.escape(package)}(>=[\d.]+)", requirements, re.MULTILINE)
        assert pin, f"{package} is not pinned in requirements.txt"
        assert f'"{package}{pin.group(1)}"' in dockerfile, f"Dockerfile does not install {package}"
