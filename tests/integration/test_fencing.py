"""The canonical fencing-token scenario from the spec.

1. Session A claims the task and receives fencing token 1.
2. Claim A expires.
3. Session B claims the task and receives fencing token 2.
4. Session A wakes up and tries to update the task with token 1.
5. The server answers 409 stale_claim and the task state is unchanged.
"""

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


async def test_stale_fencing_token_is_rejected(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, key_a = await create_agent_with_key(client, admin_key, name="agent-a")
    _, key_b = await create_agent_with_key(client, admin_key, name="agent-b")
    session_a = await open_session(client, key_a, client_name="a")
    session_b = await open_session(client, key_b, client_name="b")
    task = await create_task(client, admin_key)

    # 1. Session A claims: token 1.
    claim_a = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session_a["id"]},
            headers=auth(key_a),
        )
    ).json()
    assert claim_a["fencingToken"] == 1

    # 2. Claim A expires.
    backdate_expiry(sync_engine, "task_claims", claim_a["id"])

    # 3. Session B claims: token 2.
    claim_b = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session_b["id"]},
            headers=auth(key_b),
        )
    ).json()
    assert claim_b["fencingToken"] == 2

    task_before = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()

    # 4-5. Session A tries to write with the stale token -> 409 stale_claim.
    response = await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={
            "title": "old session writes",
            "claimId": claim_a["id"],
            "fencingToken": 1,
        },
        headers={**auth(key_a), "If-Match": f'"task-{task_before["version"]}"'},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_claim"

    # Completion attempts with the stale claim are rejected too.
    response = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={"claimId": claim_a["id"], "fencingToken": 1},
        headers={**auth(key_a), "If-Match": f'"task-{task_before["version"]}"'},
    )
    assert response.status_code == 409

    # Task state is unchanged.
    task_after = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert task_after == task_before
