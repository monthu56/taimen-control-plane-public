"""End-to-end main scenario: bootstrap -> agent -> session -> task -> claim ->
heartbeats -> update under claim -> complete -> event audit trail."""

import httpx

from tests.helpers import BOOTSTRAP_TOKEN, auth


async def test_main_scenario(client: httpx.AsyncClient) -> None:
    # 1. Bootstrap.
    response = await client.post(
        "/api/v1/bootstrap",
        json={"tenantSlug": "acme", "tenantName": "Acme Corp", "adminDisplayName": "Alice"},
        headers=auth(BOOTSTRAP_TOKEN),
    )
    assert response.status_code == 201
    admin_key = response.json()["apiKey"]["key"]

    # 2. Create an agent principal with a scoped key.
    response = await client.post(
        "/api/v1/principals",
        json={"kind": "agent", "displayName": "Build Agent", "metadata": {"model": "x"}},
        headers=auth(admin_key),
    )
    assert response.status_code == 201
    agent = response.json()

    response = await client.post(
        f"/api/v1/principals/{agent['id']}/api-keys",
        json={
            "permissions": [
                "sessions.open",
                "tasks.read",
                "tasks.write",
                "tasks.claim",
                "events.read",
            ]
        },
        headers=auth(admin_key),
    )
    assert response.status_code == 201
    agent_key = response.json()["key"]

    # 3. The agent opens a session.
    response = await client.post(
        "/api/v1/sessions",
        json={"clientName": "builder", "clientVersion": "1.0.0"},
        headers=auth(agent_key),
    )
    assert response.status_code == 201
    session = response.json()

    # 4. A human creates a task.
    response = await client.post(
        "/api/v1/tasks",
        json={"title": "Ship the release", "priority": "high"},
        headers=auth(admin_key),
    )
    assert response.status_code == 201
    task = response.json()
    assert task["publicId"] == "TASK-000001"

    # 5. The agent claims it atomically.
    response = await client.post(
        f"/api/v1/tasks/{task['publicId']}:claim",
        json={"sessionId": session["id"], "intent": "build and ship"},
        headers=auth(agent_key),
    )
    assert response.status_code == 200
    claim = response.json()
    assert claim["fencingToken"] == 1

    # 6. Heartbeats keep both leases alive.
    assert (
        await client.post(f"/api/v1/sessions/{session['id']}:heartbeat", headers=auth(agent_key))
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/claims/{claim['id']}:heartbeat", headers=auth(agent_key))
    ).status_code == 200

    # 7. The agent records progress under its claim (optimistic + fencing).
    task_now = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))).json()
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={
            "description": "Built, tests green",
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
        },
        headers={**auth(agent_key), "If-Match": f'"task-{task_now["version"]}"'},
    )
    assert response.status_code == 200

    # 8. Complete the task with the claim credentials.
    version = response.json()["version"]
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers={**auth(agent_key), "If-Match": f'"task-{version}"'},
    )
    assert response.status_code == 200
    done = response.json()
    assert done["status"] == "done"
    assert done["completedAt"] is not None
    assert done["activeClaimId"] is None

    # 9. The event journal tells the whole story, in order.
    events = (await client.get("/api/v1/events", headers=auth(agent_key))).json()["items"]
    assert [e["type"] for e in events] == [
        "tenant.bootstrapped",
        "principal.created",
        "api_key.created",
        "session.opened",
        "task.created",
        "task.claimed",
        "task.updated",
        "claim.released",
        "task.completed",
    ]
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)

    # 10. The session closes cleanly.
    response = await client.post(f"/api/v1/sessions/{session['id']}:close", headers=auth(agent_key))
    assert response.status_code == 200
    assert response.json()["status"] == "closed"
