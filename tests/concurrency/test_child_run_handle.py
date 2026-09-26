"""PostgreSQL race semantics for the Durable Child Run Handle."""

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


async def test_parallel_launches_with_one_correlation_id_create_one_child(
    client: httpx.AsyncClient,
) -> None:
    """The load-bearing claim of the design, under real contention.

    Every request carries a *different* Idempotency-Key, so the HTTP layer
    cannot be what deduplicates them: only the unique
    ``(parent_run_id, correlation_id)`` constraint can.
    """
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])

    async def send(index: int) -> httpx.Response:
        return await client.post(
            f"/api/v1/runs/{run['id']}/child-handles",
            json={"correlationId": "review:migration", "title": "Verify roundtrip"},
            headers={**auth(agent_key), "Idempotency-Key": f"attempt-{index}"},
        )

    responses = await asyncio.gather(*(send(index) for index in range(6)))

    assert all(response.status_code in (200, 201) for response in responses), [
        (response.status_code, response.text) for response in responses
    ]
    assert sum(response.status_code == 201 for response in responses) == 1
    handle_ids = {response.json()["childHandle"]["id"] for response in responses}
    child_task_ids = {response.json()["childHandle"]["childTaskId"] for response in responses}
    assert len(handle_ids) == 1
    assert len(child_task_ids) == 1
    # Exactly one call minted the one-time token.
    assert sum(response.json()["handleToken"] is not None for response in responses) == 1

    listing = (
        await client.get(f"/api/v1/runs/{run['id']}/child-handles", headers=auth(agent_key))
    ).json()
    assert len(listing["items"]) == 1


async def test_launch_loses_cleanly_against_a_concurrent_force_cancel(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, run = await _claim_and_run(client, agent_key, task["id"])

    async def launch() -> httpx.Response:
        return await client.post(
            f"/api/v1/runs/{run['id']}/child-handles",
            json={"correlationId": "late-child", "title": "Too late"},
            headers={**auth(agent_key), "Idempotency-Key": "late-child"},
        )

    async def force_cancel() -> httpx.Response:
        return await client.post(
            f"/api/v1/runs/{run['id']}/control-messages",
            json={
                "operation": "force_cancel",
                "causalPosition": "turn:1",
                "reason": "governance stop",
                "expectedRunVersion": run["version"],
            },
            headers={**auth(admin_key), "Idempotency-Key": "force-stop"},
        )

    launched, cancelled = await asyncio.gather(launch(), force_cancel())

    assert cancelled.status_code == 201, cancelled.text
    # Task -> claim -> run locks give one serial order: the launch either got
    # in before the terminal transition or is refused, never half-applied.
    assert launched.status_code in (201, 409), launched.text
    if launched.status_code == 409:
        assert launched.json()["error"]["code"] == "run_not_active"

    listing = (
        await client.get(f"/api/v1/runs/{run['id']}/child-handles", headers=auth(admin_key))
    ).json()
    assert len(listing["items"]) == (1 if launched.status_code == 201 else 0)
