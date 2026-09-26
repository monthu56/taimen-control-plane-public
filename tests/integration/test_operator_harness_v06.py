"""v0.6 Human Operator Harness server contracts on PostgreSQL."""

from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    backdate_expiry,
    create_task,
    do_bootstrap,
    make_tenant_directly,
    open_session,
)


async def _claim_and_run(
    client: httpx.AsyncClient, key: str, task_id: str, session_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    claim_response = await client.post(
        f"/api/v1/tasks/{task_id}:claim",
        json={"sessionId": session_id},
        headers=auth(key),
    )
    assert claim_response.status_code == 200, claim_response.text
    claim = claim_response.json()
    run_response = await client.post(
        f"/api/v1/tasks/{task_id}:start-run",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers=auth(key),
    )
    assert run_response.status_code == 201, run_response.text
    return claim, run_response.json()


def _handoff_body(summary: str = "Continue in the other harness") -> dict[str, Any]:
    return {
        "reason": "human_harness_handoff",
        "checkpoint": {
            "kind": "handoff",
            "data": {
                "summary": summary,
                "nextSteps": ["Read Run Context", "Continue the verified change"],
                "evidenceRefs": ["git:abc123"],
            },
        },
    }


async def test_control_level_is_server_derived_and_audited(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]

    human_session = await open_session(
        client,
        admin_key,
        client_name="codex-operator",
        harness={"type": "codex", "protocolVersion": "2"},
    )
    assert human_session["controlLevel"] == "human_operated"

    agent_response = await client.post(
        "/api/v1/principals",
        json={"kind": "agent", "displayName": "Untrusted adapter"},
        headers=auth(admin_key),
    )
    agent = agent_response.json()
    key_response = await client.post(
        f"/api/v1/principals/{agent['id']}/api-keys",
        json={"permissions": ["sessions.open"]},
        headers=auth(admin_key),
    )
    agent_key = key_response.json()["key"]
    agent_session = await open_session(
        client,
        agent_key,
        client_name="spoof",
        harness={"type": "human-operated", "protocolVersion": "2"},
    )
    assert agent_session["controlLevel"] == "connected"

    spoof = await client.post(
        "/api/v1/sessions",
        json={"clientName": "spoof", "controlLevel": "human_operated"},
        headers=auth(agent_key),
    )
    assert spoof.status_code == 400

    context = (
        await client.get(
            f"/api/v1/harness/context?sessionId={human_session['id']}",
            headers=auth(admin_key),
        )
    ).json()
    assert context["session"]["controlLevel"] == "human_operated"
    events = (
        await client.get(
            "/api/v1/events",
            params={"entityType": "session", "entityId": human_session["id"]},
            headers=auth(admin_key),
        )
    ).json()["items"]
    assert events[0]["payload"]["controlLevel"] == "human_operated"


async def test_handoff_is_atomic_idempotent_and_resumable(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    task = await create_task(client, key, title="Move work between harnesses")
    codex = await open_session(
        client,
        key,
        client_name="codex-operator",
        harness={"type": "codex", "protocolVersion": "2"},
    )
    claim1, run1 = await _claim_and_run(client, key, task["id"], codex["id"])

    headers = {**auth(key), "Idempotency-Key": "handoff-one"}
    first = await client.post(
        f"/api/v1/runs/{run1['id']}:handoff",
        json=_handoff_body(),
        headers=headers,
    )
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["run"]["status"] == "suspended"
    assert result["task"]["status"] == "todo"
    assert result["checkpoint"]["kind"] == "handoff"
    assert result["eventCursor"].startswith("ec1_")
    assert result["resume"]["nextAction"] == "claim_and_start_new_run"

    replay = await client.post(
        f"/api/v1/runs/{run1['id']}:handoff",
        json=_handoff_body(),
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["checkpoint"]["id"] == result["checkpoint"]["id"]
    with sync_engine.connect() as connection:
        checkpoint_count = connection.execute(
            text("SELECT count(*) FROM run_checkpoints WHERE run_id = :run"),
            {"run": run1["id"]},
        ).scalar_one()
    assert checkpoint_count == 1

    claude = await open_session(
        client,
        key,
        client_name="claude-code-operator",
        harness={"type": "claude-code", "protocolVersion": "2"},
    )
    claim2, run2 = await _claim_and_run(client, key, task["id"], claude["id"])
    assert claim1["fencingToken"] == 1
    assert claim2["fencingToken"] == 2
    assert run2["id"] != run1["id"]
    context = (await client.get(f"/api/v1/runs/{run2['id']}/context", headers=auth(key))).json()
    assert context["checkpoints"][0]["id"] == result["checkpoint"]["id"]
    assert context["checkpoints"][0]["runId"] == run1["id"]


async def test_handoff_rejects_stale_claim_and_unsafe_payload(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    task = await create_task(client, key)
    session = await open_session(client, key, harness={"type": "codex", "protocolVersion": "2"})
    claim, run = await _claim_and_run(client, key, task["id"], session["id"])

    unsafe = await client.post(
        f"/api/v1/runs/{run['id']}:handoff",
        json=_handoff_body("Continue from /Users/operator/private/repo"),
        headers=auth(key),
    )
    assert unsafe.status_code == 422
    assert unsafe.json()["error"]["code"] == "unsafe_handoff_payload"

    backdate_expiry(sync_engine, "task_claims", claim["id"])
    stale = await client.post(
        f"/api/v1/runs/{run['id']}:handoff",
        json=_handoff_body(),
        headers=auth(key),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_claim"
    with sync_engine.connect() as connection:
        checkpoint_count = connection.execute(
            text("SELECT count(*) FROM run_checkpoints WHERE run_id = :run"),
            {"run": run["id"]},
        ).scalar_one()
    assert checkpoint_count == 0


async def test_handoff_rejects_taken_over_claim(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    task = await create_task(client, key)
    first_session = await open_session(
        client, key, harness={"type": "codex", "protocolVersion": "2"}
    )
    claim1, run1 = await _claim_and_run(client, key, task["id"], first_session["id"])

    backdate_expiry(sync_engine, "task_claims", claim1["id"])
    second_session = await open_session(
        client, key, harness={"type": "claude-code", "protocolVersion": "2"}
    )
    claim2 = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": second_session["id"]},
            headers=auth(key),
        )
    ).json()
    assert claim2["fencingToken"] == 2

    stale = await client.post(
        f"/api/v1/runs/{run1['id']}:handoff",
        json=_handoff_body(),
        headers=auth(key),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_claim"
    with sync_engine.connect() as connection:
        checkpoint_count = connection.execute(
            text("SELECT count(*) FROM run_checkpoints WHERE run_id = :run"),
            {"run": run1["id"]},
        ).scalar_one()
    assert checkpoint_count == 0


async def test_handoff_does_not_leak_cross_tenant_run(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    task = await create_task(client, key)
    session = await open_session(client, key)
    _, run = await _claim_and_run(client, key, task["id"], session["id"])
    _, other_key = make_tenant_directly(sync_engine, "other-handoff")

    response = await client.post(
        f"/api/v1/runs/{run['id']}:handoff",
        json=_handoff_body(),
        headers=auth(other_key),
    )
    assert response.status_code == 404
    assert (await client.get(f"/api/v1/runs/{run['id']}", headers=auth(key))).json()[
        "status"
    ] == "running"


async def test_parent_task_creation_rolls_back_on_relation_failure(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    parent = await create_task(client, key, title="Root")
    child_response = await client.post(
        "/api/v1/tasks",
        json={"title": "Child", "parentTask": parent["id"]},
        headers=auth(key),
    )
    assert child_response.status_code == 201
    relations = (
        await client.get(
            f"/api/v1/tasks/{child_response.json()['id']}/relations", headers=auth(key)
        )
    ).json()["items"]
    assert relations[0]["type"] == "parent"

    failed = await client.post(
        "/api/v1/tasks",
        json={"title": "Must roll back", "parentTask": "TASK-999999"},
        headers=auth(key),
    )
    assert failed.status_code == 404
    with sync_engine.connect() as connection:
        titles = connection.execute(text("SELECT title FROM tasks ORDER BY title")).scalars().all()
    assert "Must roll back" not in titles
