"""Realtime WebSocket delivery and resume (sync TestClient drives the ASGI app)."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from control_plane.config import Settings
from control_plane.main import create_app
from tests.helpers import BOOTSTRAP_TOKEN, auth


@pytest.fixture
def tc(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _bootstrap(tc: TestClient) -> str:
    response = tc.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "A"},
        headers=auth(BOOTSTRAP_TOKEN),
    )
    assert response.status_code == 201
    return response.json()["apiKey"]["key"]


def test_ws_requires_credentials(tc: TestClient) -> None:
    # The handshake is accepted so the close code actually reaches the client,
    # then the server closes with 4401.
    with tc, tc.websocket_connect("/api/v1/events/ws") as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
        assert excinfo.value.code == 4401


def test_ws_streams_and_resumes(tc: TestClient) -> None:
    with tc:
        admin_key = _bootstrap(tc)

        with tc.websocket_connect("/api/v1/events/ws", headers=auth(admin_key)) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "tenant.bootstrapped"
            assert isinstance(hello["sequence"], int)
            assert hello["cursor"].startswith("ec1_")

            # A new mutation arrives over the socket (NOTIFY wake-up).
            tc.post("/api/v1/tasks", json={"title": "T1"}, headers=auth(admin_key))
            created = ws.receive_json()
            assert created["type"] == "task.created"
            last_seen = created["cursor"]

        # Client is offline; more events happen.
        tc.post("/api/v1/tasks", json={"title": "T2"}, headers=auth(admin_key))
        tc.post("/api/v1/tasks", json={"title": "T3"}, headers=auth(admin_key))

        # Reconnect with ?after=<opaque cursor> -> missed events arrive,
        # in order, no dupes.
        with tc.websocket_connect(
            f"/api/v1/events/ws?after={last_seen}", headers=auth(admin_key)
        ) as ws:
            first = ws.receive_json()
            second = ws.receive_json()
            assert [first["type"], second["type"]] == ["task.created", "task.created"]
            assert first["payload"]["title"] == "T2"
            assert second["payload"]["title"] == "T3"


def test_ws_accepts_legacy_integer_after(tc: TestClient) -> None:
    """v0.3 clients reconnect with ?after=<sequence>; still served."""
    with tc:
        admin_key = _bootstrap(tc)
        tc.post("/api/v1/tasks", json={"title": "T1"}, headers=auth(admin_key))

        with tc.websocket_connect("/api/v1/events/ws?after=0", headers=auth(admin_key)) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "tenant.bootstrapped"
            created = ws.receive_json()
            assert created["type"] == "task.created"


def test_ws_rejects_malformed_cursor(tc: TestClient) -> None:
    with tc:
        admin_key = _bootstrap(tc)
        with tc.websocket_connect(
            "/api/v1/events/ws?after=ec1_garbage", headers=auth(admin_key)
        ) as ws:
            with pytest.raises(WebSocketDisconnect) as excinfo:
                ws.receive_json()
            assert excinfo.value.code == 4400
