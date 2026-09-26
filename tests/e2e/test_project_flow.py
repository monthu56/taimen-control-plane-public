"""End-to-end v0.5: portfolio -> project -> work -> config -> lifecycle -> replay.

Two scenarios, both exercising the whole stack through the public API only:

1. the full project lifecycle a harness actually walks, ending with an event
   replay and a Context Adapter catch-up that reconnects from a durable opaque
   cursor;
2. governance inheritance across a project hierarchy, including a rejected
   weakening and a move that recomputes the effective config.
"""

from typing import Any

import httpx

from control_plane.application.context.mapping import MAPPING_VERSION
from control_plane.worker.context_adapter import ContextAdapter
from tests.helpers import BOOTSTRAP_TOKEN, auth
from tests.integration.test_context_adapter_v05 import FakeMemory

AGENT_PERMISSIONS = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "events.read",
    "artifacts.read",
    "artifacts.write",
    "observations.write",
    "projects.read",
    "project_templates.read",
]

LIFECYCLE = {
    "initialStatus": "discovery",
    "statuses": [
        {"key": "discovery", "displayName": "Discovery", "category": "planned"},
        {"key": "delivery", "displayName": "Delivery", "category": "active"},
        {"key": "shipped", "displayName": "Shipped", "category": "terminal_success"},
        {"key": "dropped", "displayName": "Dropped", "category": "terminal_cancelled"},
    ],
    "transitions": [
        {"from": "discovery", "to": ["delivery", "dropped"]},
        {"from": "delivery", "to": ["shipped", "dropped"]},
    ],
}


async def _bootstrap(client: httpx.AsyncClient) -> tuple[str, str, dict[str, Any]]:
    response = await client.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "acme", "tenantName": "Acme Corp", "adminDisplayName": "Alice"},
        headers=auth(BOOTSTRAP_TOKEN),
    )
    assert response.status_code == 201, response.text
    boot = response.json()
    admin_key = boot["apiKey"]["key"]

    agent = (
        await client.post(
            "/api/v1/principals",
            json={"kind": "agent", "displayName": "Delivery Agent"},
            headers=auth(admin_key),
        )
    ).json()
    agent_key = (
        await client.post(
            f"/api/v1/principals/{agent['id']}/api-keys",
            json={"permissions": AGENT_PERMISSIONS},
            headers=auth(admin_key),
        )
    ).json()["key"]
    return admin_key, agent_key, boot


