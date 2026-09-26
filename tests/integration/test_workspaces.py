import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)


async def test_workspace_hierarchy_crud(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    engineering = await create_workspace(client, admin_key, "engineering")
    platform = await create_workspace(client, admin_key, "platform", parent_id=engineering["id"])
    assert platform["parentId"] == engineering["id"]
    assert platform["version"] == 1

    # get + ETag
    response = await client.get(f"/api/v1/workspaces/{platform['id']}", headers=auth(admin_key))
    assert response.status_code == 200
    assert response.headers["etag"] == '"workspace-1"'

    # children listing
    response = await client.get(
        f"/api/v1/workspaces?parentId={engineering['id']}", headers=auth(admin_key)
    )
    assert [w["slug"] for w in response.json()["items"]] == ["platform"]

    # roots listing
    response = await client.get("/api/v1/workspaces?rootsOnly=true", headers=auth(admin_key))
    assert [w["slug"] for w in response.json()["items"]] == ["engineering"]


async def test_workspace_update_optimistic_concurrency(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "sales")

    response = await client.patch(
        f"/api/v1/workspaces/{workspace['id']}",
        json={"name": "Sales Dept"},
        headers={**auth(admin_key), "If-Match": '"workspace-1"'},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2

    # Stale version -> conflict
    response = await client.patch(
        f"/api/v1/workspaces/{workspace['id']}",
        json={"name": "Nope"},
        headers={**auth(admin_key), "If-Match": '"workspace-1"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "version_conflict"

    # Missing If-Match -> 428
    response = await client.patch(
        f"/api/v1/workspaces/{workspace['id']}",
        json={"name": "Nope"},
        headers=auth(admin_key),
    )
    assert response.status_code == 428


async def test_sibling_slug_uniqueness(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    parent = await create_workspace(client, admin_key, "eng")
    await create_workspace(client, admin_key, "web", parent_id=parent["id"])

    # Same slug under the same parent -> conflict
    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "web", "name": "Web", "parentId": parent["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_slug_conflict"

    # Same slug at ROOT level is fine (different sibling set)
    await create_workspace(client, admin_key, "web")

    # Duplicate root slug -> conflict
    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "eng", "name": "Eng"},
        headers=auth(admin_key),
    )
    assert response.status_code == 409


async def test_move_cycle_prevention(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    a = await create_workspace(client, admin_key, "wa")
    b = await create_workspace(client, admin_key, "wb", parent_id=a["id"])
    c = await create_workspace(client, admin_key, "wc", parent_id=b["id"])

    # Move A under its own grandchild -> cycle
    response = await client.post(
        f"/api/v1/workspaces/{a['id']}:move",
        json={"newParentId": c["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_cycle"

    # Move under itself -> cycle
    response = await client.post(
        f"/api/v1/workspaces/{a['id']}:move",
        json={"newParentId": a["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 422

    # A legal move: C to root
    response = await client.post(
        f"/api/v1/workspaces/{c['id']}:move",
        json={"newParentId": None},
        headers=auth(admin_key),
    )
    assert response.status_code == 200
    assert response.json()["parentId"] is None


async def test_move_slug_conflict_at_destination(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    a = await create_workspace(client, admin_key, "wa")
    b = await create_workspace(client, admin_key, "wb")
    await create_workspace(client, admin_key, "dup", parent_id=a["id"])
    dup_b = await create_workspace(client, admin_key, "dup", parent_id=b["id"])

    response = await client.post(
        f"/api/v1/workspaces/{dup_b['id']}:move",
        json={"newParentId": a["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "workspace_slug_conflict"


async def test_archive_semantics(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    parent = await create_workspace(client, admin_key, "dept")
    child = await create_workspace(client, admin_key, "team", parent_id=parent["id"])

    # Cannot archive with an active child
    response = await client.post(
        f"/api/v1/workspaces/{parent['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_has_active_children"

    # Archive child then parent; archive is idempotent
    assert (
        await client.post(f"/api/v1/workspaces/{child['id']}:archive", headers=auth(admin_key))
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/workspaces/{parent['id']}:archive", headers=auth(admin_key))
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/workspaces/{parent['id']}:archive", headers=auth(admin_key))
    ).status_code == 200

    # No new children or tasks under an archived workspace
    response = await client.post(
        "/api/v1/workspaces",
        json={"slug": "late", "name": "Late", "parentId": parent["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_archived"

    response = await client.post(
        "/api/v1/tasks",
        json={"title": "T", "workspaceId": parent["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 422


async def test_task_in_workspace(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "eng")

    task = await create_task(client, admin_key, workspaceId=workspace["id"])
    assert task["workspaceId"] == workspace["id"]

    events = (await client.get("/api/v1/events", headers=auth(admin_key))).json()["items"]
    created = next(e for e in events if e["type"] == "task.created")
    assert created["payload"]["workspaceId"] == workspace["id"]


async def test_workspace_members(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "eng")
    principal, _ = await create_agent_with_key(client, admin_key)

    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/members",
        json={"principalId": principal["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 201
    # Idempotent
    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}/members",
        json={"principalId": principal["id"]},
        headers=auth(admin_key),
    )
    assert response.status_code == 201

    members = (
        await client.get(f"/api/v1/workspaces/{workspace['id']}/members", headers=auth(admin_key))
    ).json()["items"]
    assert len(members) == 1

    assert (
        await client.post(
            f"/api/v1/workspaces/{workspace['id']}/members/{principal['id']}:remove",
            headers=auth(admin_key),
        )
    ).status_code == 204
    # Removing again is a no-op
    assert (
        await client.post(
            f"/api/v1/workspaces/{workspace['id']}/members/{principal['id']}:remove",
            headers=auth(admin_key),
        )
    ).status_code == 204


async def test_workspace_permissions_and_isolation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    workspace = await create_workspace(client, admin_key, "eng")
    _, limited_key = await create_agent_with_key(client, admin_key, permissions=["tasks.read"])

    # No workspaces.read -> 403
    assert (await client.get("/api/v1/workspaces", headers=auth(limited_key))).status_code == 403
    # No workspaces.manage -> 403
    assert (
        await client.post(
            "/api/v1/workspaces", json={"slug": "xx", "name": "X"}, headers=auth(limited_key)
        )
    ).status_code == 403

    # Cross-tenant: 404 by UUID
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    assert (
        await client.get(f"/api/v1/workspaces/{workspace['id']}", headers=auth(key_b))
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/workspaces/{workspace['id']}:archive", headers=auth(key_b))
    ).status_code == 404
    assert (await client.get("/api/v1/workspaces", headers=auth(key_b))).json()["items"] == []
