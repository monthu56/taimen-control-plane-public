"""Knowledge snapshots and domain packs through the core (CP-ADR-0060)."""

import json
from typing import Any

import httpx
from fastapi import FastAPI

from tests.helpers import (
    SECRET_TEXT,
    FakeKnowledge,
    auth,
    create_agent_with_key,
    create_workspace,
    do_bootstrap,
)
from tests.helpers import (
    knowledge_snapshot as snapshot,
)


async def _setup(client: httpx.AsyncClient) -> tuple[dict[str, Any], str, str]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    return boot, admin_key, agent_key


async def _events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    items = (await client.get("/api/v1/events", params={"limit": 200}, headers=auth(key))).json()[
        "items"
    ]
    return [e for e in items if e["type"] == event_type]


async def test_snapshot_requires_observations_write(client: httpx.AsyncClient, app) -> None:
    _, admin_key, _ = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    _, limited = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["tasks.read"]
    )
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        response = await client.post(
            "/api/v1/knowledge/snapshots",
            json={**snapshot(), "workspaceId": root["id"]},
            headers=auth(limited),
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 403, response.text
    assert fake.calls == []


async def test_client_cannot_name_namespace_or_scopes(client: httpx.AsyncClient, app) -> None:
    _, admin_key, agent_key = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        for extra in ({"namespace": "tenant:other:ws:x"}, {"scopes": ["workspace:other"]}):
            response = await client.post(
                "/api/v1/knowledge/snapshots",
                json={**snapshot(), "workspaceId": root["id"], **extra},
                headers=auth(agent_key),
            )
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "invalid_request"
    finally:
        app.state.context_provider = None
    assert fake.calls == []


async def test_snapshot_bounds_follow_memory(client: httpx.AsyncClient, app) -> None:
    _, admin_key, agent_key = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        rejected = []
        for overrides in (
            # Memory's snapshot scope is a string of the source, not an object.
            {"scope": {"namespace": "tenant:x"}},
            {"source": "s" * 201},
            {"snapshotId": "i" * 201},
            # At most 20000 entities and relations together, as Memory accepts.
            {"entities": [{}] * 10_001, "relations": [{}] * 10_000},
        ):
            rejected.append(
                await client.post(
                    "/api/v1/knowledge/snapshots",
                    json={**snapshot(**overrides), "workspaceId": root["id"]},
                    headers=auth(agent_key),
                )
            )
        at_limit = await client.post(
            "/api/v1/knowledge/snapshots",
            json={
                **snapshot(entities=[{}] * 10_000, relations=[{}] * 10_000),
                "workspaceId": root["id"],
            },
            headers=auth(agent_key),
        )
    finally:
        app.state.context_provider = None
    for response in rejected:
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"
    assert at_limit.status_code == 200, at_limit.text
    assert len(fake.calls) == 1


async def test_sub_workspace_writes_into_root_namespace(client: httpx.AsyncClient, app) -> None:
    boot, admin_key, agent_key = await _setup(client)
    tenant_id = boot["tenant"]["id"]
    root = await create_workspace(client, admin_key, "root")
    child = await create_workspace(client, admin_key, "child", parent_id=root["id"])
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        response = await client.post(
            "/api/v1/knowledge/snapshots",
            json={**snapshot(), "workspaceId": child["id"]},
            headers=auth(agent_key),
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 200, response.text
    # Memory's answer is passed through unchanged.
    assert response.json()["entities"] == {"created": 1, "updated": 0, "deleted": 0}

    [(kind, call)] = fake.calls
    assert kind == "reconcile"
    assert call["namespace"] == f"tenant:{tenant_id}:ws:{root['id']}"
    assert call["scopes"] == [f"workspace:{child['id']}"]
    # The document is forwarded as-is, minus the routing field.
    forwarded = call["snapshot"]
    assert "workspaceId" not in forwarded
    assert forwarded["snapshotId"] == "snap-1"
    assert forwarded["scope"] == "repo:control-plane"
    assert forwarded["entities"][0]["title"] == SECRET_TEXT


async def test_journal_event_carries_counters_not_content(client: httpx.AsyncClient, app) -> None:
    _, admin_key, agent_key = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    app.state.context_provider = FakeKnowledge()
    try:
        response = await client.post(
            "/api/v1/knowledge/snapshots",
            json={**snapshot(), "workspaceId": root["id"]},
            headers=auth(agent_key),
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 200, response.text

    [event] = await _events(client, agent_key, "knowledge.snapshot_reconciled")
    assert event["entityId"] == root["id"]
    payload = event["payload"]
    assert payload["snapshotId"] == "snap-1"
    assert payload["entityCount"] == 1
    assert payload["relationCount"] == 1
    assert payload["duplicate"] is False
    assert payload["counters"]["entities.created"] == 1
    assert SECRET_TEXT not in json.dumps(event)
    assert "entities" not in payload and "relations" not in payload


async def test_changed_regulation_is_one_knowledge_changed_event(
    client: httpx.AsyncClient, app
) -> None:
    """CP-ADR-0076 §7: the keys Memory reports for a snapshot become one event."""
    _, admin_key, agent_key = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    child = await create_workspace(client, admin_key, "child", parent_id=root["id"])
    app.state.context_provider = FakeKnowledge(
        changes={
            "opened": [],
            "changed": [{"kind": "regulation", "key": "regulation:procurement"}],
            "closed": [],
            "limit": 1000,
            "truncated": False,
        }
    )
    try:
        response = await client.post(
            "/api/v1/knowledge/snapshots",
            json={**snapshot(), "workspaceId": child["id"]},
            headers=auth(agent_key),
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 200, response.text

    [event] = await _events(client, agent_key, "knowledge.changed")
    assert event["entityType"] == "workspace" and event["entityId"] == child["id"]
    payload = event["payload"]
    assert payload["changes"] == [
        {"kind": "regulation", "key": "regulation:procurement", "change": "changed"}
    ]
    assert payload["truncated"] is False
    assert payload["snapshotId"] == "snap-1"
    assert payload["source"] == snapshot()["source"]
    assert payload["workspaceId"] == child["id"]
    assert payload["rootWorkspaceId"] == root["id"]
    assert SECRET_TEXT not in json.dumps(event)
    [reconciled] = await _events(client, agent_key, "knowledge.snapshot_reconciled")
    # Keys go to knowledge.changed; the counters stay numbers of the answer.
    assert not any(name.startswith("changes.") for name in reconciled["payload"]["counters"])


async def test_reconciliation_without_changes_writes_no_knowledge_changed(
    client: httpx.AsyncClient, app
) -> None:
    _, admin_key, agent_key = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    empty = {"opened": [], "changed": [], "closed": [], "limit": 1000, "truncated": False}
    try:
        # Memory before MEM-ADR-020 answers without ``changes``; a repeated or
        # unchanged snapshot answers with empty lists.
        for fake in (FakeKnowledge(), FakeKnowledge(changes=empty)):
            app.state.context_provider = fake
            response = await client.post(
                "/api/v1/knowledge/snapshots",
                json={**snapshot(), "workspaceId": root["id"]},
                headers=auth(agent_key),
            )
            assert response.status_code == 200, response.text
    finally:
        app.state.context_provider = None
    assert len(await _events(client, agent_key, "knowledge.snapshot_reconciled")) == 2
    assert await _events(client, agent_key, "knowledge.changed") == []


async def test_memory_failures_are_mapped(client: httpx.AsyncClient, app) -> None:
    _, admin_key, agent_key = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    body = {**snapshot(), "workspaceId": root["id"]}

    app.state.context_provider = FakeKnowledge(fail_status=409)
    try:
        stale = await client.post("/api/v1/knowledge/snapshots", json=body, headers=auth(agent_key))
        app.state.context_provider = FakeKnowledge(fail_status=503, retryable=True)
        down = await client.post("/api/v1/knowledge/snapshots", json=body, headers=auth(agent_key))
        app.state.context_provider = FakeKnowledge(fail_status=400)
        invalid = await client.post(
            "/api/v1/knowledge/snapshots", json=body, headers=auth(agent_key)
        )
        # Memory refusing the core's own credential is the core's problem.
        app.state.context_provider = FakeKnowledge(fail_status=403, retryable=True)
        forbidden = await client.post(
            "/api/v1/knowledge/snapshots", json=body, headers=auth(agent_key)
        )
        app.state.context_provider = FakeKnowledge(fail_status=422)
        rejected = await client.post(
            "/api/v1/knowledge/snapshots", json=body, headers=auth(agent_key)
        )
    finally:
        app.state.context_provider = None

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "snapshot_stale"
    assert down.status_code == 502
    assert down.json()["error"]["code"] == "memory_unavailable"
    assert down.json()["error"]["details"]["retryable"] is True
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "snapshot_invalid"
    assert invalid.json()["error"]["details"] == {"memoryStatus": 400}
    assert forbidden.status_code == 502
    assert forbidden.json()["error"]["details"] == {"memoryStatus": 403, "retryable": False}
    assert rejected.status_code == 502
    assert rejected.json()["error"]["details"] == {"memoryStatus": 422, "retryable": False}
    # Memory's own text never reaches the client.
    for response in (stale, down, invalid, forbidden, rejected):
        assert "memory said" not in response.text
    # Nothing reconciled, nothing journaled.
    assert await _events(client, agent_key, "knowledge.snapshot_reconciled") == []


async def test_snapshot_without_provider_or_workspace(client: httpx.AsyncClient) -> None:
    _, admin_key, agent_key = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    disabled = await client.post(
        "/api/v1/knowledge/snapshots",
        json={**snapshot(), "workspaceId": root["id"]},
        headers=auth(agent_key),
    )
    assert disabled.status_code == 503
    assert disabled.json()["error"]["code"] == "memory_disabled"

    unknown = await client.post(
        "/api/v1/knowledge/snapshots",
        json={**snapshot(), "workspaceId": "00000000-0000-0000-0000-000000000001"},
        headers=auth(agent_key),
    )
    assert unknown.status_code == 404


async def test_snapshot_body_limit_is_larger_than_the_api_default(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    _, admin_key, agent_key = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    settings = app.state.settings
    filler = "x" * 1000
    fits = settings.max_body_bytes * 2 // 1100
    app.state.context_provider = FakeKnowledge()
    try:
        big = await client.post(
            "/api/v1/knowledge/snapshots",
            json={
                **snapshot(
                    entities=[{"kind": "doc", "key": str(i), "t": filler} for i in range(fits)]
                ),
                "workspaceId": root["id"],
            },
            headers=auth(agent_key),
        )
        too_big = await client.post(
            "/api/v1/knowledge/snapshots",
            content=b"{" + b" " * settings.knowledge_snapshot_max_body_bytes + b"}",
            headers={**auth(agent_key), "Content-Type": "application/json"},
        )
    finally:
        app.state.context_provider = None
    assert big.status_code == 200, big.text[:500]
    assert too_big.status_code == 413
    # Elsewhere the ordinary ceiling still holds.
    other = await client.post(
        "/api/v1/observations",
        content=b"{" + b" " * settings.max_body_bytes + b"}",
        headers={**auth(agent_key), "Content-Type": "application/json"},
    )
    assert other.status_code == 413


async def test_pack_registration_is_platform_admin_only(client: httpx.AsyncClient, app) -> None:
    boot, admin_key, agent_key = await _setup(client)
    admin_id = boot["adminPrincipal"]["id"]
    settings = app.state.settings
    fake = FakeKnowledge()
    app.state.context_provider = fake
    manifest = {"name": "selfdev", "version": "1", "kinds": [{"kind": "module"}]}

    async def register(key: str, body: dict[str, Any] = manifest) -> httpx.Response:
        return await client.post("/api/v1/knowledge/packs", json=body, headers=auth(key))

    try:
        # No platform administrators configured: closed even for the tenant admin.
        closed = await register(admin_key)
        settings.knowledge_pack_admins = [admin_id]
        denied = await register(agent_key)
        ok = await register(admin_key)
        empty = await register(admin_key, {})
        unversioned = await register(admin_key, {"name": "selfdev", "kinds": []})
        app.state.context_provider = FakeKnowledge(fail_status=409)
        conflict = await register(admin_key)
        app.state.context_provider = FakeKnowledge(fail_status=400)
        invalid = await register(admin_key)
    finally:
        app.state.context_provider = None
        settings.knowledge_pack_admins = []
    assert closed.status_code == 403
    assert closed.json()["error"]["details"] == {"required": "knowledge_pack_admin"}
    assert denied.status_code == 403
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"status": "created", "pack": {"name": "selfdev", "version": "1"}}
    assert empty.status_code == 422
    assert unversioned.status_code == 422
    assert unversioned.json()["error"]["code"] == "pack_invalid"
    assert unversioned.json()["error"]["details"] == {"field": "version"}
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "pack_version_conflict"
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "pack_invalid"
    # Forwarded once from this provider (refused requests never reach Memory), as-is.
    [(kind, call)] = fake.calls
    assert kind == "package"
    assert call["package"] == manifest
    # Audited: who registered which version; a failed registration is not.
    [event] = await _events(client, admin_key, "knowledge.pack_registered")
    assert event["actorId"] == admin_id
    assert event["entityType"] == "knowledge_pack"
    assert event["payload"]["name"] == "selfdev"
    assert event["payload"]["version"] == "1"
    assert event["payload"]["status"] == "created"
    assert "kinds" not in event["payload"]


async def test_workspace_packs_target_the_root_namespace(client: httpx.AsyncClient, app) -> None:
    boot, admin_key, agent_key = await _setup(client)
    tenant_id = boot["tenant"]["id"]
    root = await create_workspace(client, admin_key, "root")
    child = await create_workspace(client, admin_key, "child", parent_id=root["id"])
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        denied = await client.put(
            f"/api/v1/workspaces/{root['id']}/knowledge-packs",
            json={"packs": ["selfdev@1"]},
            headers=auth(agent_key),
        )
        not_root = await client.put(
            f"/api/v1/workspaces/{child['id']}/knowledge-packs",
            json={"packs": ["selfdev@1"]},
            headers=auth(admin_key),
        )
        ok = await client.put(
            f"/api/v1/workspaces/{root['id']}/knowledge-packs",
            json={"packs": ["selfdev@1", "selfdev@1", "ops@2.0"], "strict": True},
            headers=auth(admin_key),
        )
        # The latest version is whatever anyone published last: only pinned refs.
        unpinned = await client.put(
            f"/api/v1/workspaces/{root['id']}/knowledge-packs",
            json={"packs": ["selfdev@1", "ops"]},
            headers=auth(admin_key),
        )
        bad = await client.put(
            f"/api/v1/workspaces/{root['id']}/knowledge-packs",
            json={"packs": ["selfdev@1"], "namespace": "tenant:x"},
            headers=auth(admin_key),
        )
        app.state.context_provider = FakeKnowledge(fail_status=500, retryable=True)
        down = await client.put(
            f"/api/v1/workspaces/{root['id']}/knowledge-packs",
            json={"packs": []},
            headers=auth(admin_key),
        )
    finally:
        app.state.context_provider = None
    assert denied.status_code == 403
    assert not_root.status_code == 422
    assert not_root.json()["error"]["code"] == "workspace_not_root"
    assert not_root.json()["error"]["details"]["rootWorkspaceId"] == root["id"]
    assert ok.status_code == 200, ok.text
    assert unpinned.status_code == 422
    assert unpinned.json()["error"]["code"] == "pack_version_required"
    assert unpinned.json()["error"]["details"]["packs"] == ["ops"]
    assert bad.status_code == 400
    assert down.status_code == 502
    [(kind, call)] = fake.calls
    assert kind == "kinds"
    assert call["namespace"] == f"tenant:{tenant_id}:ws:{root['id']}"
    assert call["packages"] == ["selfdev@1", "ops@2.0"]
    assert call["strict"] is True
    [event] = await _events(client, admin_key, "knowledge.packs_configured")
    assert event["entityId"] == root["id"]
    assert event["actorId"] == boot["adminPrincipal"]["id"]
    assert event["payload"] == {
        "workspaceId": root["id"],
        "namespace": call["namespace"],
        "packs": ["selfdev@1", "ops@2.0"],
        "strict": True,
    }


async def test_memory_unknown_pack_is_a_client_error(client: httpx.AsyncClient, app) -> None:
    _, admin_key, _ = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    app.state.context_provider = FakeKnowledge(fail_status=404)
    try:
        response = await client.put(
            f"/api/v1/workspaces/{root['id']}/knowledge-packs",
            json={"packs": ["nope@1"]},
            headers=auth(admin_key),
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "pack_not_found"
