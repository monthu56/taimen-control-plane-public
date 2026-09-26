"""Race semantics of scoped tool discovery (HRS-3).

The point of re-authorizing at execution time is the window between "the model
saw the schema" and "the action happened". These tests live in that window.
"""

import asyncio
from typing import Any

import httpx

from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_skill,
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
    register_skill,
)


async def _claim_and_run(client: httpx.AsyncClient, agent_key: str, task_id: str) -> dict[str, Any]:
    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task_id}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    response = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_revocation_between_describe_and_invoke_is_refused(
    client: httpx.AsyncClient,
) -> None:
    """T7: a projection the client already holds grants nothing."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    skill = await register_skill(client, admin_key, "repo.search", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], skill["id"])
    task = await create_task(client, admin_key)
    run = await _claim_and_run(client, agent_key, task["id"])

    described = await client.get("/api/v1/tools/repo.search", headers=auth(agent_key))
    assert described.status_code == 200

    revoked = await client.post(
        f"/api/v1/principals/{agent['id']}/skills/{skill['id']}:revoke",
        headers=auth(admin_key),
    )
    assert revoked.status_code == 204

    denied = await client.post(
        f"/api/v1/runs/{run['id']}/actions",
        json={"action": "invoke", "skill": "repo.search"},
        headers=auth(agent_key),
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "tool_not_authorized"

    # And it disappears from discovery in the same breath.
    page = (await client.get("/api/v1/tools", headers=auth(agent_key))).json()
    assert page["items"] == []


async def test_concurrent_invocations_all_see_one_consistent_policy(
    client: httpx.AsyncClient,
) -> None:
    """Whatever the interleaving, no action is recorded for a revoked tool."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=ORG_AGENT_PERMISSIONS
    )
    skill = await register_skill(client, admin_key, "repo.search", protocol="mcp")
    await assign_skill(client, admin_key, agent["id"], skill["id"])
    task = await create_task(client, admin_key)
    run = await _claim_and_run(client, agent_key, task["id"])

    async def invoke(index: int) -> httpx.Response:
        return await client.post(
            f"/api/v1/runs/{run['id']}/actions",
            json={"action": f"invoke-{index}", "skill": "repo.search"},
            headers=auth(agent_key),
        )

    async def revoke() -> httpx.Response:
        return await client.post(
            f"/api/v1/principals/{agent['id']}/skills/{skill['id']}:revoke",
            headers=auth(admin_key),
        )

    results = await asyncio.gather(invoke(1), revoke(), invoke(2), return_exceptions=True)
    statuses = [r.status_code for r in results if isinstance(r, httpx.Response)]
    assert all(status in (201, 204, 403) for status in statuses), statuses

    # After the revocation is committed the answer is stable in one direction.
    after = await invoke(3)
    assert after.status_code == 403

    recorded = (
        await client.get(f"/api/v1/runs/{run['id']}/actions", headers=auth(agent_key))
    ).json()
    assert all(action["skillId"] == skill["id"] for action in recorded["items"])
    assert [action["seq"] for action in recorded["items"]] == list(
        range(1, len(recorded["items"]) + 1)
    )
