"""v0.8 races: one winning transition, one winning claim, one version line.

Moving a work item through its lifecycle is a read-modify-write on the task
row, so it has exactly the failure mode ``If-Match`` exists for. These tests
assert the OUTCOME distribution — the row lock and the version check are the
implementation.
"""

import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)


async def test_concurrent_transitions_produce_one_winner(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    task = await create_task(client, admin_key, title="Contended")
    headers = {**auth(admin_key), "If-Match": f'"task-{task["version"]}"'}

    async def move(target: str) -> httpx.Response:
        return await client.patch(
            f"/api/v1/tasks/{task['id']}", json={"status": target}, headers=headers
        )

    responses = await asyncio.gather(
        *[move(target) for target in ("blocked", "backlog", "cancelled", "blocked", "backlog")]
    )

    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == 4, statuses
    winner = next(r for r in responses if r.status_code == 200).json()
    with sync_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT status, system_status_category, version FROM tasks WHERE id = :id"),
            {"id": task["id"]},
        ).one()
    assert stored.status == winner["status"]
    assert stored.system_status_category == winner["systemStatusCategory"]
    assert stored.version == task["version"] + 1


async def test_refused_transitions_never_move_the_version(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A storm of undeclared transitions must be inert, not merely unsuccessful."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    created = await client.post(
        "/api/v1/task-types",
        json={
            "key": "linear",
            "displayName": "Linear",
            "lifecycleSchema": {
                "initialStatus": "open",
                "statuses": [
                    {"key": "open", "category": "active"},
                    {"key": "closed", "category": "terminal_success"},
                ],
                "transitions": [{"from": "open", "to": ["closed"]}],
            },
        },
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    task = await create_task(client, admin_key, title="Linear", typeKey="linear")

    async def move() -> httpx.Response:
        return await client.patch(
            f"/api/v1/tasks/{task['id']}",
            json={"status": "open"},  # self-transition: never declared
            headers={**auth(admin_key), "If-Match": f'"task-{task["version"]}"'},
        )

    responses = await asyncio.gather(*[move() for _ in range(8)])

    assert {r.status_code for r in responses} == {422}
    with sync_engine.connect() as conn:
        version = conn.execute(
            text("SELECT version FROM tasks WHERE id = :id"), {"id": task["id"]}
        ).scalar_one()
    assert version == task["version"]


async def test_claim_races_completion(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    """Never both: a task cannot end up claimed AND in a terminal category."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    task = await create_task(client, admin_key, title="Contended")

    claim, completion = await asyncio.gather(
        claim_task(client, agent_key, task["id"], session["id"]),
        client.post(
            f"/api/v1/tasks/{task['id']}:complete",
            json={},
            headers={**auth(admin_key), "If-Match": f'"task-{task["version"]}"'},
        ),
    )

    assert {claim.status_code, completion.status_code} <= {200, 409, 422}
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT system_status_category, active_claim_id FROM tasks WHERE id = :id"),
            {"id": task["id"]},
        ).one()
    if row.system_status_category == "terminal_success":
        assert row.active_claim_id is None
    else:
        assert claim.status_code == 200
