import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    backdate_expiry,
    create_agent_with_key,
    do_bootstrap,
    open_session,
)


async def test_session_lifecycle(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)

    session = await open_session(client, agent_key, clientVersion="1.2.3")
    assert session["status"] == "active"
    assert session["expiresAt"] > session["startedAt"]

    # Heartbeat extends the lease.
    response = await client.post(
        f"/api/v1/sessions/{session['id']}:heartbeat", headers=auth(agent_key)
    )
    assert response.status_code == 200
    assert response.json()["expiresAt"] >= session["expiresAt"]

    # Close is terminal and idempotent.
    response = await client.post(f"/api/v1/sessions/{session['id']}:close", headers=auth(agent_key))
    assert response.status_code == 200
    assert response.json()["status"] == "closed"
    response = await client.post(f"/api/v1/sessions/{session['id']}:close", headers=auth(agent_key))
    assert response.status_code == 200

    # Heartbeat of a closed session is a conflict.
    response = await client.post(
        f"/api/v1/sessions/{session['id']}:heartbeat", headers=auth(agent_key)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_not_active"


async def test_expired_session_heartbeat_marks_stale(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)

    backdate_expiry(sync_engine, "sessions", session["id"])

    response = await client.post(
        f"/api/v1/sessions/{session['id']}:heartbeat", headers=auth(agent_key)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_expired"

    response = await client.get(f"/api/v1/sessions/{session['id']}", headers=auth(agent_key))
    assert response.json()["status"] == "stale"


async def test_session_access_control(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_a = await create_agent_with_key(client, admin_key, name="a")
    _, agent_b = await create_agent_with_key(client, admin_key, name="b")
    session = await open_session(client, agent_a)

    # Another principal without sessions.manage cannot touch it.
    response = await client.post(
        f"/api/v1/sessions/{session['id']}:heartbeat", headers=auth(agent_b)
    )
    assert response.status_code == 403
    # Admin (sessions.manage via admin) can.
    response = await client.post(
        f"/api/v1/sessions/{session['id']}:heartbeat", headers=auth(admin_key)
    )
    assert response.status_code == 200

    # Listing sessions needs sessions.manage.
    assert (await client.get("/api/v1/sessions", headers=auth(agent_b))).status_code == 403
    response = await client.get("/api/v1/sessions", headers=auth(admin_key))
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_delegation_flow(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    human_id = body["adminPrincipal"]["id"]
    agent, agent_key = await create_agent_with_key(client, admin_key)

    # Without a delegation, on-behalf session opening is rejected.
    response = await client.post(
        "/api/v1/sessions",
        json={"clientName": "agent", "onBehalfOf": human_id},
        headers=auth(agent_key),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "delegation_required"

    response = await client.post(
        "/api/v1/delegations",
        json={
            "humanPrincipalId": human_id,
            "agentPrincipalId": agent["id"],
            "permissions": ["tasks.write"],
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 201, response.text
    delegation = response.json()

    session = await open_session(client, agent_key, onBehalfOf=human_id)
    assert session["onBehalfOfId"] == human_id
    assert session["delegationId"] == delegation["id"]

    # Revoked delegation stops new on-behalf sessions.
    response = await client.post(
        f"/api/v1/delegations/{delegation['id']}:revoke", headers=auth(admin_key)
    )
    assert response.status_code == 200
    response = await client.post(
        "/api/v1/sessions",
        json={"clientName": "agent", "onBehalfOf": human_id},
        headers=auth(agent_key),
    )
    assert response.status_code == 403


async def test_delegation_direction_validation(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    human_id = body["adminPrincipal"]["id"]
    agent, _ = await create_agent_with_key(client, admin_key)

    response = await client.post(
        "/api/v1/delegations",
        json={"humanPrincipalId": agent["id"], "agentPrincipalId": human_id},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_delegation"
