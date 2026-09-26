"""Explicit context observations: replayable, attributed, tenant-isolated."""

import uuid

import httpx

from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)


async def test_remember_records_replayable_event(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key, title="Investigate race")
    session = await open_session(client, agent_key)

    response = await client.post(
        "/api/v1/observations",
        json={
            "kind": "finding",
            "content": "The race is caused by xid/sequence inversion",
            "data": {"confidence": "high"},
            "task": task["id"],
            "sessionId": session["id"],
        },
        headers=auth(agent_key),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "finding"

    # The observation IS a journal event: replayable by any follower.
    events = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(agent_key))
    ).json()["items"]
    recorded = [e for e in events if e["type"] == "observation.recorded"]
    assert len(recorded) == 1
    event = recorded[0]
    assert event["entityId"] == body["id"]
    assert event["payload"]["content"] == "The race is caused by xid/sequence inversion"
    assert event["payload"]["taskId"] == task["id"]
    # Attribution comes from authentication, not from the request body.
    assert event["actorId"] == agent["id"]
    assert event["sessionId"] == session["id"]


async def test_remember_requires_permission(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    _, limited_key = await create_agent_with_key(
        client, boot["apiKey"]["key"], permissions=["tasks.read"]
    )
    response = await client.post(
        "/api/v1/observations",
        json={"kind": "note", "content": "x"},
        headers=auth(limited_key),
    )
    assert response.status_code == 403


async def test_remember_cannot_attach_to_foreign_tenant_task(
    client: httpx.AsyncClient, sync_engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    # A task id from ANOTHER tenant: unresolvable inside the caller's tenant.
    _other_tenant, other_key = make_tenant_directly(sync_engine, "other")
    other_task = await create_task(client, other_key, title="Foreign")

    response = await client.post(
        "/api/v1/observations",
        json={"kind": "note", "content": "spy", "task": other_task["id"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 404


async def test_remember_cannot_use_foreign_session(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, a_key = await create_agent_with_key(client, admin_key, name="a")
    _, b_key = await create_agent_with_key(client, admin_key, name="b")
    session_a = await open_session(client, a_key)

    # B cannot claim provenance under A's session.
    response = await client.post(
        "/api/v1/observations",
        json={"kind": "note", "content": "x", "sessionId": session_a["id"]},
        headers=auth(b_key),
    )
    assert response.status_code == 404


async def test_remember_validates_kind_and_content(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])

    bad_kind = await client.post(
        "/api/v1/observations",
        json={"kind": "Not Valid!", "content": "x"},
        headers=auth(agent_key),
    )
    assert bad_kind.status_code == 422
    assert bad_kind.json()["error"]["code"] == "observation_invalid"

    empty = await client.post(
        "/api/v1/observations",
        json={"kind": "note", "content": "   "},
        headers=auth(agent_key),
    )
    assert empty.status_code == 422


async def test_remember_is_idempotent(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    key = uuid.uuid4().hex

    first = await client.post(
        "/api/v1/observations",
        json={"kind": "decision", "content": "Use (tx_id, sequence)"},
        headers={**auth(agent_key), "Idempotency-Key": key},
    )
    replay = await client.post(
        "/api/v1/observations",
        json={"kind": "decision", "content": "Use (tx_id, sequence)"},
        headers={**auth(agent_key), "Idempotency-Key": key},
    )
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]

    events = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(agent_key))
    ).json()["items"]
    assert len([e for e in events if e["type"] == "observation.recorded"]) == 1
