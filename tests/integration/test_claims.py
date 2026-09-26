import httpx
from sqlalchemy.engine import Engine

from tests.helpers import (
    auth,
    backdate_expiry,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)


async def _setup_agent_session(
    client: httpx.AsyncClient, admin_key: str, name: str = "agent"
) -> tuple[dict, str, dict]:
    principal, key = await create_agent_with_key(client, admin_key, name=name)
    session = await open_session(client, key, client_name=name)
    return principal, key, session


async def test_claim_happy_path(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal, agent_key, session = await _setup_agent_session(client, admin_key)
    task = await create_task(client, admin_key)

    response = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session["id"], "intent": "work on it"},
        headers=auth(agent_key),
    )
    assert response.status_code == 200, response.text
    claim = response.json()
    assert claim["status"] == "active"
    assert claim["fencingToken"] == 1
    assert claim["holderId"] == principal["id"]

    task_now = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))).json()
    assert task_now["status"] == "in_progress"
    assert task_now["activeClaimId"] == claim["id"]
    assert task_now["claimEpoch"] == 1
    assert task_now["version"] == 2


async def test_second_claim_conflicts(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, key_a, session_a = await _setup_agent_session(client, admin_key, "agent-a")
    _, key_b, session_b = await _setup_agent_session(client, admin_key, "agent-b")
    task = await create_task(client, admin_key)

    first = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session_a["id"]},
        headers=auth(key_a),
    )
    assert first.status_code == 200

    second = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session_b["id"]},
        headers=auth(key_b),
    )
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "task_already_claimed"
    assert error["details"]["claimId"] == first.json()["id"]
    assert "expiresAt" in error["details"]


async def test_claim_requires_own_live_session(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, key_a, session_a = await _setup_agent_session(client, admin_key, "agent-a")
    _, key_b, _ = await _setup_agent_session(client, admin_key, "agent-b")
    task = await create_task(client, admin_key)

    # B cannot claim through A's session.
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session_a["id"]},
        headers=auth(key_b),
    )
    assert response.status_code == 403

    # A closed session cannot claim.
    await client.post(f"/api/v1/sessions/{session_a['id']}:close", headers=auth(key_a))
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session_a["id"]},
        headers=auth(key_a),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "session_not_active"


async def test_claim_heartbeat_and_release(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup_agent_session(client, admin_key)
    task = await create_task(client, admin_key)

    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()

    response = await client.post(f"/api/v1/claims/{claim['id']}:heartbeat", headers=auth(agent_key))
    assert response.status_code == 200
    assert response.json()["expiresAt"] >= claim["expiresAt"]

    response = await client.post(
        f"/api/v1/claims/{claim['id']}:release",
        json={"reason": "shutting down"},
        headers=auth(agent_key),
    )
    assert response.status_code == 200
    released = response.json()
    assert released["status"] == "released"
    assert released["releaseReason"] == "shutting down"

    # The task went back to todo and lost its claim pointer.
    task_now = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))).json()
    assert task_now["status"] == "todo"
    assert task_now["activeClaimId"] is None

    # Release is idempotent; heartbeat of a released claim conflicts.
    assert (
        await client.post(f"/api/v1/claims/{claim['id']}:release", headers=auth(agent_key))
    ).status_code == 200
    response = await client.post(f"/api/v1/claims/{claim['id']}:heartbeat", headers=auth(agent_key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "claim_not_active"

    # Claim history is preserved.
    response = await client.get(f"/api/v1/claims?taskId={task['id']}", headers=auth(agent_key))
    assert len(response.json()["items"]) == 1


async def test_expired_claim_can_be_reclaimed_by_new_session(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, key_a, session_a = await _setup_agent_session(client, admin_key, "agent-a")
    _, key_b, session_b = await _setup_agent_session(client, admin_key, "agent-b")
    task = await create_task(client, admin_key)

    claim_a = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session_a["id"]},
            headers=auth(key_a),
        )
    ).json()

    # While the claim is live, reclaim is rejected.
    response = await client.post(
        f"/api/v1/claims/{claim_a['id']}:reclaim",
        json={"sessionId": session_b["id"]},
        headers=auth(key_b),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "claim_not_expired"

    backdate_expiry(sync_engine, "task_claims", claim_a["id"])

    response = await client.post(
        f"/api/v1/claims/{claim_a['id']}:reclaim",
        json={"sessionId": session_b["id"]},
        headers=auth(key_b),
    )
    assert response.status_code == 200, response.text
    claim_b = response.json()
    assert claim_b["fencingToken"] == 2
    assert claim_b["sessionId"] == session_b["id"]

    # The old claim is stale, history intact.
    old = (await client.get(f"/api/v1/claims/{claim_a['id']}", headers=auth(key_a))).json()
    assert old["status"] == "stale"
    assert old["releaseReason"] == "expired"


async def test_direct_claim_reaps_expired_active_claim(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The claim command itself requisitions an expired claim — no worker needed."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, key_a, session_a = await _setup_agent_session(client, admin_key, "agent-a")
    _, key_b, session_b = await _setup_agent_session(client, admin_key, "agent-b")
    task = await create_task(client, admin_key)

    claim_a = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session_a["id"]},
            headers=auth(key_a),
        )
    ).json()
    backdate_expiry(sync_engine, "task_claims", claim_a["id"])

    response = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session_b["id"]},
        headers=auth(key_b),
    )
    assert response.status_code == 200, response.text
    assert response.json()["fencingToken"] == 2