async def test_project_lifecycle_end_to_end(client: httpx.AsyncClient, settings, app) -> None:
    admin_key, agent_key, boot = await _bootstrap(client)
    tenant_id = boot["tenant"]["id"]

    # 1. Workspace types describe the shape of the tree.
    for key, display, children in (
        ("portfolio", "Portfolio", ["project"]),
        ("project", "Project", ["workstream"]),
        ("workstream", "Workstream", []),
    ):
        response = await client.post(
            "/api/v1/workspace-types",
            json={"key": key, "displayName": display, "allowedChildTypes": children},
            headers=auth(admin_key),
        )
        assert response.status_code == 201, response.text

    # 2. A template version pins the lifecycle and the baseline config.
    template = (
        await client.post(
            "/api/v1/project-templates",
            json={
                "key": "delivery",
                "displayName": "Delivery",
                "lifecycleSchema": LIFECYCLE,
                "fieldSchema": {
                    "type": "object",
                    "properties": {"codename": {"type": "string"}},
                    "required": ["codename"],
                },
                "defaultConfig": {
                    "settings": {"tone": "neutral"},
                    "governance": {"maxRunActions": 100},
                },
                "defaultViews": [{"key": "overview"}, {"key": "work"}],
            },
            headers=auth(admin_key),
        )
    ).json()
    assert template["version"] == 1

    # 3. Portfolio workspace, then the project on a workspace beneath it.
    portfolio = (
        await client.post(
            "/api/v1/workspaces",
            json={"slug": "acme-portfolio", "name": "Portfolio", "typeKey": "portfolio"},
            headers=auth(admin_key),
        )
    ).json()
    project = (
        await client.post(
            "/api/v1/projects",
            json={
                "workspaceSlug": "apollo",
                "workspaceName": "Apollo",
                "parentWorkspaceId": portfolio["id"],
                "workspaceTypeKey": "project",
                "templateId": template["id"],
                "customFields": {"codename": "apollo"},
            },
            headers=auth(admin_key),
        )
    ).json()
    assert project["statusKey"] == "discovery"
    assert project["systemStatusCategory"] == "planned"
    assert project["templateKey"] == "delivery"

    # 4. A workstream inside the project inherits it without its own profile.
    workstream = (
        await client.post(
            "/api/v1/workspaces",
            json={
                "slug": "backend",
                "name": "Backend",
                "parentId": project["workspaceId"],
                "typeKey": "workstream",
            },
            headers=auth(admin_key),
        )
    ).json()

    tree = (
        await client.get(
            "/api/v1/workspaces/tree", params={"rootId": portfolio["id"]}, headers=auth(admin_key)
        )
    ).json()["roots"]
    assert len(tree) == 1
    assert tree[0]["project"] is None
    assert tree[0]["children"][0]["project"]["id"] == project["id"]
    assert tree[0]["children"][0]["children"][0]["slug"] == "backend"

    # 5. A task in the workstream belongs to the project by derivation.
    task = (
        await client.post(
            "/api/v1/tasks",
            json={"title": "Ship the API", "workspaceId": workstream["id"]},
            headers=auth(admin_key),
        )
    ).json()
    assert task["projectId"] == project["id"]

    # 6. Discovery by project finds it.
    available = (
        await client.get(
            "/api/v1/work/available",
            params={"projectId": project["id"]},
            headers=auth(agent_key),
        )
    ).json()["items"]
    assert [t["id"] for t in available] == [task["id"]]

    # 7. Session, claim, run.
    session = (
        await client.post(
            "/api/v1/sessions",
            json={
                "clientName": "delivery-agent",
                "harness": {"type": "cli", "protocolVersion": "2", "capabilities": ["resume"]},
            },
            headers=auth(agent_key),
        )
    ).json()
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()

    # 8. Project-focused working context (memory disabled -> degraded, HTTP 200).
    context = (
        await client.post(
            "/api/v1/context",
            json={"projectId": project["id"], "task": task["id"]},
            headers=auth(agent_key),
        )
    ).json()
    assert context["memoryStatus"] == "disabled"
    project_block = context["operational"]["project"]
    assert project_block["id"] == project["id"]
    assert project_block["statusKey"] == "discovery"
    assert project_block["effectiveConfig"]["settings"]["tone"] == "neutral"
    assert project_block["effectiveConfig"]["views"] == [{"key": "overview"}, {"key": "work"}]
    assert workstream["id"] in project_block["workspaceScope"]
    assert context["operational"]["focus"]["task"]["id"] == task["id"]

    # 9. A config revision is written, then activated as a separate act.
    revision = (
        await client.post(
            f"/api/v1/projects/{project['id']}/config-revisions",
            json={
                "config": {"settings": {"tone": "formal", "sla": {"hours": 4}}},
                "comment": "tighten the tone",
            },
            headers=auth(admin_key),
        )
    ).json()
    assert revision["revision"] == 1 and revision["activatedAt"] is None
    current = (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    ).json()
    assert current["activeConfigRevision"] is None

    activated = await client.post(
        f"/api/v1/projects/{project['id']}/config-revisions/1:activate",
        headers={**auth(admin_key), "If-Match": f'"project-{current["version"]}"'},
    )
    assert activated.status_code == 200, activated.text
    effective = (
        await client.get(
            f"/api/v1/projects/{project['id']}/effective-config", headers=auth(admin_key)
        )
    ).json()
    assert effective["config"]["settings"] == {"tone": "formal", "sla": {"hours": 4}}
    assert effective["provenance"]["settings"]["tone"]["source"] == "revision"

    # 10. Lifecycle transition.
    project_now = (
        await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
    ).json()
    transitioned = await client.post(
        f"/api/v1/projects/{project['id']}:transition",
        json={"statusKey": "delivery", "comment": "kickoff"},
        headers={**auth(admin_key), "If-Match": f'"project-{project_now["version"]}"'},
    )
    assert transitioned.status_code == 200, transitioned.text
    assert transitioned.json()["systemStatusCategory"] == "active"

    # 11. Finish the run and the task.
    succeeded = await client.post(
        f"/api/v1/runs/{run['id']}:succeed",
        json={"output": {"ok": True}, "completeTask": True},
        headers=auth(agent_key),
    )
    assert succeeded.status_code == 200, succeeded.text
    final_task = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))).json()
    assert final_task["status"] == "done"
    assert final_task["projectId"] == project["id"]

    # 12. Replay: project and task history are both in the journal.
    page = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(agent_key))
    ).json()
    types = [event["type"] for event in page["items"]]
    for expected in (
        "workspace_type.created",
        "project_template.created",
        "workspace.created",
        "project.created",
        "task.created",
        "task.claimed",
        "run.started",
        "project.config_revision_created",
        "project.config_revision_activated",
        "project.status_changed",
        "run.succeeded",
        "task.completed",
    ):
        assert expected in types, f"{expected} missing from the replay"
    assert page["nextCursor"].startswith("ec1_")
    assert all(event["traceRunId"] for event in page["items"]), "every event carries a trace id"

    # 13. Context Adapter catch-up over the same journal.
    memory = FakeMemory()
    adapter = ContextAdapter(settings, engine=app.state.engine, provider=memory)
    moved = 0
    for _ in range(10):
        step = await adapter.deliver_once()
        moved += step
        if step == 0:
            break
    assert moved >= len(page["items"]) - 1
    delivered_kinds = {observation["kind"] for observation in memory.store.values()}
    assert "project.created" in delivered_kinds
    assert "project.status_changed" in delivered_kinds
    # Whitelisting is narrow: template-authored custom fields never leave.
    created = next(o for o in memory.store.values() if o["kind"] == "project.created")
    assert "customFields" not in created["data"]
    assert created["data"]["templateKey"] == "delivery"
    assert created["data"]["mappingVersion"] == MAPPING_VERSION

    # 14. Reconnect from a durable opaque cursor: no duplicates, no gaps.
    first_page = (
        await client.get("/api/v1/events", params={"limit": 5}, headers=auth(agent_key))
    ).json()
    resumed = (
        await client.get(
            "/api/v1/events",
            params={"limit": 200, "cursor": first_page["nextCursor"]},
            headers=auth(agent_key),
        )
    ).json()
    ids_first = [event["id"] for event in first_page["items"]]
    ids_rest = [event["id"] for event in resumed["items"]]
    assert set(ids_first).isdisjoint(ids_rest)
    assert ids_first + ids_rest == [event["id"] for event in page["items"]]

    diagnostics = (
        await client.get("/api/v1/operations/context-adapter", headers=auth(admin_key))
    ).json()
    assert diagnostics["tenantId"] == tenant_id
    assert diagnostics["parked"] is False


