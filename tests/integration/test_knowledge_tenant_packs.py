"""Tenant knowledge packs (CP-ADR-0060 amendment 2026-09-28, company-knowledge K010).

A pack with ``scope: tenant`` is registered by the tenant itself under
``knowledge.packs.manage``, without ``CP_KNOWLEDGE_PACK_ADMINS``; the core names
its owner (the tenant's namespace). A shared pack stays with the platform
administrators whatever the tenant's rights.
"""

import uuid
from typing import Any

import httpx
from fastapi import FastAPI

from tests.helpers import FakeKnowledge, auth, create_agent_with_key, create_workspace, do_bootstrap

MANIFEST = {
    "name": "fleet",
    "version": 1,
    "scope": "tenant",
    "kinds": [{"kind": "vehicle", "naturalKey": "<plate>"}],
}


async def _events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    items = (await client.get("/api/v1/events", params={"limit": 200}, headers=auth(key))).json()[
        "items"
    ]
    return [e for e in items if e["type"] == event_type]


async def _register(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/api/v1/knowledge/packs", json=body, headers=auth(key))


async def test_a_tenant_registers_its_pack_without_platform_admins(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    manager_principal, manager = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["knowledge.packs.manage"]
    )
    # No platform administrators are configured.
    assert app.state.settings.knowledge_pack_admins == []
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        ok = await _register(client, manager, MANIFEST)
    finally:
        app.state.context_provider = None
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"status": "created", "pack": {"name": "fleet", "version": "1"}}
    # The manifest as-is, plus the owner the core computed: the tenant namespace,
    # above every workspace tree of the tenant.
    [(kind, call)] = fake.calls
    assert kind == "package"
    assert call["package"] == {**MANIFEST, "namespace": f"tenant:{tenant_id}"}
    [event] = await _events(client, admin_key, "knowledge.pack_registered")
    assert event["actorId"] == manager_principal["id"]
    assert event["schemaVersion"] == 2
    assert event["payload"] == {
        "name": "fleet",
        "version": "1",
        "status": "created",
        "scope": "tenant",
    }
    # Tenants may share a pack name: the entity id carries the tenant.
    assert event["entityId"] == str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"knowledge-pack:tenant:{tenant_id}:fleet@1")
    )


async def test_a_shared_pack_without_platform_admin_rights_is_403(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, manager = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["knowledge.packs.manage"]
    )
    _, other = await create_agent_with_key(
        client, admin_key, name="other", permissions=["observations.write"]
    )
    shared = {k: v for k, v in MANIFEST.items() if k != "scope"}
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        # The tenant's right is not enough for a shared pack, nor the tenant admin.
        by_manager = await _register(client, manager, shared)
        by_admin = await _register(client, admin_key, shared)
        # scope: common is Memory's name of a shared pack: the same path.
        common = await _register(client, manager, {**shared, "scope": "common"})
        # A tenant pack takes the right.
        denied = await _register(client, other, MANIFEST)
        unknown_scope = await _register(client, manager, {**MANIFEST, "scope": "platform"})
        # The owner is the core's to name.
        foreign = await _register(client, manager, {**MANIFEST, "namespace": "tenant:other"})
        unversioned = await _register(
            client, manager, {k: v for k, v in MANIFEST.items() if k != "version"}
        )
    finally:
        app.state.context_provider = None
    for response in (by_manager, by_admin, common):
        assert response.status_code == 403
        assert response.json()["error"]["details"] == {"required": "knowledge_pack_admin"}
    assert denied.status_code == 403
    assert denied.json()["error"]["details"]["required"] == ["knowledge.packs.manage"]
    assert unknown_scope.status_code == 400
    assert foreign.status_code == 400
    assert unversioned.status_code == 422
    assert unversioned.json()["error"]["code"] == "pack_invalid"
    assert fake.calls == []
    assert await _events(client, admin_key, "knowledge.pack_registered") == []


async def test_memory_refusals_of_a_tenant_pack(client: httpx.AsyncClient, app: FastAPI) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    _, manager = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["knowledge.packs.manage"]
    )
    try:
        # A kind named like one of a shared pack: the manifest is wrong.
        app.state.context_provider = FakeKnowledge(fail_status=409, fail_code="kind_conflict")
        clash = await _register(client, manager, MANIFEST)
        # The version exists with other content (Memory's detail is a string).
        app.state.context_provider = FakeKnowledge(fail_status=409)
        version = await _register(client, manager, MANIFEST)
        app.state.context_provider = FakeKnowledge(fail_status=400)
        invalid = await _register(client, manager, MANIFEST)
    finally:
        app.state.context_provider = None
    assert clash.status_code == 422
    assert clash.json()["error"]["code"] == "pack_invalid"
    assert clash.json()["error"]["details"] == {"memoryStatus": 409, "conflict": "kind_conflict"}
    assert version.status_code == 409
    assert version.json()["error"]["code"] == "pack_version_conflict"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "pack_invalid"
    assert await _events(client, admin_key, "knowledge.pack_registered") == []


async def test_a_workspace_enables_a_pinned_tenant_pack(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    root = await create_workspace(client, admin_key, "root")
    fake = FakeKnowledge()
    app.state.context_provider = fake
    path = f"/api/v1/workspaces/{root['id']}/knowledge-packs"
    try:
        ok = await client.put(
            path, json={"packs": ["selfdev@1", "tenant:fleet@1"]}, headers=auth(admin_key)
        )
        unpinned = await client.put(path, json={"packs": ["tenant:fleet"]}, headers=auth(admin_key))
    finally:
        app.state.context_provider = None
    assert ok.status_code == 200, ok.text
    assert unpinned.status_code == 422
    assert unpinned.json()["error"]["code"] == "pack_version_required"
    assert unpinned.json()["error"]["details"]["packs"] == ["tenant:fleet"]
    [(kind, call)] = fake.calls
    assert kind == "kinds"
    assert call["packages"] == ["selfdev@1", "tenant:fleet@1"]
