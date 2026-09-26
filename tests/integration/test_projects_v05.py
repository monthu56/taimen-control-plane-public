"""Project Model (v0.5): a Project is a profile on a Workspace, not a second tree.

The guarantees under test are the ones ADR-0031 and ADR-0035 make load-bearing:
creation is atomic with the workspace it lands on, one workspace carries at most
one profile, ``parentProjectId`` is derived from the workspace tree at read time
(a plain workspace in between must not break the derivation), archiving a project
retires the record only — workspace, tasks and journal history survive — and a
workspace refuses to be archived while it still carries a live project.
"""

import uuid
from typing import Any

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

# A schema strict enough that a wrong type is a real violation, and loose
# enough that most tests can ignore custom fields entirely.
BUDGET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"budget": {"type": "integer"}, "codename": {"type": "string"}},
    "required": ["budget"],
    "additionalProperties": False,
}


async def create_template(
    client: httpx.AsyncClient, key: str, *, template_key: str = "delivery", **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/project-templates",
        json={"key": template_key, "displayName": template_key.title(), **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_project(client: httpx.AsyncClient, key: str, **payload: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/projects", json=payload, headers=auth(key))
    assert response.status_code == 201, response.text
    return response.json()


async def list_workspace_slugs(client: httpx.AsyncClient, key: str) -> list[str]:
    response = await client.get("/api/v1/workspaces?limit=200", headers=auth(key))
    assert response.status_code == 200, response.text
    return [w["slug"] for w in response.json()["items"]]


async def test_create_project_on_existing_workspace(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    template = await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "atlas")

    project = await create_project(
        client, admin_key, workspaceId=workspace["id"], templateKey="delivery"
    )

    assert project["workspaceId"] == workspace["id"]
    assert project["templateId"] == template["id"]
    assert project["templateKey"] == "delivery"
    assert project["templateVersion"] == 1
    # The default lifecycle starts every project as planned/planned.
    assert project["statusKey"] == "planned"
    assert project["systemStatusCategory"] == "planned"
    assert project["status"] == "active"
    assert project["version"] == 1
    assert project["parentProjectId"] is None
    assert project["activeConfigRevision"] is None


async def test_create_project_with_workspace_slug_is_atomic(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)

    project = await create_project(
        client,
        admin_key,
        workspaceSlug="atlas",
        workspaceName="Atlas Programme",
        templateKey="delivery",
    )

    workspace = (
        await client.get(f"/api/v1/workspaces/{project['workspaceId']}", headers=auth(admin_key))
    ).json()
    assert workspace["slug"] == "atlas"
    assert workspace["name"] == "Atlas Programme"
    assert workspace["status"] == "active"
    # Exactly one workspace exists: the one the project request created.
    assert await list_workspace_slugs(client, admin_key) == ["atlas"]


async def test_failed_create_leaves_no_orphan_workspace(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)

    # The workspace is written before the template is resolved, so an unknown
    # templateKey can only be safe if the whole request rolls back.
    response = await client.post(
        "/api/v1/projects",
        json={"workspaceSlug": "ghost", "templateKey": "does-not-exist"},
        headers=auth(admin_key),
    )
    assert response.status_code == 404, response.text

    assert await list_workspace_slugs(client, admin_key) == []
    assert (await client.get("/api/v1/projects", headers=auth(admin_key))).json()["items"] == []

    # The slug is still free afterwards — nothing invisible is holding it.
    project = await create_project(client, admin_key, workspaceSlug="ghost", templateKey="delivery")
    assert project["statusKey"] == "planned"


async def test_second_project_on_same_workspace_conflicts(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "atlas")
    await create_project(client, admin_key, workspaceId=workspace["id"], templateKey="delivery")

    response = await client.post(
        "/api/v1/projects",
        json={"workspaceId": workspace["id"], "templateKey": "delivery"},
        headers=auth(admin_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "project_exists"


async def test_archived_workspace_rejects_a_project(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "retired")
    assert (
        await client.post(f"/api/v1/workspaces/{workspace['id']}:archive", headers=auth(admin_key))
    ).status_code == 200

    response = await client.post(
        "/api/v1/projects",
        json={"workspaceId": workspace["id"], "templateKey": "delivery"},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_archived"


async def test_get_project_etag_and_response_shape(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "atlas")
    project = await create_project(
        client, admin_key, workspaceId=workspace["id"], templateKey="delivery"
    )

    response = await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    assert response.status_code == 200
    assert response.headers["etag"] == '"project-1"'
    fetched = response.json()
    for field in (
        "id",
        "workspaceId",
        "parentProjectId",
        "templateKey",
        "templateVersion",
        "statusKey",
        "systemStatusCategory",
        "activeConfigRevision",
        "version",
        "createdAt",
        "updatedAt",
        "archivedAt",
    ):
        assert field in fetched, field
    assert fetched["id"] == project["id"]
    assert fetched["workspaceId"] == workspace["id"]
    assert fetched["parentProjectId"] is None
    assert fetched["templateKey"] == "delivery"
    assert fetched["templateVersion"] == 1
    assert fetched["version"] == 1
    assert fetched["archivedAt"] is None

    # A config revision is only reflected once it is activated, and activation
    # bumps the version the ETag is built from.
    assert (
        await client.post(
            f"/api/v1/projects/{project['id']}/config-revisions",
            json={"config": {"settings": {"tone": "formal"}}},
            headers=auth(admin_key),
        )
    ).status_code == 201
    assert (
        await client.post(
            f"/api/v1/projects/{project['id']}/config-revisions/1:activate",
            headers={**auth(admin_key), "If-Match": '"project-1"'},
        )
    ).status_code == 200

    response = await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    assert response.headers["etag"] == '"project-2"'
    assert response.json()["activeConfigRevision"] == 1
    assert response.json()["activeConfigRevisionId"] is not None


async def test_update_project_with_if_match(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key, fieldSchema=BUDGET_SCHEMA)
    owner, _ = await create_agent_with_key(client, admin_key, name="owner")
    workspace = await create_workspace(client, admin_key, "atlas")
    project = await create_project(
        client,
        admin_key,
        workspaceId=workspace["id"],
        templateKey="delivery",
        customFields={"budget": 100},
    )

    response = await client.patch(
        f"/api/v1/projects/{project['id']}",
        json={
            "ownerPrincipalId": owner["id"],
            "startDate": "2026-01-05T00:00:00Z",
            "targetDate": "2026-03-01T00:00:00Z",
            "customFields": {"budget": 250, "codename": "atlas"},
        },
        headers={**auth(admin_key), "If-Match": '"project-1"'},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["version"] == 2
    assert updated["ownerPrincipalId"] == owner["id"]
    assert updated["startDate"].startswith("2026-01-05")
    assert updated["targetDate"].startswith("2026-03-01")
    assert updated["customFields"] == {"budget": 250, "codename": "atlas"}

    # Stale If-Match -> conflict
    response = await client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"customFields": {"budget": 300}},
        headers={**auth(admin_key), "If-Match": '"project-1"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "version_conflict"

    # Missing If-Match -> precondition required
    response = await client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"customFields": {"budget": 300}},
        headers=auth(admin_key),
    )
    assert response.status_code == 428
    assert response.json()["error"]["code"] == "if_match_required"

    # Custom fields are validated against the exact template version's schema.
    response = await client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"customFields": {"budget": "plenty"}},
        headers={**auth(admin_key), "If-Match": '"project-2"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "custom_fields_invalid"
    assert response.json()["error"]["details"]["field"] == "customFields"

    # The rejected write changed nothing.
    assert (await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))).json()[
        "customFields"
    ] == {"budget": 250, "codename": "atlas"}


async def test_nested_projects_derive_parent_through_plain_workspace(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)

    portfolio = await create_project(
        client, admin_key, workspaceSlug="portfolio", templateKey="delivery"
    )
    # A plain workspace with no profile sits between the two projects.
    squad = await create_workspace(client, admin_key, "squad", parent_id=portfolio["workspaceId"])
    nested = await create_project(
        client,
        admin_key,
        workspaceSlug="atlas",
        parentWorkspaceId=squad["id"],
        templateKey="delivery",
    )
    direct = await create_project(
        client,
        admin_key,
        workspaceSlug="beta",
        parentWorkspaceId=portfolio["workspaceId"],
        templateKey="delivery",
    )

    assert portfolio["parentProjectId"] is None
    assert nested["parentProjectId"] == portfolio["id"]
    assert direct["parentProjectId"] == portfolio["id"]

    # Derived at read time, not stored: re-reading gives the same answer.
    fetched = (await client.get(f"/api/v1/projects/{nested['id']}", headers=auth(admin_key))).json()
    assert fetched["parentProjectId"] == portfolio["id"]

    # A third level shadows the root: the nearest ancestor profile wins.
    deepest = await create_project(
        client,
        admin_key,
        workspaceSlug="atlas-core",
        parentWorkspaceId=nested["workspaceId"],
        templateKey="delivery",
    )
    assert deepest["parentProjectId"] == nested["id"]


async def test_archive_project_keeps_workspace_tasks_and_events(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "atlas")
    project = await create_project(
        client, admin_key, workspaceId=workspace["id"], templateKey="delivery"
    )
    task = await create_task(client, admin_key, title="Survives", workspaceId=workspace["id"])

    response = await client.post(
        f"/api/v1/projects/{project['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    archived = response.json()
    assert archived["status"] == "archived"
    assert archived["archivedAt"] is not None
    assert archived["version"] == 2

    # Idempotent: a repeat archive returns the same record, unchanged.
    repeat = await client.post(f"/api/v1/projects/{project['id']}:archive", headers=auth(admin_key))
    assert repeat.status_code == 200
    assert repeat.json()["version"] == 2
    assert repeat.json()["archivedAt"] == archived["archivedAt"]

    # The container, the work and the history are all untouched.
    workspace_after = await client.get(
        f"/api/v1/workspaces/{workspace['id']}", headers=auth(admin_key)
    )
    assert workspace_after.status_code == 200
    assert workspace_after.json()["status"] == "active"

    task_after = await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))
    assert task_after.status_code == 200
    assert task_after.json()["workspaceId"] == workspace["id"]

    events = (
        await client.get(
            f"/api/v1/events?entityType=project&entityId={project['id']}",
            headers=auth(admin_key),
        )
    ).json()["items"]
    assert [e["type"] for e in events] == ["project.created", "project.archived"]

    task_events = (
        await client.get(
            f"/api/v1/events?entityType=task&entityId={task['id']}", headers=auth(admin_key)
        )
    ).json()["items"]
    assert [e["type"] for e in task_events] == ["task.created"]


async def test_archived_project_rejects_mutations(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "atlas")
    project = await create_project(
        client, admin_key, workspaceId=workspace["id"], templateKey="delivery"
    )
    assert (
        await client.post(f"/api/v1/projects/{project['id']}:archive", headers=auth(admin_key))
    ).status_code == 200

    # Every rejection below uses the CURRENT version, so 422 is about the
    # archived record and not about a stale precondition.
    response = await client.patch(
        f"/api/v1/projects/{project['id']}",
        json={"customFields": {"note": "late"}},
        headers={**auth(admin_key), "If-Match": '"project-2"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "project_archived"

    response = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "active"},
        headers={**auth(admin_key), "If-Match": '"project-2"'},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "project_archived"

    response = await client.post(
        f"/api/v1/projects/{project['id']}/config-revisions",
        json={"config": {"settings": {"tone": "formal"}}},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "project_archived"


async def test_workspace_archive_requires_project_archived_first(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "atlas")
    project = await create_project(
        client, admin_key, workspaceId=workspace["id"], templateKey="delivery"
    )

    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "workspace_has_active_project"
    assert response.json()["error"]["details"]["projectId"] == project["id"]

    assert (
        await client.post(f"/api/v1/projects/{project['id']}:archive", headers=auth(admin_key))
    ).status_code == 200

    response = await client.post(
        f"/api/v1/workspaces/{workspace['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"


async def test_list_projects_filters(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    await create_template(client, admin_key, template_key="research")

    delivery = await create_project(
        client, admin_key, workspaceSlug="atlas", templateKey="delivery"
    )
    research = await create_project(client, admin_key, workspaceSlug="lab", templateKey="research")
    running = await create_project(client, admin_key, workspaceSlug="beta", templateKey="delivery")
    assert (
        await client.post(
            f"/api/v1/projects/{running['id']}:transition",
            json={"statusKey": "active"},
            headers={**auth(admin_key), "If-Match": '"project-1"'},
        )
    ).status_code == 200

    async def ids(query: str) -> set[str]:
        response = await client.get(f"/api/v1/projects?{query}", headers=auth(admin_key))
        assert response.status_code == 200, response.text
        return {p["id"] for p in response.json()["items"]}

    assert await ids(f"workspaceId={delivery['workspaceId']}") == {delivery["id"]}
    assert await ids("templateKey=research") == {research["id"]}
    assert await ids("templateKey=delivery") == {delivery["id"], running["id"]}
    assert await ids("statusKey=active") == {running["id"]}
    assert await ids("statusKey=planned") == {delivery["id"], research["id"]}
    assert await ids("systemStatusCategory=active") == {running["id"]}
    assert await ids("systemStatusCategory=planned") == {delivery["id"], research["id"]}
    assert await ids("status=active") == {delivery["id"], research["id"], running["id"]}
    assert await ids("status=archived") == set()

    assert (
        await client.post(f"/api/v1/projects/{research['id']}:archive", headers=auth(admin_key))
    ).status_code == 200
    assert await ids("status=archived") == {research["id"]}
    assert await ids("status=active") == {delivery["id"], running["id"]}
    # The record status and the lifecycle status are independent axes.
    assert await ids("status=archived&statusKey=planned") == {research["id"]}

    response = await client.get("/api/v1/projects?status=nonsense", headers=auth(admin_key))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_status"


async def test_list_projects_cursor_pagination(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    created = {
        (await create_project(client, admin_key, workspaceSlug=slug, templateKey="delivery"))["id"]
        for slug in ("one", "two", "three")
    }

    first = (await client.get("/api/v1/projects?limit=2", headers=auth(admin_key))).json()
    assert len(first["items"]) == 2
    assert first["nextCursor"] is not None

    second = (
        await client.get(
            f"/api/v1/projects?limit=2&cursor={first['nextCursor']}", headers=auth(admin_key)
        )
    ).json()
    assert len(second["items"]) == 1
    assert second["nextCursor"] is None

    paged = [p["id"] for p in first["items"]] + [p["id"] for p in second["items"]]
    assert len(paged) == len(set(paged))
    assert set(paged) == created


async def test_project_permission_matrix(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "atlas")
    project = await create_project(
        client, admin_key, workspaceId=workspace["id"], templateKey="delivery"
    )
    spare = await create_workspace(client, admin_key, "spare")

    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["projects.read"]
    )
    _, manager_key = await create_agent_with_key(
        client, admin_key, name="manager", permissions=["projects.manage"]
    )
    _, outsider_key = await create_agent_with_key(
        client, admin_key, name="outsider", permissions=["tasks.read"]
    )

    # projects.read: may look, may not touch.
    assert (await client.get("/api/v1/projects", headers=auth(reader_key))).status_code == 200
    assert (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(reader_key))
    ).status_code == 200
    assert (
        await client.post(
            "/api/v1/projects",
            json={"workspaceId": spare["id"], "templateKey": "delivery"},
            headers=auth(reader_key),
        )
    ).status_code == 403
    assert (
        await client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"customFields": {"x": 1}},
            headers={**auth(reader_key), "If-Match": '"project-1"'},
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/projects/{project['id']}:archive", headers=auth(reader_key))
    ).status_code == 403

    # projects.manage: may write, may not read back — and inline workspace
    # creation still needs workspaces.manage on top.
    assert (
        await client.post(
            "/api/v1/projects",
            json={"workspaceId": spare["id"], "templateKey": "delivery"},
            headers=auth(manager_key),
        )
    ).status_code == 201
    assert (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(manager_key))
    ).status_code == 403
    assert (await client.get("/api/v1/projects", headers=auth(manager_key))).status_code == 403
    assert (
        await client.post(
            "/api/v1/projects",
            json={"workspaceSlug": "denied", "templateKey": "delivery"},
            headers=auth(manager_key),
        )
    ).status_code == 403
    assert "denied" not in await list_workspace_slugs(client, admin_key)

    # Neither permission: nothing.
    assert (await client.get("/api/v1/projects", headers=auth(outsider_key))).status_code == 403
    assert (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(outsider_key))
    ).status_code == 403
    assert (
        await client.post(
            "/api/v1/projects",
            json={"workspaceId": spare["id"], "templateKey": "delivery"},
            headers=auth(outsider_key),
        )
    ).status_code == 403


async def test_project_tenant_isolation(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    template = await create_template(client, admin_key)
    workspace = await create_workspace(client, admin_key, "atlas")
    project = await create_project(
        client, admin_key, workspaceId=workspace["id"], templateKey="delivery"
    )

    _, key_b = make_tenant_directly(sync_engine, "tenant-b")

    assert (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(key_b))
    ).status_code == 404
    assert (
        await client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"customFields": {"x": 1}},
            headers={**auth(key_b), "If-Match": '"project-1"'},
        )
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/projects/{project['id']}:archive", headers=auth(key_b))
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/project-templates/{template['id']}", headers=auth(key_b))
    ).status_code == 404
    assert (await client.get("/api/v1/projects", headers=auth(key_b))).json()["items"] == []

    # Another tenant's workspace is not a place to hang a profile.
    assert (
        await client.post(
            "/api/v1/projects",
            json={"workspaceId": workspace["id"], "templateId": template["id"]},
            headers=auth(key_b),
        )
    ).status_code == 404

    # Tenant A is undisturbed.
    assert (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    ).status_code == 200


async def test_create_project_idempotency_replay(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    await create_template(client, admin_key)
    key = str(uuid.uuid4())
    payload = {"workspaceSlug": "atlas", "templateKey": "delivery"}

    first = await client.post(
        "/api/v1/projects", json=payload, headers={**auth(admin_key), "Idempotency-Key": key}
    )
    assert first.status_code == 201, first.text
    assert "idempotency-replayed" not in first.headers

    second = await client.post(
        "/api/v1/projects", json=payload, headers={**auth(admin_key), "Idempotency-Key": key}
    )
    assert second.status_code == 201
    assert second.headers.get("idempotency-replayed") == "true"
    assert second.json() == first.json()

    # One project, and one workspace: the replay re-created neither.
    listed = (await client.get("/api/v1/projects", headers=auth(admin_key))).json()["items"]
    assert [p["id"] for p in listed] == [first.json()["id"]]
    assert await list_workspace_slugs(client, admin_key) == ["atlas"]
