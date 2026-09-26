"""Artifacts (append-only work products) and approvals (one record = one decision)."""

import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    assign_role,
    auth,
    claim_task,
    create_agent_with_key,
    create_role,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)

DECIDER_PERMISSIONS = ["approvals.read", "approvals.decide"]


async def test_artifact_creation_and_linking(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    from tests.helpers import ORG_AGENT_PERMISSIONS

    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    session = await open_session(client, agent_key)
    task = await create_task(client, admin_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()

    # Artifact linked to a run inherits the task automatically.
    response = await client.post(
        "/api/v1/artifacts",
        json={
            "type": "github.pull_request",
            "name": "PR #42",
            "runId": run["id"],
            "uri": "https://github.com/acme/repo/pull/42",
            "content": {"additions": 10},
        },
        headers=auth(agent_key),
    )
    assert response.status_code == 201, response.text
    artifact = response.json()
    assert artifact["taskId"] == task["id"]
    assert artifact["runId"] == run["id"]

    # The event references the artifact but never carries its content.
    events = (await client.get("/api/v1/events", headers=auth(admin_key))).json()["items"]
    created = next(e for e in events if e["type"] == "artifact.created")
    assert created["payload"]["type"] == "github.pull_request"
    assert "content" not in created["payload"]
    assert "additions" not in str(created["payload"])

    # Run/task mismatch is rejected.
    other = await create_task(client, admin_key, title="Other")
    response = await client.post(
        "/api/v1/artifacts",
        json={"type": "x", "name": "X", "runId": run["id"], "task": other["id"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "artifact_mismatch"

    # Filtered listing
    items = (
        await client.get(f"/api/v1/artifacts?taskId={task['id']}", headers=auth(admin_key))
    ).json()["items"]
    assert len(items) == 1


async def test_artifact_permissions_and_isolation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, limited_key = await create_agent_with_key(client, admin_key, permissions=["tasks.read"])
    assert (
        await client.post(
            "/api/v1/artifacts", json={"type": "t", "name": "n"}, headers=auth(limited_key)
        )
    ).status_code == 403
    assert (await client.get("/api/v1/artifacts", headers=auth(limited_key))).status_code == 403

    artifact = (
        await client.post(
            "/api/v1/artifacts", json={"type": "doc", "name": "D"}, headers=auth(admin_key)
        )
    ).json()
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    assert (
        await client.get(f"/api/v1/artifacts/{artifact['id']}", headers=auth(key_b))
    ).status_code == 404


async def test_approval_assigned_principal_flow(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    reviewer, reviewer_key = await create_agent_with_key(
        client, admin_key, name="reviewer", permissions=DECIDER_PERMISSIONS
    )
    __, outsider_key = await create_agent_with_key(
        client, admin_key, name="outsider", permissions=DECIDER_PERMISSIONS
    )
    task = await create_task(client, admin_key)

    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"task": task["id"], "assignedPrincipalId": reviewer["id"]},
            headers=auth(admin_key),
        )
    ).json()
    assert approval["status"] == "pending"

    # Wrong principal (even with approvals.decide) -> not eligible
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve", headers=auth(outsider_key)
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_eligible"

    # Assigned principal decides
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve",
        json={"comment": "LGTM"},
        headers=auth(reviewer_key),
    )
    assert response.status_code == 200
    decided = response.json()
    assert decided["status"] == "approved"
    assert decided["decisionByPrincipalId"] == reviewer["id"]
    assert decided["comment"] == "LGTM"

    # Exactly one terminal decision
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:reject", headers=auth(reviewer_key)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_already_decided"


async def test_approval_role_based_flow_with_workspace_scope(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    legal = await create_workspace(client, admin_key, "legal")
    contracts = await create_workspace(client, admin_key, "contracts", parent_id=legal["id"])
    role = await create_role(client, admin_key, "legal-lead")

    lead, lead_key = await create_agent_with_key(
        client, admin_key, name="lead", permissions=DECIDER_PERMISSIONS
    )
    # Role granted on the PARENT workspace covers approvals in the child scope.
    await assign_role(client, admin_key, lead["id"], role["id"], workspace_id=legal["id"])

    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"requiredRoleId": role["id"], "workspaceId": contracts["id"]},
            headers=auth(admin_key),
        )
    ).json()

    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:reject",
        json={"comment": "indemnity clause is unacceptable"},
        headers=auth(lead_key),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"

    events = (await client.get("/api/v1/events", headers=auth(admin_key))).json()["items"]
    types = [e["type"] for e in events]
    assert "approval.requested" in types
    assert "approval.rejected" in types


async def test_approval_validation_and_cancel(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    role = await create_role(client, admin_key, "some-role")
    principal = body["adminPrincipal"]["id"]

    # Exactly one addressing mode
    for payload in (
        {},
        {"requiredRoleId": role["id"], "assignedPrincipalId": principal},
    ):
        response = await client.post("/api/v1/approvals", json=payload, headers=auth(admin_key))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_approval"

    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"assignedPrincipalId": principal},
            headers=auth(admin_key),
        )
    ).json()
    # Cancel; cancel again is idempotent; decide after cancel conflicts.
    assert (
        await client.post(f"/api/v1/approvals/{approval['id']}:cancel", headers=auth(admin_key))
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/approvals/{approval['id']}:cancel", headers=auth(admin_key))
    ).status_code == 200
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:approve", headers=auth(admin_key)
    )
    assert response.status_code == 409


async def test_approval_permissions_and_isolation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal = body["adminPrincipal"]["id"]
    _, limited_key = await create_agent_with_key(client, admin_key, permissions=["tasks.read"])

    assert (
        await client.post(
            "/api/v1/approvals",
            json={"assignedPrincipalId": principal},
            headers=auth(limited_key),
        )
    ).status_code == 403
    assert (await client.get("/api/v1/approvals", headers=auth(limited_key))).status_code == 403

    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"assignedPrincipalId": principal},
            headers=auth(admin_key),
        )
    ).json()
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    assert (
        await client.get(f"/api/v1/approvals/{approval['id']}", headers=auth(key_b))
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/approvals/{approval['id']}:approve", headers=auth(key_b))
    ).status_code == 404