async def test_governance_inheritance_end_to_end(client: httpx.AsyncClient) -> None:
    admin_key, _agent_key, _boot = await _bootstrap(client)

    template = (
        await client.post(
            "/api/v1/project-templates",
            json={"key": "generic", "displayName": "Generic"},
            headers=auth(admin_key),
        )
    ).json()

    async def make_project(slug: str, parent_workspace: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"workspaceSlug": slug, "templateId": template["id"]}
        if parent_workspace is not None:
            body["parentWorkspaceId"] = parent_workspace
        response = await client.post("/api/v1/projects", json=body, headers=auth(admin_key))
        assert response.status_code == 201, response.text
        return response.json()

    async def set_governance(project: dict[str, Any], governance: dict[str, Any]) -> httpx.Response:
        created = await client.post(
            f"/api/v1/projects/{project['id']}/config-revisions",
            json={"config": {"governance": governance}},
            headers=auth(admin_key),
        )
        if created.status_code != 201:
            return created
        revision = created.json()["revision"]
        current = (
            await client.get(f"/api/v1/projects/{project['id']}", headers=auth(admin_key))
        ).json()
        return await client.post(
            f"/api/v1/projects/{project['id']}/config-revisions/{revision}:activate",
            headers={**auth(admin_key), "If-Match": f'"project-{current["version"]}"'},
        )

    # 1. A parent project with a real ceiling.
    parent = await make_project("parent")
    assert (
        await set_governance(
            parent,
            {
                "maxRunActions": 50,
                "requireApprovalForRun": True,
                "allowedSkillProtocols": ["http", "mcp"],
            },
        )
    ).status_code == 200

    # 2. A child project under it inherits that ceiling.
    child = await make_project("child", parent_workspace=parent["workspaceId"])
    effective = (
        await client.get(
            f"/api/v1/projects/{child['id']}/effective-config", headers=auth(admin_key)
        )
    ).json()
    assert effective["config"]["governance"] == {
        "maxRunActions": 50,
        "requireApprovalForRun": True,
        "allowedSkillProtocols": ["http", "mcp"],
    }
    assert effective["provenance"]["governance"]["maxRunActions"]["source"] == "ancestor"
    assert effective["provenance"]["governance"]["maxRunActions"]["projectId"] == parent["id"]

    child_body = (
        await client.get(f"/api/v1/projects/{child['id']}", headers=auth(admin_key))
    ).json()
    assert child_body["parentProjectId"] == parent["id"]

    # 3. Weakening is refused, with the offending paths named.
    weakened = await set_governance(child, {"maxRunActions": 500})
    assert weakened.status_code == 422
    error = weakened.json()["error"]
    assert error["code"] == "governance_weakened"
    assert error["details"]["violations"][0]["path"] == "/governance/maxRunActions"

    dropped_approval = await set_governance(child, {"requireApprovalForRun": False})
    assert dropped_approval.status_code == 422
    widened = await set_governance(child, {"allowedSkillProtocols": ["http", "mcp", "local"]})
    assert widened.status_code == 422
    unbounded = await set_governance(child, {"maxRunActions": None})
    assert unbounded.status_code == 422

    # 4. Tightening is accepted and shows up as the child's own layer.
    assert (await set_governance(child, {"maxRunActions": 5})).status_code == 200
    effective = (
        await client.get(
            f"/api/v1/projects/{child['id']}/effective-config", headers=auth(admin_key)
        )
    ).json()
    assert effective["config"]["governance"]["maxRunActions"] == 5
    assert effective["provenance"]["governance"]["maxRunActions"]["source"] == "revision"

    # 5. Move the child under a laxer parent: the effective config recomputes.
    other = await make_project("other-parent")
    assert (await set_governance(other, {"maxRunActions": 1000})).status_code == 200
    moved = await client.post(
        f"/api/v1/workspaces/{child['workspaceId']}:move",
        json={"newParentId": other["workspaceId"]},
        headers=auth(admin_key),
    )
    assert moved.status_code == 200, moved.text

    after_move = (
        await client.get(f"/api/v1/projects/{child['id']}", headers=auth(admin_key))
    ).json()
    assert after_move["parentProjectId"] == other["id"]
    effective = (
        await client.get(
            f"/api/v1/projects/{child['id']}/effective-config", headers=auth(admin_key)
        )
    ).json()
    # The child's own tightening survives; the inherited approval gate does not
    # (the new ancestor never required it).
    assert effective["config"]["governance"]["maxRunActions"] == 5
    assert "requireApprovalForRun" not in effective["config"]["governance"]
    assert [layer["projectId"] for layer in effective["provenance"]["layers"]] == [
        other["id"],
        child["id"],
    ]

    # 6. A move that would break the ceiling is refused whole.
    lax_child = await make_project("lax-child", parent_workspace=other["workspaceId"])
    assert (await set_governance(lax_child, {"maxRunActions": 900})).status_code == 200
    refused = await client.post(
        f"/api/v1/workspaces/{lax_child['workspaceId']}:move",
        json={"newParentId": parent["workspaceId"]},
        headers=auth(admin_key),
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "governance_weakened"
    unchanged = (
        await client.get(f"/api/v1/workspaces/{lax_child['workspaceId']}", headers=auth(admin_key))
    ).json()
    assert unchanged["parentId"] == other["workspaceId"], "a refused move changes nothing"
