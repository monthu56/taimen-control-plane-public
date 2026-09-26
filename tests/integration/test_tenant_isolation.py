"""Tenant A's credential must not read or mutate tenant B's data, even by UUID."""

import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)


async def test_cross_tenant_reads_and_writes_are_404(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    key_a = body["apiKey"]["key"]
    principal_a = body["adminPrincipal"]["id"]
    task_a = await create_task(client, key_a, title="Tenant A task")

    _, key_b = make_tenant_directly(sync_engine, "tenant-b")

    # Reads by UUID land in 404, not 403 (existence is not leaked).
    for url in (
        f"/api/v1/tasks/{task_a['id']}",
        f"/api/v1/principals/{principal_a}",
    ):
        response = await client.get(url, headers=auth(key_b))
        assert response.status_code == 404, url

    # Lists are empty.
    assert (await client.get("/api/v1/tasks", headers=auth(key_b))).json()["items"] == []
    assert (await client.get("/api/v1/events", headers=auth(key_b))).json()["items"] == []

    # Mutations by UUID fail as not-found.
    response = await client.patch(
        f"/api/v1/tasks/{task_a['id']}",
        json={"title": "Hijacked"},
        headers={**auth(key_b), "If-Match": '"task-1"'},
    )
    assert response.status_code == 404
    response = await client.post(
        f"/api/v1/tasks/{task_a['id']}:complete",
        headers={**auth(key_b), "If-Match": '"task-1"'},
    )
    assert response.status_code == 404

    # And tenant A's data is untouched.
    current = await client.get(f"/api/v1/tasks/{task_a['id']}", headers=auth(key_a))
    assert current.json()["title"] == "Tenant A task"


async def test_cross_tenant_claim_and_session_isolation(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    key_a = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, key_a)
    session_a = await open_session(client, agent_key)
    task_a = await create_task(client, key_a)
    claim_a = (
        await client.post(
            f"/api/v1/tasks/{task_a['id']}:claim",
            json={"sessionId": session_a["id"]},
            headers=auth(agent_key),
        )
    ).json()

    _, key_b = make_tenant_directly(sync_engine, "tenant-b")

    # Tenant B cannot see or manipulate tenant A's session/claims.
    assert (
        await client.get(f"/api/v1/sessions/{session_a['id']}", headers=auth(key_b))
    ).status_code == 404
    assert (
        await client.get(f"/api/v1/claims/{claim_a['id']}", headers=auth(key_b))
    ).status_code == 404
    assert (
        await client.post(f"/api/v1/claims/{claim_a['id']}:release", headers=auth(key_b))
    ).status_code == 404
    assert (
        await client.post(
            f"/api/v1/tasks/{task_a['id']}:claim",
            json={"sessionId": session_a["id"]},
            headers=auth(key_b),
        )
    ).status_code == 404

    # Tenant B's own world works independently.
    task_b = await create_task(client, key_b, title="Tenant B task")
    assert task_b["publicId"] == "TASK-000001"  # counters are per tenant
