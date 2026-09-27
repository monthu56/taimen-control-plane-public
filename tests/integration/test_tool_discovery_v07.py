"""v0.7 Scoped Tool Discovery (HRS-3), end to end.

Covers the verification matrix in
``docs/verification/TASK-000005-threat-model.md``: what may be searched, what
may be described, what invalidates a cached projection, and — the part that
actually protects anything — what may be executed.
"""

from typing import Any

import httpx
import pytest

from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_skill,
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
    open_session,
    register_skill,
)

pytestmark = pytest.mark.usefixtures("clean_database")

MCP_SESSION = {
    "harness": {
        "type": "claude-code",
        "version": "0.7.0",
        "protocolVersion": "2",
        "capabilities": ["checkpoints", "skills.protocol.mcp"],
    }
}


async def _claim_and_run(
    client: httpx.AsyncClient,
    key: str,
    task_id: str,
    *,
    session_extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    session = await open_session(client, key, **(session_extra or {}))
    claim = (
        await client.post(
            f"/api/v1/tasks/{task_id}:claim",
            json={"sessionId": session["id"]},
            headers=auth(key),
        )
    ).json()
    response = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return session, response.json()


async def _search(
    client: httpx.AsyncClient, key: str, **params: Any
) -> tuple[dict[str, Any], httpx.Response]:
    response = await client.get("/api/v1/tools", params=params, headers=auth(key))
    assert response.status_code == 200, response.text
    return response.json(), response


async def _governed_project(
    client: httpx.AsyncClient, admin_key: str, governance: dict[str, Any]
) -> dict[str, Any]:
    """A workspace whose project pins governance — returns the workspace."""
    workspace = await create_workspace(client, admin_key, "delivery")
    template = (
        await client.post(
            "/api/v1/project-templates",
            json={"key": "delivery", "displayName": "Delivery"},
            headers=auth(admin_key),
        )
    ).json()
    project = (
        await client.post(
            "/api/v1/projects",
            json={"workspaceId": workspace["id"], "templateId": template["id"]},
            headers=auth(admin_key),
        )
    ).json()
    revision = (
        await client.post(
            f"/api/v1/projects/{project['id']}/config-revisions",
            json={"config": {"governance": governance}},
            headers=auth(admin_key),
        )
    ).json()
    activated = await client.post(
        f"/api/v1/projects/{project['id']}/config-revisions/{revision['revision']}:activate",
        json={},
        headers={**auth(admin_key), "If-Match": f'"project-{project["version"]}"'},
    )
    assert activated.status_code == 200, activated.text
    return workspace


# --- search -------------------------------------------------------------------


async def test_search_finds_assigned_tools_by_name_and_description(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    deploy = await register_skill(
        client,
        admin_key,
        "release.deploy",
        protocol="mcp",
        description="Ship a build to the production cluster",
    )
    await assign_skill(client, admin_key, agent["id"], deploy["id"])

    by_name, _ = await _search(client, agent_key, query="release")
    by_description, _ = await _search(client, agent_key, query="production cluster")
    assert [item["name"] for item in by_name["items"]] == ["release.deploy"]
    assert [item["id"] for item in by_description["items"]] == [deploy["id"]]
    assert by_name["items"][0]["visible"] is True
    assert by_name["items"][0]["reason"] == "assigned_and_protocol_supported"
    assert by_name["view"]["mode"] == "search"

    eager, _ = await _search(client, agent_key)
    assert eager["view"]["mode"] == "eager"
    assert [item["id"] for item in eager["items"]] == [deploy["id"]]


async def test_unassigned_tool_is_invisible_and_indistinguishable_from_missing(
    client: httpx.AsyncClient,
) -> None:
    """T1/T2: discovery must not become an enumeration oracle."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    secret_tool = await register_skill(
        client, admin_key, "payments.refund", protocol="mcp", description="Refund a charge"
    )

    page, _ = await _search(client, agent_key, query="refund")
    assert page["items"] == []

    existing = await client.get(f"/api/v1/tools/{secret_tool['id']}", headers=auth(agent_key))
    by_name = await client.get("/api/v1/tools/payments.refund", headers=auth(agent_key))
    imaginary = await client.get("/api/v1/tools/nothing.here", headers=auth(agent_key))
    assert existing.status_code == by_name.status_code == imaginary.status_code == 404
    assert existing.json()["error"]["code"] == imaginary.json()["error"]["code"]


async def test_governance_removes_a_tool_from_the_view(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    workspace = await _governed_project(client, admin_key, {"allowedSkillProtocols": ["mcp"]})
    http_tool = await register_skill(client, admin_key, "legacy.fetch", protocol="http")
    mcp_tool = await register_skill(client, admin_key, "repo.search", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], http_tool["id"])
    await assign_skill(client, admin_key, agent["id"], mcp_tool["id"])

    task = await create_task(client, admin_key, workspaceId=workspace["id"])
    _, run = await _claim_and_run(client, agent_key, task["id"], session_extra=MCP_SESSION)

    scoped, _ = await _search(client, agent_key, runId=run["id"])
    assert [item["name"] for item in scoped["items"]] == ["repo.search"]

    # Outside the run there is no project governance, so the same principal
    # legitimately sees more: policy is per Run, not per principal.
    unscoped, _ = await _search(client, agent_key)
    assert {item["name"] for item in unscoped["items"]} == {"legacy.fetch", "repo.search"}


async def test_capability_mismatch_is_reported_not_hidden(client: httpx.AsyncClient) -> None:
    """A tool the caller owns but its own harness cannot run is a config hint."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    local_tool = await register_skill(client, admin_key, "build.local", protocol="local")
    await assign_skill(client, admin_key, agent["id"], local_tool["id"])
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"], session_extra=MCP_SESSION)

    page, _ = await _search(client, agent_key, runId=run["id"])
    assert page["items"][0]["visible"] is False
    assert page["items"][0]["reason"] == "protocol_not_supported_by_harness"


async def test_pagination_is_bounded_and_terminates(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    for index in range(5):
        skill = await register_skill(client, admin_key, f"tool.{index:02d}", protocol="mcp")
        await assign_skill(client, admin_key, agent["id"], skill["id"])

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        page, _ = await _search(client, agent_key, **params)
        seen.extend(item["name"] for item in page["items"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
    assert seen == [f"tool.{index:02d}" for index in range(5)]
    assert cursor is None

    too_wide = await client.get("/api/v1/tools", params={"limit": 101}, headers=auth(agent_key))
    assert too_wide.status_code == 422
    assert too_wide.json()["error"]["code"] == "invalid_tool_query"


async def test_tenant_isolation(client: httpx.AsyncClient, sync_engine: Any) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    mine = await register_skill(client, admin_key, "mine.tool", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], mine["id"])

    _, other_admin_key = make_tenant_directly(sync_engine, "other-tenant")
    theirs = await register_skill(client, other_admin_key, "theirs.tool", protocol="mcp")

    page, _ = await _search(client, agent_key)
    assert [item["name"] for item in page["items"]] == ["mine.tool"]
    cross = await client.get(f"/api/v1/tools/{theirs['id']}", headers=auth(agent_key))
    assert cross.status_code == 404


# --- describe -----------------------------------------------------------------


async def test_describe_projects_a_sanitized_schema(client: httpx.AsyncClient) -> None:
    """T4/T5: connection material and pre-filled defaults never reach a client."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    skill = await register_skill(
        client,
        admin_key,
        "release.deploy",
        protocol="mcp",
        description="Ship a build",
        config={"endpoint": "https://internal.example/deploy", "secretRef": "vault://deploy"},
        inputSchema={
            "type": "object",
            "properties": {"environment": {"type": "string", "default": "production"}},
            "required": ["environment"],
        },
    )
    await assign_skill(client, admin_key, agent["id"], skill["id"])

    response = await client.get("/api/v1/tools/release.deploy", headers=auth(agent_key))
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["inputSchema"] == {
        "type": "object",
        "properties": {"environment": {"type": "string"}},
        "required": ["environment"],
    }
    assert detail["schemaRedactions"] == ["properties.environment.default"]
    body = response.text
    assert "internal.example" not in body
    assert "vault://" not in body
    assert "config" not in detail


# --- cache invalidation -------------------------------------------------------


async def test_catalog_change_invalidates_the_cached_projection(
    client: httpx.AsyncClient,
) -> None:
    """AC2: a 304 may only mean "both revisions are unchanged"."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    skill = await register_skill(client, admin_key, "repo.search", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], skill["id"])

    first, response = await _search(client, agent_key)
    etag = response.headers["etag"]
    assert etag.strip('"') == first["view"]["viewHash"]

    cached = await client.get("/api/v1/tools", headers={**auth(agent_key), "If-None-Match": etag})
    assert cached.status_code == 304

    patched = await client.patch(
        f"/api/v1/skills/{skill['id']}",
        json={"description": "Search the repository"},
        headers={**auth(admin_key), "If-Match": '"skill-1"'},
    )
    assert patched.status_code == 200, patched.text

    revalidated = await client.get(
        "/api/v1/tools", headers={**auth(agent_key), "If-None-Match": etag}
    )
    assert revalidated.status_code == 200
    assert revalidated.json()["view"]["catalogRevision"] != first["view"]["catalogRevision"]


async def test_assignment_change_invalidates_the_policy_revision(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    first_skill = await register_skill(client, admin_key, "a.tool", protocol="mcp")
    second_skill = await register_skill(client, admin_key, "b.tool", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], first_skill["id"])
    before, _ = await _search(client, agent_key)

    await assign_skill(client, admin_key, agent["id"], second_skill["id"])
    after, _ = await _search(client, agent_key)

    assert after["view"]["catalogRevision"] == before["view"]["catalogRevision"]
    assert after["view"]["policyRevision"] != before["view"]["policyRevision"]
    assert after["view"]["viewHash"] != before["view"]["viewHash"]


# --- invocation re-authorization ----------------------------------------------


async def _record_action(
    client: httpx.AsyncClient, key: str, run_id: str, skill_ref: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/runs/{run_id}/actions",
        json={"action": "invoke", "skill": skill_ref},
        headers=auth(key),
    )


async def test_invocation_is_authorized_again_against_the_policy(
    client: httpx.AsyncClient,
) -> None:
    """AC1/T1: the only gate that protects anything is the one at execution."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    workspace = await _governed_project(client, admin_key, {"allowedSkillProtocols": ["mcp"]})
    allowed = await register_skill(client, admin_key, "repo.search", protocol="mcp")
    forbidden = await register_skill(client, admin_key, "legacy.fetch", protocol="http")
    unassigned = await register_skill(client, admin_key, "payments.refund", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], allowed["id"])
    await assign_skill(client, admin_key, agent["id"], forbidden["id"])

    task = await create_task(client, admin_key, workspaceId=workspace["id"])
    _, run = await _claim_and_run(client, agent_key, task["id"], session_extra=MCP_SESSION)

    ok = await _record_action(client, agent_key, run["id"], "repo.search")
    assert ok.status_code == 201, ok.text
    assert ok.json()["skillId"] == allowed["id"]

    for reference in ("legacy.fetch", "payments.refund", unassigned["id"]):
        denied = await _record_action(client, agent_key, run["id"], reference)
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "tool_not_authorized"

    # A refused invocation costs nothing: the audit sequence did not advance.
    actions = (
        await client.get(f"/api/v1/runs/{run['id']}/actions", headers=auth(agent_key))
    ).json()
    assert [action["seq"] for action in actions["items"]] == [1]


async def test_capability_mismatch_is_recorded_but_never_enforced(
    client: httpx.AsyncClient,
) -> None:
    """T6: a client must not widen or narrow authorization by self-description."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    local_tool = await register_skill(client, admin_key, "build.local", protocol="local")
    await assign_skill(client, admin_key, agent["id"], local_tool["id"])
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"], session_extra=MCP_SESSION)

    response = await _record_action(client, agent_key, run["id"], "build.local")
    assert response.status_code == 201, response.text
    assert response.json()["metadata"]["capabilityMismatch"] == "local"


async def test_denial_is_observable_without_inventing_a_domain_event(
    client: httpx.AsyncClient,
) -> None:
    """T9: a refusal commits nothing, so it is a metric — not a journal entry."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key, permissions=ORG_AGENT_PERMISSIONS)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"], session_extra=MCP_SESSION)

    denied = await _record_action(client, agent_key, run["id"], "payments.refund")
    assert denied.status_code == 403

    metrics = (await client.get("/metrics")).text
    assert "tool_invocation_denied_total" in metrics

    events = (
        await client.get("/api/v1/events", params={"limit": 100}, headers=auth(admin_key))
    ).json()
    # The rolled-back attempt left no fact behind, and the tool name — which a
    # probe controls — never enters the durable journal.
    assert all("payments.refund" not in str(event["payload"]) for event in events["items"])
