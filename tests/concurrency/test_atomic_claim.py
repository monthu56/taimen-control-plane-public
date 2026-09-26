"""Atomic checkout: 20 concurrent claim attempts, exactly one winner."""

import asyncio

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)


async def test_twenty_concurrent_claims_one_winner(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    sessions = await asyncio.gather(
        *[open_session(client, agent_key, client_name=f"worker-{i}") for i in range(20)]
    )
    task = await create_task(client, admin_key)

    async def attempt(session_id: str) -> httpx.Response:
        return await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session_id},
            headers=auth(agent_key),
        )

    responses = await asyncio.gather(*[attempt(s["id"]) for s in sessions])
    statuses = sorted(r.status_code for r in responses)

    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == 19, statuses
    for response in responses:
        if response.status_code == 409:
            assert response.json()["error"]["code"] == "task_already_claimed"

    with sync_engine.connect() as conn:
        active_claims = conn.execute(
            text("SELECT count(*) FROM task_claims WHERE status = 'active'")
        ).scalar()
        total_claims = conn.execute(text("SELECT count(*) FROM task_claims")).scalar()
        claimed_events = conn.execute(
            text("SELECT count(*) FROM events WHERE event_type = 'task.claimed'")
        ).scalar()
        epoch = conn.execute(text("SELECT claim_epoch FROM tasks")).scalar()
    assert active_claims == 1
    assert total_claims == 1  # losers never insert a claim row
    assert claimed_events == 1
    assert epoch == 1
