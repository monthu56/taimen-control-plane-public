"""Graph writes/reads are governed by CP, including the SDK/MCP path."""

import httpx
import pytest

from tests.helpers import auth, create_agent_with_key, do_bootstrap
from tests.integration.test_context_api_v04 import SpyProvider

ASSERTIONS = [{"assert": "entity", "entity": {"key": "company:example", "type": "company"}}]


async def test_assertions_recorded_idempotently_with_server_actor(client: httpx.AsyncClient):
    boot = await do_bootstrap(client)
    agent, key = await create_agent_with_key(client, boot["apiKey"]["key"])
    body = {"kind": "external_fact", "content": "Example", "assertions": ASSERTIONS}
    headers = {**auth(key), "Idempotency-Key": "graph-write-test"}
    first = await client.post("/api/v1/observations", json=body, headers=headers)
    second = await client.post("/api/v1/observations", json=body, headers=headers)
    assert first.status_code == 201, first.text
    assert second.json() == first.json()
    events = (await client.get("/api/v1/events", headers=auth(key))).json()["items"]
    recorded = [e for e in events if e["type"] == "observation.recorded"]
    assert len(recorded) == 1
    assert recorded[0]["actorId"] == agent["id"]
    assert recorded[0]["payload"]["assertions"][0]["entity"]["key"] == "company:example"


async def test_assertions_do_not_bypass_write_permission(client):
    boot = await do_bootstrap(client)
    _, key = await create_agent_with_key(client, boot["apiKey"]["key"], permissions=["tasks.read"])
    response = await client.post(
        "/api/v1/observations",
        headers=auth(key),
        json={
            "kind": "note",
            "content": "Example",
            "assertions": ASSERTIONS,
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize("field", ["namespace", "scope", "actor", "provenance"])
async def test_client_cannot_set_observation_authority(client, field):
    boot = await do_bootstrap(client)
    response = await client.post(
        "/api/v1/observations",
        headers=auth(boot["apiKey"]["key"]),
        json={
            "kind": "note",
            "content": "Example",
            "assertions": ASSERTIONS,
            field: "spoof",
        },
    )
    # An unknown field breaks the API contract: 400 invalid_request (as for a
    # forged authorPrincipalId on comments), not FastAPI's default 422.
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["details"]["errors"][0]["loc"] == f"body.{field}"


async def test_anchors_need_read_permission_and_cannot_change_namespace(client, app):
    boot = await do_bootstrap(client)
    _, restricted = await create_agent_with_key(
        client, boot["apiKey"]["key"], permissions=["tasks.read"]
    )
    spy = SpyProvider()
    app.state.context_provider = spy
    try:
        response = await client.post(
            "/api/v1/context",
            headers=auth(restricted),
            json={
                "anchors": ["company:example"],
            },
        )
        assert response.status_code == 200
        assert response.json()["memoryStatus"] == "forbidden"
        assert not spy.context_requests
        response = await client.post(
            "/api/v1/context",
            headers=auth(boot["apiKey"]["key"]),
            json={
                "anchors": ["company:example"],
            },
        )
        assert response.json()["memoryStatus"] == "ok"
        assert spy.context_requests[0]["anchors"][0] == "company:example"
        assert str(boot["tenant"]["id"]) in spy.context_requests[0]["namespace"]
        response = await client.post(
            "/api/v1/context",
            headers=auth(boot["apiKey"]["key"]),
            json={
                "anchors": ["company:example"],
                "namespace": "foreign",
            },
        )
        # The core derives the namespace; a client-supplied one is refused.
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"
        assert len(spy.context_requests) == 1
    finally:
        app.state.context_provider = None
