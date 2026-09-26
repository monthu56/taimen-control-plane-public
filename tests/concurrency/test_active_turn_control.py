"""PostgreSQL race semantics for Durable Active Turn Control."""

import asyncio
from typing import Any

import httpx

from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap, open_session


async def _claim_and_run(
    client: httpx.AsyncClient, agent_key: str, task_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task_id}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    run_response = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(agent_key),
    )
    assert run_response.status_code == 201, run_response.text
    return claim, run_response.json()


async def test_competing_control_messages_serialize_on_run_version(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])

    async def send(index: int) -> httpx.Response:
        return await client.post(
            f"/api/v1/runs/{run['id']}/control-messages",
            json={
                "operation": "steer",
                "causalPosition": f"race:{index}",
                "directive": f"correction-{index}",
                "expectedRunVersion": run["version"],
            },
            headers={**auth(admin_key), "Idempotency-Key": f"race-steer-{index}"},
        )

    responses = await asyncio.gather(send(1), send(2))
    assert sorted(response.status_code for response in responses) == [201, 409]
    conflict = next(response for response in responses if response.status_code == 409)
    assert conflict.json()["error"]["code"] == "run_version_conflict"

    listed = await client.get(f"/api/v1/runs/{run['id']}/control-messages", headers=auth(agent_key))
    assert len(listed.json()["items"]) == 1


async def test_force_cancel_serializes_with_action_append(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])

    async def force_cancel() -> httpx.Response:
        return await client.post(
            f"/api/v1/runs/{run['id']}/control-messages",
            json={
                "operation": "force_cancel",
                "causalPosition": "race:force",
                "reason": "concurrent operator stop",
                "expectedRunVersion": run["version"],
            },
            headers={**auth(admin_key), "Idempotency-Key": "race-force"},
        )

    async def append_action() -> httpx.Response:
        return await client.post(
            f"/api/v1/runs/{run['id']}/actions",
            json={"action": "tool.concurrent"},
            headers=auth(agent_key),
        )

    force_response, action_response = await asyncio.gather(force_cancel(), append_action())
    assert force_response.status_code == 201, force_response.text
    assert action_response.status_code in (201, 409), action_response.text
    if action_response.status_code == 409:
        assert action_response.json()["error"]["code"] == "run_not_active"

    run_after = await client.get(f"/api/v1/runs/{run['id']}", headers=auth(agent_key))
    assert run_after.json()["status"] == "cancelled"
    actions = await client.get(f"/api/v1/runs/{run['id']}/actions", headers=auth(agent_key))
    assert len(actions.json()["items"]) in (0, 1)


async def test_overlapping_force_trees_are_serialized(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task_a = await create_task(client, admin_key, title="tree-a")
    task_b = await create_task(client, admin_key, title="tree-b")
    # spawned_by is intentionally a general graph, so exercise a cycle.
    for child, parent in ((task_a, task_b), (task_b, task_a)):
        response = await client.post(
            f"/api/v1/tasks/{child['id']}/relations",
            json={"toTask": parent["id"], "type": "spawned_by"},
            headers=auth(admin_key),
        )
        assert response.status_code == 201, response.text
    _, run_a = await _claim_and_run(client, agent_key, task_a["id"])
    _, run_b = await _claim_and_run(client, agent_key, task_b["id"])

    async def force(run: dict[str, Any], key: str) -> httpx.Response:
        return await client.post(
            f"/api/v1/runs/{run['id']}/control-messages",
            json={
                "operation": "force_cancel",
                "causalPosition": f"race:{key}",
                "reason": "overlapping tree cancellation",
                "expectedRunVersion": run["version"],
            },
            headers={**auth(admin_key), "Idempotency-Key": key},
        )

    responses = await asyncio.gather(force(run_a, "force-a"), force(run_b, "force-b"))
    assert sorted(response.status_code for response in responses) == [201, 409]
    loser = next(response for response in responses if response.status_code == 409)
    assert loser.json()["error"]["code"] == "run_not_active"
    for run in (run_a, run_b):
        current = await client.get(f"/api/v1/runs/{run['id']}", headers=auth(agent_key))
        assert current.json()["status"] == "cancelled"
