"""Reading back what a pack install writes (CP-ADR-0060, amendment 2026-09-30).

``GET /workspaces/{id}/knowledge-packs`` answers with the set and strictness a
``PUT`` left on the tree's namespace, ``GET /knowledge/packs/{ref}`` with a
registered pack version. Both are reads: rights to read, no journal events.
"""

import httpx
from fastapi import FastAPI

from tests.helpers import FakeKnowledge, auth, create_agent_with_key, create_workspace, do_bootstrap


async def _events(client: httpx.AsyncClient, key: str) -> list[str]:
    items = (await client.get("/api/v1/events", params={"limit": 200}, headers=auth(key))).json()[
        "items"
    ]
    return [e["type"] for e in items if e["type"].startswith("knowledge.")]


async def test_the_set_of_a_tree_reads_as_the_put_left_it(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    root = await create_workspace(client, admin_key, "root")
    child = await create_workspace(client, admin_key, "child", parent_id=root["id"])
    _, reader = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["workspaces.read"]
    )
    _, outsider = await create_agent_with_key(
        client, admin_key, name="outsider", permissions=["tasks.read"]
    )
    path = f"/api/v1/workspaces/{root['id']}/knowledge-packs"
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        before = await client.get(path, headers=auth(reader))
        put = await client.put(
            path,
            json={"packs": ["selfdev@1", "tenant:fleet@2"], "strict": True},
            headers=auth(admin_key),
        )
        after = await client.get(path, headers=auth(reader))
        from_child = await client.get(
            f"/api/v1/workspaces/{child['id']}/knowledge-packs", headers=auth(reader)
        )
        denied = await client.get(path, headers=auth(outsider))
        app.state.context_provider = FakeKnowledge(fail_status=500, retryable=True)
        down = await client.get(path, headers=auth(reader))
        app.state.context_provider = None
        disabled = await client.get(path, headers=auth(reader))
    finally:
        app.state.context_provider = None
    assert before.status_code == 200, before.text
    assert before.json() == {
        "workspaceId": root["id"],
        "rootWorkspaceId": root["id"],
        "configured": False,
        "packs": [],
        "strict": False,
        "effective": ["default@1"],
        "updatedAt": None,
    }
    assert put.status_code == 200, put.text
    assert after.status_code == 200, after.text
    assert after.json()["configured"] is True
    assert after.json()["packs"] == ["selfdev@1", "tenant:fleet@2"]
    assert after.json()["strict"] is True
    assert after.json()["effective"] == ["selfdev@1", "tenant:fleet@2"]
    assert after.json()["updatedAt"] is not None
    # A sub-workspace works under its root's set.
    assert from_child.status_code == 200, from_child.text
    assert from_child.json()["workspaceId"] == child["id"]
    assert from_child.json()["rootWorkspaceId"] == root["id"]
    assert from_child.json()["packs"] == after.json()["packs"]
    assert denied.status_code == 403
    assert down.status_code == 502
    assert down.json()["error"]["code"] == "memory_unavailable"
    assert disabled.status_code == 503
    # Every read went to the root's namespace; the refused one never reached Memory.
    reads = [call for kind, call in fake.calls if kind == "read_kinds"]
    assert len(reads) == 3
    assert {call["namespace"] for call in reads} == {f"tenant:{tenant_id}:ws:{root['id']}"}
    # A read is not a fact: only the PUT is journaled.
    assert await _events(client, admin_key) == ["knowledge.packs_configured"]


