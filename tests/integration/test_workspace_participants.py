"""Participants of a workspace: explicit members and holders of its roles
(CP-ADR-0010 amendment, TASK-001194)."""

from typing import Any

import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)


async def _participants(
    client: httpx.AsyncClient, key: str, workspace_id: str, **params: Any
) -> dict[str, Any]:
    response = await client.get(
        f"/api/v1/workspaces/{workspace_id}/participants", params=params, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _by_principal(
    client: httpx.AsyncClient, key: str, workspace_id: str
) -> dict[str, dict[str, Any]]:
    return {p["principalId"]: p for p in (await _participants(client, key, workspace_id))["items"]}


async def _members(client: httpx.AsyncClient, key: str, workspace_id: str) -> list[str]:
    response = await client.get(f"/api/v1/workspaces/{workspace_id}/members", headers=auth(key))
    assert response.status_code == 200, response.text
    return [m["principalId"] for m in response.json()["items"]]


async def _role_holders(
    client: httpx.AsyncClient, key: str, role_id: str, workspace_id: str
) -> set[str]:
    response = await client.get(
        f"/api/v1/roles/{role_id}/principals",
        params={"workspaceId": workspace_id},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    return {p["id"] for p in response.json()["items"]}


async def test_role_holder_without_membership_is_a_participant(
    client: httpx.AsyncClient,
) -> None:
    """R024: both roles of the workspace are held, explicit membership is empty."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    invoices = await create_workspace(client, admin_key, "invoices")
    accounting = await create_role(client, admin_key, "accounting", workspace_id=invoices["id"])
    cfo = await create_role(client, admin_key, "cfo", workspace_id=invoices["id"])
    owner, _ = await create_agent_with_key(client, admin_key, name="Owner", kind="human")
    # One tenant-wide (the staging workaround), one scoped to the workspace.
    await assign_role(client, admin_key, owner["id"], accounting["id"])
    await assign_role(client, admin_key, owner["id"], cfo["id"], workspace_id=invoices["id"])

    assert await _members(client, admin_key, invoices["id"]) == []

    body = await _participants(client, admin_key, invoices["id"])
    assert body["nextCursor"] is None
    assert len(body["items"]) == 1
    participant = body["items"][0]
    assert participant["principalId"] == owner["id"]
    assert participant["kind"] == "human"
    assert participant["displayName"] == "Owner"
    assert participant["status"] == "active"
    assert participant["member"] is False
    assert participant["roles"] == [
        {
            "roleId": accounting["id"],
            "slug": "accounting",
            "name": "Accounting",
            "roleWorkspaceId": invoices["id"],
            "assignmentWorkspaceId": None,
        },
        {
            "roleId": cfo["id"],
            "slug": "cfo",
            "name": "Cfo",
            "roleWorkspaceId": invoices["id"],
            "assignmentWorkspaceId": invoices["id"],
        },
    ]
    # Agreed with the holders of every role of the workspace.
    for role in (accounting, cfo):
        assert await _role_holders(client, admin_key, role["id"], invoices["id"]) == {owner["id"]}


async def test_member_flag_and_roles_combine(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, admin_key, "ops")
    role = await create_role(client, admin_key, "operator", workspace_id=ws["id"])
    only_member, _ = await create_agent_with_key(client, admin_key, name="member")
    both, _ = await create_agent_with_key(client, admin_key, name="both")
    only_role, _ = await create_agent_with_key(client, admin_key, name="role")
    for principal in (only_member, both):
        response = await client.post(
            f"/api/v1/workspaces/{ws['id']}/members",
            json={"principalId": principal["id"]},
            headers=auth(admin_key),
        )
        assert response.status_code == 201
    for principal in (both, only_role):
        await assign_role(client, admin_key, principal["id"], role["id"], workspace_id=ws["id"])
    # Assigning again is idempotent: no duplicate role in the participant.
    await assign_role(client, admin_key, both["id"], role["id"], workspace_id=ws["id"])

    got = await _by_principal(client, admin_key, ws["id"])
    assert set(got) == {only_member["id"], both["id"], only_role["id"]}
    assert (got[only_member["id"]]["member"], got[only_member["id"]]["roles"]) == (True, [])
    assert got[both["id"]]["member"] is True
    assert [r["roleId"] for r in got[both["id"]]["roles"]] == [role["id"]]
    assert got[only_role["id"]]["member"] is False
    assert [r["roleId"] for r in got[only_role["id"]]["roles"]] == [role["id"]]

    # Members stay the explicit list; participants add the role-only holder.
    assert set(await _members(client, admin_key, ws["id"])) == {only_member["id"], both["id"]}

    # Revoking the role leaves an explicit member; revoking the last reason drops out.
    for principal in (both, only_role):
        response = await client.post(
            f"/api/v1/principals/{principal['id']}/roles/{role['id']}:revoke",
            json={"workspaceId": ws["id"]},
            headers=auth(admin_key),
        )
        assert response.status_code == 204, response.text
    got = await _by_principal(client, admin_key, ws["id"])
    assert set(got) == {only_member["id"], both["id"]}
    assert got[both["id"]]["roles"] == []


async def test_which_assignments_count(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    parent = await create_workspace(client, admin_key, "finance")
    ws = await create_workspace(client, admin_key, "payables", parent_id=parent["id"])
    child = await create_workspace(client, admin_key, "vendors", parent_id=ws["id"])
    sibling = await create_workspace(client, admin_key, "receivables", parent_id=parent["id"])
    ws_role = await create_role(client, admin_key, "payer", workspace_id=ws["id"])
    tenant_role = await create_role(client, admin_key, "auditor")
    parent_role = await create_role(client, admin_key, "controller", workspace_id=parent["id"])
    sibling_role = await create_role(client, admin_key, "collector", workspace_id=sibling["id"])

    async def principal(name: str) -> str:
        return (await create_agent_with_key(client, admin_key, name=name))[0]["id"]

    via_ancestor = await principal("via-ancestor")
    await assign_role(client, admin_key, via_ancestor, ws_role["id"], workspace_id=parent["id"])
    tenant_scoped_here = await principal("tenant-role-here")
    await assign_role(
        client, admin_key, tenant_scoped_here, tenant_role["id"], workspace_id=ws["id"]
    )
    # Not participants of ws: counts elsewhere or is not a role of ws.
    tenant_wide = await principal("tenant-wide")
    await assign_role(client, admin_key, tenant_wide, tenant_role["id"])
    in_child = await principal("in-child")
    await assign_role(client, admin_key, in_child, ws_role["id"], workspace_id=child["id"])
    parent_holder = await principal("parent-holder")
    await assign_role(
        client, admin_key, parent_holder, parent_role["id"], workspace_id=parent["id"]
    )
    sibling_holder = await principal("sibling-holder")
    await assign_role(
        client, admin_key, sibling_holder, sibling_role["id"], workspace_id=sibling["id"]
    )
    tenant_role_elsewhere = await principal("tenant-role-elsewhere")
    await assign_role(
        client, admin_key, tenant_role_elsewhere, tenant_role["id"], workspace_id=sibling["id"]
    )

    got = await _by_principal(client, admin_key, ws["id"])
    assert set(got) == {via_ancestor, tenant_scoped_here}
    assert got[via_ancestor]["roles"][0]["assignmentWorkspaceId"] == parent["id"]
    assert got[tenant_scoped_here]["roles"][0]["roleWorkspaceId"] is None
    assert await _role_holders(client, admin_key, ws_role["id"], ws["id"]) == {via_ancestor}

    # The child workspace sees its own scoped holder, not the ancestor's role holders.
    assert set(await _by_principal(client, admin_key, child["id"])) == {in_child}
    # An assignment scoped to the parent makes a participant of the parent too.
    assert set(await _by_principal(client, admin_key, parent["id"])) == {
        parent_holder,
        via_ancestor,
    }


async def test_empty_and_paged(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, admin_key, "empty")
    assert await _participants(client, admin_key, ws["id"]) == {"items": [], "nextCursor": None}

    role = await create_role(client, admin_key, "clerk", workspace_id=ws["id"])
    ids = []
    for n in range(3):
        principal, _ = await create_agent_with_key(client, admin_key, name=f"clerk-{n}")
        await assign_role(client, admin_key, principal["id"], role["id"], workspace_id=ws["id"])
        ids.append(principal["id"])

    seen: list[str] = []
    cursor = None
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        page = await _participants(client, admin_key, ws["id"], **params)
        assert len(page["items"]) <= 2
        seen += [p["principalId"] for p in page["items"]]
        assert all(p["roles"] for p in page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert sorted(seen) == sorted(ids)


async def test_participants_permissions_and_isolation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    ws = await create_workspace(client, admin_key, "private")
    url = f"/api/v1/workspaces/{ws['id']}/participants"

    _, no_workspaces = await create_agent_with_key(
        client, admin_key, name="a", permissions=["org.read"]
    )
    assert (await client.get(url, headers=auth(no_workspaces))).status_code == 403
    _, no_org = await create_agent_with_key(
        client, admin_key, name="b", permissions=["workspaces.read"]
    )
    assert (await client.get(url, headers=auth(no_org))).status_code == 403
    _, reader = await create_agent_with_key(
        client, admin_key, name="c", permissions=["workspaces.read", "principals.read"]
    )
    assert (await client.get(url, headers=auth(reader))).status_code == 200

    # Another tenant's workspace and an unknown one look the same: 404.
    _, key_b = make_tenant_directly(sync_engine, "tenant-b")
    foreign = await client.get(url, headers=auth(key_b))
    missing = await client.get(
        "/api/v1/workspaces/00000000-0000-0000-0000-000000000000/participants",
        headers=auth(key_b),
    )
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["error"]["code"] == missing.json()["error"]["code"]
    assert (
        await client.get("/api/v1/workspaces/not-a-uuid/participants", headers=auth(admin_key))
    ).status_code == 400
