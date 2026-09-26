import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, create_agent_with_key, do_bootstrap


async def test_missing_and_malformed_credentials(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/tasks")).status_code == 401
    assert (
        await client.get("/api/v1/tasks", headers={"Authorization": "Basic abc"})
    ).status_code == 401
    assert (await client.get("/api/v1/tasks", headers=auth("cp_bad"))).status_code == 401
    assert (
        await client.get("/api/v1/tasks", headers=auth("cp_12345678_notreal"))
    ).status_code == 401


async def test_revoked_key_is_rejected(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, agent_key = await create_agent_with_key(client, admin_key)

    # Find the agent's key id via the event journal payload-free route: revoke
    # by listing is not exposed, so grab it from the creation response instead.
    response = await client.post(
        f"/api/v1/principals/{principal['id']}/api-keys",
        json={"permissions": ["tasks.read"]},
        headers=auth(admin_key),
    )
    key_id = response.json()["id"]
    second_key = response.json()["key"]
    assert (await client.get("/api/v1/tasks", headers=auth(second_key))).status_code == 200

    response = await client.post(f"/api/v1/api-keys/{key_id}:revoke", headers=auth(admin_key))
    assert response.status_code == 200
    assert response.json()["revokedAt"] is not None

    assert (await client.get("/api/v1/tasks", headers=auth(second_key))).status_code == 401
    # The first key still works.
    assert (await client.get("/api/v1/tasks", headers=auth(agent_key))).status_code == 200


async def test_permission_enforcement(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, readonly_key = await create_agent_with_key(
        client, admin_key, name="readonly", permissions=["tasks.read"]
    )

    response = await client.post(
        "/api/v1/tasks", json={"title": "nope"}, headers=auth(readonly_key)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"

    # principals.read is also missing
    assert (await client.get("/api/v1/principals", headers=auth(readonly_key))).status_code == 403


async def test_disabled_principal_is_rejected(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, agent_key = await create_agent_with_key(client, admin_key)

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE principals SET status = 'disabled' WHERE id = :id"),
            {"id": principal["id"]},
        )

    response = await client.get("/api/v1/tasks", headers=auth(agent_key))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "principal_not_active"


async def test_admin_cannot_be_minted_by_non_admin(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, limited_key = await create_agent_with_key(
        client, admin_key, permissions=["principals.write", "principals.read"]
    )
    response = await client.post(
        f"/api/v1/principals/{principal['id']}/api-keys",
        json={"permissions": ["admin"]},
        headers=auth(limited_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_permissions"


async def test_key_creation_cannot_escalate_permissions(client: httpx.AsyncClient) -> None:
    """A non-admin key can only grant permissions it holds itself."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, limited_key = await create_agent_with_key(
        client, admin_key, permissions=["principals.write", "tasks.read"]
    )

    response = await client.post(
        f"/api/v1/principals/{principal['id']}/api-keys",
        json={"permissions": ["tasks.claim"]},
        headers=auth(limited_key),
    )
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "permission_escalation"
    assert error["details"]["missing"] == ["tasks.claim"]

    # Granting a subset of held permissions is fine.
    response = await client.post(
        f"/api/v1/principals/{principal['id']}/api-keys",
        json={"permissions": ["tasks.read"]},
        headers=auth(limited_key),
    )
    assert response.status_code == 201