async def test_the_set_of_an_unknown_workspace_is_not_found(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        response = await client.get(
            "/api/v1/workspaces/00000000-0000-0000-0000-00000000dead/knowledge-packs",
            headers=auth(admin_key),
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 404
    assert fake.calls == []


async def test_the_set_of_an_archived_workspace_is_unprocessable(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "atlas")
    archived = await client.post(
        f"/api/v1/workspaces/{workspace['id']}:archive", headers=auth(admin_key)
    )
    assert archived.status_code == 200, archived.text
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        response = await client.get(
            f"/api/v1/workspaces/{workspace['id']}/knowledge-packs", headers=auth(admin_key)
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_archived"
    assert fake.calls == []


async def test_a_registered_pack_reads_back(client: httpx.AsyncClient, app: FastAPI) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    tenant_id = boot["tenant"]["id"]
    _, reader = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["events.read"]
    )
    _, manager = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["knowledge.packs.manage"]
    )
    _, outsider = await create_agent_with_key(
        client, admin_key, name="outsider", permissions=["workspaces.read"]
    )
    settings = app.state.settings
    fake = FakeKnowledge()
    app.state.context_provider = fake
    shared = {"name": "selfdev", "version": "1", "kinds": [{"kind": "module"}]}
    tenant = {"name": "fleet", "version": 2, "scope": "tenant", "kinds": [{"kind": "vehicle"}]}

    async def read(key: str, ref: str) -> httpx.Response:
        return await client.get(f"/api/v1/knowledge/packs/{ref}", headers=auth(key))

    try:
        settings.knowledge_pack_admins = [admin_id]
        registered = await client.post(
            "/api/v1/knowledge/packs", json=shared, headers=auth(admin_key)
        )
        registered_tenant = await client.post(
            "/api/v1/knowledge/packs", json=tenant, headers=auth(manager)
        )
        pinned = await read(reader, "selfdev@1")
        latest = await read(reader, "selfdev")
        own = await read(manager, "tenant:fleet@2")
        # A tenant pack is not a shared one of the same name.
        not_shared = await read(reader, "fleet@2")
        unknown = await read(reader, "selfdev@9")
        malformed = await read(reader, "Selfdev@1")
        denied = await read(outsider, "selfdev@1")
        # A platform pack administrator reads a shared pack as such.
        platform_principal, platform = await create_agent_with_key(
            client, admin_key, name="platform", permissions=["workspaces.read"]
        )
        settings.knowledge_pack_admins = [admin_id, platform_principal["id"]]
        as_platform = await read(platform, "selfdev@1")
        platform_tenant = await read(platform, "tenant:fleet@2")
    finally:
        app.state.context_provider = None
        settings.knowledge_pack_admins = []
    assert registered.status_code == 200, registered.text
    assert registered_tenant.status_code == 200, registered_tenant.text
    assert pinned.status_code == 200, pinned.text
    assert pinned.json() == {
        "name": "selfdev",
        "version": "1",
        "kinds": [{"kind": "module"}],
        "relations": [],
        "scope": "common",
        "ref": "selfdev@1",
    }
    assert latest.status_code == 200 and latest.json() == pinned.json()
    assert own.status_code == 200, own.text
    assert own.json()["scope"] == "tenant"
    assert own.json()["version"] == "2"
    assert "namespace" not in own.json()
    assert not_shared.status_code == 404
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"
    assert unknown.json()["error"]["details"] == {"pack": "selfdev@9"}
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "invalid_pack_ref"
    assert denied.status_code == 403
    assert as_platform.status_code == 200, as_platform.text
    assert platform_tenant.status_code == 403
    # The tenant pack was asked for in the tenant's namespace, by its tenant ref.
    tenant_reads = [
        call
        for kind, call in fake.calls
        if kind == "read_package" and call["name"] == "tenant:fleet"
    ]
    assert [(c["version"], c["namespace"]) for c in tenant_reads] == [("2", f"tenant:{tenant_id}")]
    # Reads journal nothing: two registrations, no more.
    assert await _events(client, admin_key) == [
        "knowledge.pack_registered",
        "knowledge.pack_registered",
    ]


async def test_a_pack_read_without_memory_is_unavailable(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    assert app.state.context_provider is None
    response = await client.get("/api/v1/knowledge/packs/selfdev@1", headers=auth(admin_key))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "memory_disabled"
