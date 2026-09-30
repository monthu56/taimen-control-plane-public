"""PostgreSQL race semantics of a type migration (ADR-0048, amendment 2026-09-30)."""

import asyncio
from typing import Any

import httpx
from sqlalchemy import Engine, text

from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)

LIFECYCLE: dict[str, Any] = {
    "initialStatus": "open",
    "statuses": [
        {"key": "open", "category": "active"},
        {"key": "closed", "category": "terminal_success"},
    ],
    "transitions": [{"from": "open", "to": ["closed"]}],
}


async def _versions(client: httpx.AsyncClient, key: str, count: int) -> list[dict[str, Any]]:
    created = []
    for _ in range(count):
        response = await client.post(
            "/api/v1/task-types",
            json={"key": "flow", "displayName": "Flow", "lifecycleSchema": LIFECYCLE},
            headers=auth(key),
        )
        assert response.status_code == 201, response.text
        created.append(response.json())
    return created


def _migrations(sync_engine: Engine, task_id: str) -> int:
    with sync_engine.connect() as conn:
        return int(
            conn.execute(
                text(
                    "SELECT count(*) FROM events WHERE event_type = 'task.type_migrated' "
                    "AND entity_id = :task"
                ),
                {"task": task_id},
            ).scalar_one()
        )


async def test_claim_and_migration_serialize_on_the_task_row(
    client: httpx.AsyncClient,
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    await _versions(client, key, 1)
    _, agent_key = await create_agent_with_key(client, key)
    task = await create_task(client, key, typeKey="flow")
    await _versions(client, key, 1)
    work_session = await open_session(client, agent_key)

    claim, migration = await asyncio.gather(
        claim_task(client, agent_key, task["id"], work_session["id"]),
        client.post(
            f"/api/v1/tasks/{task['id']}:migrate-type",
            json={},
            headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
        ),
    )

    assert claim.status_code == 200, claim.text
    current = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(key))).json()
    if migration.status_code == 200:
        # The migration went first; the claim then took the task on v2.
        assert current["typeVersion"] == 2
    else:
        # The claim went first: the migration saw either the claim or the
        # version the claim wrote, and moved nothing.
        assert migration.status_code == 409, migration.text
        assert migration.json()["error"]["code"] in ("task_claimed", "version_conflict")
        assert current["typeVersion"] == 1


async def test_parallel_bulk_migrations_move_each_task_once(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    (v1,) = await _versions(client, key, 1)
    tasks = [await create_task(client, key, typeKey="flow") for _ in range(4)]
    await _versions(client, key, 1)

    responses = await asyncio.gather(
        *(
            client.post(f"/api/v1/task-types/{v1['id']}:migrate-tasks", json={}, headers=auth(key))
            for _ in range(3)
        )
    )

    assert [r.status_code for r in responses] == [200, 200, 200], [r.text for r in responses]
    moved = [m["taskId"] for r in responses for m in r.json()["migrated"]]
    assert sorted(moved) == sorted(t["id"] for t in tasks)
    for task in tasks:
        assert _migrations(sync_engine, task["id"]) == 1
