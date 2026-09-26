"""v0.5 project scope: a task's project is DERIVED from the workspace tree.

Guarantee under test (ADR-0035): there is no second ownership field. The owning
project of a task is the nearest ancestor workspace carrying a project profile,
``projectId`` is a query filter that expands into a workspace set BEFORE
pagination (so pages neither skip nor duplicate), and archiving a project stops
it offering NEW work without touching claims and runs already in flight.

The fixture tree used throughout is portfolio ``P`` (project A) -> workstream
``W`` (no profile, inherits A) -> nested ``N`` (project B, starts its own scope).
"""

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
    open_session,
)

TEMPLATE_KEY = "delivery"


async def _create_template(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/project-templates",
        json={
            "key": TEMPLATE_KEY,
            "displayName": "Delivery",
            "defaultConfig": {"settings": {"reviewRequired": True}},
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _create_project(
    client: httpx.AsyncClient, key: str, workspace_id: str, **extra: Any
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/projects",
        json={"workspaceId": workspace_id, "templateKey": TEMPLATE_KEY, **extra},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _build_tree(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    """P (project A) -> W (no project) -> N (project B), one task in each."""
    for type_key, display_name in (("portfolio", "Portfolio"), ("workstream", "Workstream")):
        response = await client.post(
            "/api/v1/workspace-types",
            json={"key": type_key, "displayName": display_name, "allowedChildTypes": ["*"]},
            headers=auth(key),
        )
        assert response.status_code == 201, response.text
    await _create_template(client, key)

    portfolio = await create_workspace(client, key, "portfolio-p", typeKey="portfolio")
    workstream = await create_workspace(
        client, key, "workstream-w", parent_id=portfolio["id"], typeKey="workstream"
    )
    nested = await create_workspace(client, key, "nested-n", parent_id=workstream["id"])

    return {
        "P": portfolio,
        "W": workstream,
        "N": nested,
        "A": await _create_project(client, key, portfolio["id"]),
        "B": await _create_project(client, key, nested["id"]),
        "taskP": await create_task(client, key, title="In P", workspaceId=portfolio["id"]),
        "taskW": await create_task(client, key, title="In W", workspaceId=workstream["id"]),
        "taskN": await create_task(client, key, title="In N", workspaceId=nested["id"]),
    }


async def _task_ids(client: httpx.AsyncClient, key: str, **params: Any) -> list[str]:
    """Every task id from GET /tasks, following nextCursor to the end."""
    ids: list[str] = []
    cursor: str | None = None
    while True:
        query = {**params, "cursor": cursor} if cursor else params
        response = await client.get("/api/v1/tasks", params=query, headers=auth(key))
        assert response.status_code == 200, response.text
        body = response.json()
        ids.extend(task["id"] for task in body["items"])
        cursor = body["nextCursor"]
        if cursor is None:
            return ids


async def _available_ids(client: httpx.AsyncClient, key: str, **params: Any) -> list[str]:
    """Every task id from GET /work/available, following nextCursor to the end."""
    ids: list[str] = []
    cursor: str | None = None
    while True:
        query = {**params, "cursor": cursor} if cursor else params
        response = await client.get("/api/v1/work/available", params=query, headers=auth(key))
        assert response.status_code == 200, response.text
        body = response.json()
        ids.extend(task["id"] for task in body["items"])
        cursor = body["nextCursor"]
        if cursor is None:
            return ids


async def test_task_project_id_is_derived_from_the_tree(client: httpx.AsyncClient) -> None:
    """A task in the project workspace and one in a plain descendant report the
    SAME project; a nested profile shadows its ancestors."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tree = await _build_tree(client, admin_key)
    orphan = await create_task(client, admin_key, title="No workspace")

    async def project_of(task_id: str) -> str | None:
        response = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(admin_key))
        assert response.status_code == 200, response.text
        return response.json()["projectId"]

    assert await project_of(tree["taskP"]["id"]) == tree["A"]["id"]
    assert await project_of(tree["taskW"]["id"]) == tree["A"]["id"]
    assert await project_of(tree["taskN"]["id"]) == tree["B"]["id"]
    # A task outside the tree belongs to no project at all (ADR-0035).
    assert await project_of(orphan["id"]) is None

    # The list path resolves the whole page, not one query per row.
    response = await client.get("/api/v1/tasks", headers=auth(admin_key))
    derived = {task["id"]: task["projectId"] for task in response.json()["items"]}
    assert derived[tree["taskP"]["id"]] == tree["A"]["id"]
    assert derived[tree["taskW"]["id"]] == tree["A"]["id"]
    assert derived[tree["taskN"]["id"]] == tree["B"]["id"]
    assert derived[orphan["id"]] is None


async def test_project_filter_is_exact_scope_by_default(client: httpx.AsyncClient) -> None:
    """projectId=A is the exact scope; includeSubprojects widens to the subtree."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tree = await _build_tree(client, admin_key)

    exact = await _task_ids(client, admin_key, projectId=tree["A"]["id"])
    assert set(exact) == {tree["taskP"]["id"], tree["taskW"]["id"]}
    assert tree["taskN"]["id"] not in exact

    widened = await _task_ids(
        client, admin_key, projectId=tree["A"]["id"], includeSubprojects="true"
    )
    assert set(widened) == {tree["taskP"]["id"], tree["taskW"]["id"], tree["taskN"]["id"]}

    # The nested project sees only its own scope, either way.
    assert await _task_ids(client, admin_key, projectId=tree["B"]["id"]) == [tree["taskN"]["id"]]
    assert await _task_ids(
        client, admin_key, projectId=tree["B"]["id"], includeSubprojects="true"
    ) == [tree["taskN"]["id"]]


async def test_available_work_honours_project_scope(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    tree = await _build_tree(client, admin_key)

    exact = await _available_ids(client, agent_key, projectId=tree["A"]["id"])
    assert set(exact) == {tree["taskP"]["id"], tree["taskW"]["id"]}

    widened = await _available_ids(
        client, agent_key, projectId=tree["A"]["id"], includeSubprojects="true"
    )
    assert set(widened) == {tree["taskP"]["id"], tree["taskW"]["id"], tree["taskN"]["id"]}

    assert await _available_ids(client, agent_key, projectId=tree["B"]["id"]) == [
        tree["taskN"]["id"]
    ]


async def test_archived_project_stops_offering_work_but_keeps_claims(
    client: httpx.AsyncClient,
) -> None:
    """Archiving B retires its NEW work only: a live claim and its run survive,
    because the claim — not the project status — is the authoritative gate."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    tree = await _build_tree(client, admin_key)
    in_flight = await create_task(
        client, admin_key, title="Already claimed", workspaceId=tree["N"]["id"]
    )

    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{in_flight['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{in_flight['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()

    response = await client.post(
        f"/api/v1/projects/{tree['B']['id']}:archive", headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "archived"

    # No filter, exact project scope, subtree scope: B offers nothing anywhere.
    unfiltered = await _available_ids(client, agent_key)
    assert tree["taskN"]["id"] not in unfiltered
    assert {tree["taskP"]["id"], tree["taskW"]["id"]} <= set(unfiltered)
    assert await _available_ids(client, agent_key, projectId=tree["B"]["id"]) == []
    assert (
        await _available_ids(
            client, agent_key, projectId=tree["B"]["id"], includeSubprojects="true"
        )
        == []
    )
    widened = await _available_ids(
        client, agent_key, projectId=tree["A"]["id"], includeSubprojects="true"
    )
    assert set(widened) == {tree["taskP"]["id"], tree["taskW"]["id"]}

    # Listing is unaffected: archiving is a discovery rule, not a data filter.
    assert set(await _task_ids(client, admin_key, projectId=tree["B"]["id"])) == {
        tree["taskN"]["id"],
        in_flight["id"],
    }

    # The in-flight claim keeps its lease and the run still completes.
    still_claimed = await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(agent_key))
    assert still_claimed.json()["status"] == "active"
    beat = await client.post(f"/api/v1/claims/{claim['id']}:heartbeat", headers=auth(agent_key))
    assert beat.status_code == 200, beat.text
    finished = await client.post(f"/api/v1/runs/{run['id']}:succeed", headers=auth(agent_key))
    assert finished.status_code == 200, finished.text
    assert finished.json()["run"]["status"] == "succeeded"
    assert finished.json()["task"]["status"] == "done"


async def test_workspace_filters_are_unchanged(client: httpx.AsyncClient) -> None:
    """Regression: the v0.3 workspaceId/includeDescendants semantics stand, and
    combining them with projectId intersects both sets."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    tree = await _build_tree(client, admin_key)

    assert await _task_ids(client, admin_key, workspaceId=tree["W"]["id"]) == [tree["taskW"]["id"]]
    assert set(
        await _task_ids(client, admin_key, workspaceId=tree["W"]["id"], includeDescendants="true")
    ) == {tree["taskW"]["id"], tree["taskN"]["id"]}
    assert await _task_ids(client, admin_key, workspaceId=tree["P"]["id"]) == [tree["taskP"]["id"]]
    assert set(
        await _task_ids(client, admin_key, workspaceId=tree["P"]["id"], includeDescendants="true")
    ) == {tree["taskP"]["id"], tree["taskW"]["id"], tree["taskN"]["id"]}

    assert set(
        await _available_ids(
            client, agent_key, workspaceId=tree["P"]["id"], includeDescendants="true"
        )
    ) == {tree["taskP"]["id"], tree["taskW"]["id"], tree["taskN"]["id"]}
    assert await _available_ids(client, agent_key, workspaceId=tree["N"]["id"]) == [
        tree["taskN"]["id"]
    ]

    # projectId + workspaceId is an intersection, not a replacement.
    assert await _task_ids(
        client, admin_key, projectId=tree["A"]["id"], workspaceId=tree["W"]["id"]
    ) == [tree["taskW"]["id"]]
    assert (
        await _task_ids(client, admin_key, projectId=tree["A"]["id"], workspaceId=tree["N"]["id"])
        == []
    )
    assert await _available_ids(
        client, agent_key, projectId=tree["A"]["id"], workspaceId=tree["W"]["id"]
    ) == [tree["taskW"]["id"]]

    # An unknown workspace is still a 404, with or without a project filter.
    missing = "00000000-0000-0000-0000-000000000001"
    response = await client.get(
        "/api/v1/tasks",
        params={"workspaceId": missing, "includeDescendants": "true"},
        headers=auth(admin_key),
    )
    assert response.status_code == 404


async def test_project_pagination_returns_every_task_once(client: httpx.AsyncClient) -> None:
    """The filter is applied before pagination, so paging a project scope can
    neither skip nor duplicate a task."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tree = await _build_tree(client, admin_key)

    expected = {tree["taskP"]["id"], tree["taskW"]["id"]}
    for index in range(7):
        workspace = tree["P"] if index % 2 == 0 else tree["W"]
        task = await create_task(
            client, admin_key, title=f"Scoped {index}", workspaceId=workspace["id"]
        )
        expected.add(task["id"])
    # Noise inside the nested project must never leak into the exact scope.
    noise = await create_task(client, admin_key, title="Nested noise", workspaceId=tree["N"]["id"])

    first = await client.get(
        "/api/v1/tasks",
        params={"projectId": tree["A"]["id"], "limit": 2},
        headers=auth(admin_key),
    )
    assert len(first.json()["items"]) == 2
    assert first.json()["nextCursor"] is not None

    paged = await _task_ids(client, admin_key, projectId=tree["A"]["id"], limit=2)
    assert len(paged) == len(set(paged)), "a page boundary duplicated a task"
    assert set(paged) == expected
    assert noise["id"] not in paged


async def test_project_from_another_tenant_is_not_found(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    await _build_tree(client, admin_key)

    _tenant_id, other_key = make_tenant_directly(sync_engine, "tenant-b")
    await _create_template(client, other_key)
    other_workspace = await create_workspace(client, other_key, "their-portfolio")
    foreign = await _create_project(client, other_key, other_workspace["id"])

    response = await client.get(
        "/api/v1/tasks", params={"projectId": foreign["id"]}, headers=auth(admin_key)
    )
    assert response.status_code == 404
    response = await client.get(
        "/api/v1/work/available", params={"projectId": foreign["id"]}, headers=auth(admin_key)
    )
    assert response.status_code == 404


async def test_context_project_focus_returns_the_derived_projection(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tree = await _build_tree(client, admin_key)

    response = await client.post(
        "/api/v1/context", json={"projectId": tree["B"]["id"]}, headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    project = response.json()["operational"]["project"]

    assert project["id"] == tree["B"]["id"]
    assert project["workspaceId"] == tree["N"]["id"]
    # Parent is derived from the tree, never stored on the profile.
    assert project["parentProjectId"] == tree["A"]["id"]
    assert project["templateKey"] == TEMPLATE_KEY
    assert project["statusKey"] == "planned"
    assert project["systemStatusCategory"] == "planned"
    assert project["workspaceScope"] == [tree["N"]["id"]]
    assert project["effectiveConfig"]["settings"]["reviewRequired"] is True
    # B inherits the setting through A, and the provenance says so.
    origin = project["configProvenance"]["settings"]["reviewRequired"]
    assert origin["source"] == "ancestor"
    assert origin["projectId"] == tree["A"]["id"]
    # The provenance chain shows the ancestry that produced the config.
    assert [layer["projectId"] for layer in project["configProvenance"]["layers"]] == [
        tree["A"]["id"],
        tree["B"]["id"],
    ]

    # The same two scope semantics as the list filters.
    exact = await client.post(
        "/api/v1/context", json={"projectId": tree["A"]["id"]}, headers=auth(admin_key)
    )
    root = exact.json()["operational"]["project"]
    assert set(root["workspaceScope"]) == {tree["P"]["id"], tree["W"]["id"]}
    assert root["parentProjectId"] is None
    assert root["configProvenance"]["settings"]["reviewRequired"]["source"] == "template"
    widened = await client.post(
        "/api/v1/context",
        json={"projectId": tree["A"]["id"], "includeSubprojects": True},
        headers=auth(admin_key),
    )
    assert set(widened.json()["operational"]["project"]["workspaceScope"]) == {
        tree["P"]["id"],
        tree["W"]["id"],
        tree["N"]["id"],
    }


async def test_context_project_focus_is_tenant_scoped_and_gated(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A foreign project is a 404 and a key without projects.read a 403 —
    both decided before any provider could see the scope."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tree = await _build_tree(client, admin_key)

    _tenant_id, other_key = make_tenant_directly(sync_engine, "tenant-b")
    await _create_template(client, other_key)
    other_workspace = await create_workspace(client, other_key, "their-portfolio")
    foreign = await _create_project(client, other_key, other_workspace["id"])

    response = await client.post(
        "/api/v1/context", json={"projectId": foreign["id"]}, headers=auth(admin_key)
    )
    assert response.status_code == 404

    _, limited_key = await create_agent_with_key(
        client, admin_key, name="no-projects", permissions=["sessions.open", "tasks.read"]
    )
    response = await client.post(
        "/api/v1/context", json={"projectId": tree["A"]["id"]}, headers=auth(limited_key)
    )
    assert response.status_code == 403
    assert response.json()["error"]["details"]["required"] == ["projects.read"]

    # Without a project focus the same key still gets its own operational state.
    response = await client.post("/api/v1/context", json={}, headers=auth(limited_key))
    assert response.status_code == 200


async def test_project_context_degrades_without_memory_provider(
    client: httpx.AsyncClient, app: Any
) -> None:
    """Memory disabled must not cost the operational half: 200 + "disabled"."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tree = await _build_tree(client, admin_key)
    app.state.context_provider = None

    response = await client.post(
        "/api/v1/context", json={"projectId": tree["A"]["id"]}, headers=auth(admin_key)
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["memoryStatus"] == "disabled"
    assert body["memory"] is None
    assert "context provider is not configured" in body["warnings"][0]
    assert body["operational"]["project"]["id"] == tree["A"]["id"]
    assert body["operational"]["project"]["effectiveConfig"]["settings"]["reviewRequired"] is True