async def test_claim_gate_on_mutations(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup_agent_session(client, admin_key)
    task = await create_task(client, admin_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()

    # Mutation without claim credentials is rejected while the claim is live.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "sneaky"},
        headers={**auth(admin_key), "If-Match": '"task-2"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "task_claimed"

    # Wrong fencing token is rejected.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "sneaky", "claimId": claim["id"], "fencingToken": 999},
        headers={**auth(agent_key), "If-Match": '"task-2"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_claim"

    # The holder with correct credentials can update and complete.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={
            "description": "progress",
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
        },
        headers={**auth(agent_key), "If-Match": '"task-2"'},
    )
    assert response.status_code == 200, response.text

    response = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers={**auth(agent_key), "If-Match": '"task-3"'},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"

    # The claim was released as part of completion.
    released = (await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(agent_key))).json()
    assert released["status"] == "released"
    assert released["releaseReason"] == "completed"


async def test_completed_task_cannot_be_claimed(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key, session = await _setup_agent_session(client, admin_key)
    task = await create_task(client, admin_key)
    await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )

    response = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session["id"]},
        headers=auth(agent_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "task_not_claimable"


async def test_dead_holder_session_unblocks_the_task(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A live claim whose session died no longer gates mutations or claims."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, key_a, session_a = await _setup_agent_session(client, admin_key, "agent-a")
    _, key_b, session_b = await _setup_agent_session(client, admin_key, "agent-b")
    task = await create_task(client, admin_key)
    claim_a = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session_a["id"]},
            headers=auth(key_a),
        )
    ).json()

    # The holder's session dies (expired, not yet swept by the worker).
    backdate_expiry(sync_engine, "sessions", session_a["id"])

    # The stranded holder can no longer write under its claim.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={
            "title": "zombie write",
            "claimId": claim_a["id"],
            "fencingToken": claim_a["fencingToken"],
        },
        headers={**auth(key_a), "If-Match": '"task-2"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_claim"

    # An admin can now mutate without claim credentials.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"priority": "low"},
        headers={**auth(admin_key), "If-Match": '"task-2"'},
    )
    assert response.status_code == 200, response.text

    # And another agent can claim the task directly (dead claim is reaped).
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session_b["id"]},
        headers=auth(key_b),
    )
    assert response.status_code == 200, response.text
    assert response.json()["fencingToken"] == 2

    old = (await client.get(f"/api/v1/claims/{claim_a['id']}", headers=auth(admin_key))).json()
    assert old["status"] == "stale"
    assert old["releaseReason"] == "session_inactive"
