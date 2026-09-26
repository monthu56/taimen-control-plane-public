import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    assign_capability,
    assign_role,
    assign_skill,
    auth,
    create_agent_with_key,
    create_capability,
    create_role,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
    register_skill,
)


async def test_role_lifecycle(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "eng")

    global_role = await create_role(client, admin_key, "engineer")
    scoped_role = await create_role(client, admin_key, "engineer", workspace_id=workspace["id"])
    assert scoped_role["workspaceId"] == workspace["id"]

    # Duplicate slug in the same scope -> 409
    response = await client.post(
        "/api/v1/roles", json={"slug": "engineer", "name": "E"}, headers=auth(admin_key)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "role_slug_conflict"

    # Update with optimistic concurrency
    response = await client.patch(
        f"/api/v1/roles/{global_role['id']}",
        json={"description": "Builds things"},
        headers={**auth(admin_key), "If-Match": '"role-1"'},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2

    response = await client.patch(
        f"/api/v1/roles/{global_role['id']}",
        json={"description": "stale"},
        headers={**auth(admin_key), "If-Match": '"role-1"'},
    )
    assert response.status_code == 409


async def test_role_assignment_lifecycle(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, _ = await create_agent_with_key(client, admin_key)
    role = await create_role(client, admin_key, "reviewer")

    await assign_role(client, admin_key, principal["id"], role["id"])
    # Idempotent re-assign
    await assign_role(client, admin_key, principal["id"], role["id"])

    items = (
        await client.get(f"/api/v1/principals/{principal['id']}/roles", headers=auth(admin_key))
    ).json()["items"]
    assert len(items) == 1
    assert items[0]["role"]["slug"] == "reviewer"
    assert items[0]["workspaceId"] is None

    # Revoke (204), then revoke again (no-op 204)
    assert (
        await client.post(
            f"/api/v1/principals/{principal['id']}/roles/{role['id']}:revoke",
            headers=auth(admin_key),
        )
    ).status_code == 204
    assert (
        await client.post(
            f"/api/v1/principals/{principal['id']}/roles/{role['id']}:revoke",
            headers=auth(admin_key),
        )
    ).status_code == 204
    items = (
        await client.get(f"/api/v1/principals/{principal['id']}/roles", headers=auth(admin_key))
    ).json()["items"]
    assert items == []


async def test_capability_lifecycle(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, _ = await create_agent_with_key(client, admin_key)

    capability = await create_capability(client, admin_key, "code.python")
    response = await client.post(
        "/api/v1/capabilities", json={"name": "code.python"}, headers=auth(admin_key)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "capability_exists"

    # Assign with metadata
    response = await client.post(
        f"/api/v1/principals/{principal['id']}/capabilities",
        json={"capabilityId": capability["id"], "metadata": {"level": "advanced"}},
        headers=auth(admin_key),
    )
    assert response.status_code == 201
    assert response.json()["metadata"] == {"level": "advanced"}

    items = (
        await client.get(
            f"/api/v1/principals/{principal['id']}/capabilities", headers=auth(admin_key)
        )
    ).json()["items"]
    assert items[0]["capability"]["name"] == "code.python"

    assert (
        await client.post(
            f"/api/v1/principals/{principal['id']}/capabilities/{capability['id']}:revoke",
            headers=auth(admin_key),
        )
    ).status_code == 204


async def test_skill_lifecycle(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, _ = await create_agent_with_key(client, admin_key)

    skill = await register_skill(
        client,
        admin_key,
        "github.create_pr",
        protocol="mcp",
        config={"server": "github"},
    )
    # Same name+version -> 409; new version ok
    response = await client.post(
        "/api/v1/skills",
        json={"name": "github.create_pr", "protocol": "mcp"},
        headers=auth(admin_key),
    )
    assert response.status_code == 409
    await register_skill(client, admin_key, "github.create_pr", protocol="mcp", version="2.0.0")

    # Invalid protocol -> 422
    response = await client.post(
        "/api/v1/skills",
        json={"name": "x", "protocol": "carrier-pigeon"},
        headers=auth(admin_key),
    )
    assert response.status_code == 422

    # Update with If-Match on row_version
    response = await client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"status": "deprecated"},
        headers={**auth(admin_key), "If-Match": '"skill-1"'},
    )
    assert response.status_code == 200
    assert response.json()["rowVersion"] == 2
    assert response.json()["status"] == "deprecated"

    # Assign / list / revoke
    await assign_skill(client, admin_key, principal["id"], skill["id"])
    items = (
        await client.get(f"/api/v1/principals/{principal['id']}/skills", headers=auth(admin_key))
    ).json()["items"]
    assert items[0]["skill"]["name"] == "github.create_pr"
    assert (
        await client.post(
            f"/api/v1/principals/{principal['id']}/skills/{skill['id']}:revoke",
            headers=auth(admin_key),
        )
    ).status_code == 204

    # Disabled skills cannot be assigned
    response = await client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"status": "disabled"},
        headers={**auth(admin_key), "If-Match": '"skill-2"'},
    )
    assert response.status_code == 200
    response = await client.post(
        f"/api/v1/principals/{principal['id']}/skills",
        json={"skillId": skill["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "skill_disabled"


async def test_org_permissions_enforced(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, limited_key = await create_agent_with_key(
        client, admin_key, permissions=["tasks.read"]
    )

    assert (await client.get("/api/v1/roles", headers=auth(limited_key))).status_code == 403
    assert (
        await client.post(
            "/api/v1/roles", json={"slug": "rr", "name": "R"}, headers=auth(limited_key)
        )
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/principals/{principal['id']}/roles", headers=auth(limited_key))
    ).status_code == 403

    role = await create_role(client, admin_key, "rr")
    assert (
        await client.post(
            f"/api/v1/principals/{principal['id']}/roles",
            json={"roleId": role["id"]},
            headers=auth(limited_key),
        )
    ).status_code == 403


async def test_org_tenant_isolation(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    role = await create_role(client, admin_key, "engineer")
    capability = await create_capability(client, admin_key, "code.python")
    skill = await register_skill(client, admin_key, "web.search")
    principal_a = body["adminPrincipal"]["id"]

    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    for url in (
        f"/api/v1/roles/{role['id']}",
        f"/api/v1/capabilities/{capability['id']}",
        f"/api/v1/skills/{skill['id']}",
    ):
        assert (await client.get(url, headers=auth(key_b))).status_code == 404, url

    # Cannot assign tenant A's role to anything from tenant B
    assert (
        await client.post(
            f"/api/v1/principals/{principal_a}/roles",
            json={"roleId": role["id"]},
            headers=auth(key_b),
        )
    ).status_code == 404

    # Assignment helpers reject cross-tenant capability ids too
    await assign_capability(client, admin_key, principal_a, capability["id"])
    assert (await client.get("/api/v1/roles", headers=auth(key_b))).json()["items"] == []


async def test_harness_context_lists_assigned_capabilities(client: httpx.AsyncClient) -> None:
    """Регресс BO-03 (BidOps): у principal с назначенной capability
    ``GET /harness/context`` падал с 500 — строки capability читались как Row,
    а не как ORM-объекты (``.all()`` без ``.scalars()``)."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, agent_key = await create_agent_with_key(client, admin_key)
    capability = await create_capability(client, admin_key, "requirement.write")
    await assign_capability(client, admin_key, principal["id"], capability["id"])

    response = await client.get("/api/v1/harness/context", headers=auth(agent_key))
    assert response.status_code == 200, response.text
    names = [c["name"] for c in response.json()["capabilities"]]
    assert names == ["requirement.write"]
    assert response.json()["capabilities"][0]["id"] == capability["id"]
