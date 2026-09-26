"""v0.3 resume/reconnect: recovery through /harness/context and event replay."""

import httpx
import pytest

from tests.helpers import (
    auth,
    backdate_expiry,
    claim_task,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)

pytestmark = pytest.mark.usefixtures("clean_database")


async def test_restart_recovers_active_work(client: httpx.AsyncClient) -> None:
    """Scenario B: a fresh process (no memory) discovers its active claim/run
    from the server alone and can continue with the same fencing context."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key, title="Long job")
    session = await open_session(client, agent_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()

    # --- "process restart": only the API key survives -------------------------
    context = (await client.get("/api/v1/harness/context", headers=auth(agent_key))).json()
    my_claim = context["activeClaims"][0]
    my_run = context["activeRuns"][0]
    assert my_claim["taskId"] == task["id"]
    assert my_run["id"] == run["id"]

    # Heartbeats still work from the recovered identifiers.
    response = await client.post(
        f"/api/v1/sessions/{my_claim['sessionId']}:heartbeat", headers=auth(agent_key)
    )
    assert response.status_code == 200
    response = await client.post(
        f"/api/v1/claims/{my_claim['id']}:heartbeat", headers=auth(agent_key)
    )
    assert response.status_code == 200

    # And the run finishes under the recovered fencing context.
    response = await client.post(f"/api/v1/runs/{my_run['id']}:succeed", headers=auth(agent_key))
    assert response.status_code == 200
    assert response.json()["task"]["status"] == "done"


async def test_takeover_while_offline_is_detected(client: httpx.AsyncClient, sync_engine) -> None:
    """Scenario C: the old harness wakes up after a takeover — context shows
    no active claim, and a stale success attempt is rejected by fencing."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, old_key = await create_agent_with_key(client, admin_key, name="old")
    _, new_key = await create_agent_with_key(client, admin_key, name="new")
    task = await create_task(client, admin_key)

    old_session = await open_session(client, old_key)
    old_claim = (await claim_task(client, old_key, task["id"], old_session["id"])).json()
    old_run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": old_claim["id"], "fencingToken": old_claim["fencingToken"]},
            headers=auth(old_key),
        )
    ).json()

    # Laptop sleeps; the lease dies; someone else takes the task over.
    backdate_expiry(sync_engine, "task_claims", old_claim["id"])
    backdate_expiry(sync_engine, "sessions", old_session["id"])
    new_session = await open_session(client, new_key)
    response = await claim_task(client, new_key, task["id"], new_session["id"])
    assert response.status_code == 200

    # The waking harness consults context: no active claim on that task.
    context = (await client.get("/api/v1/harness/context", headers=auth(old_key))).json()
    assert context["activeClaims"] == []

    # A blind success attempt from the zombie is fenced off.
    response = await client.post(f"/api/v1/runs/{old_run['id']}:succeed", headers=auth(old_key))
    assert response.status_code == 409
    assert response.json()["error"]["code"] in ("stale_claim", "run_not_active")


async def test_event_replay_from_context_cursor(client: httpx.AsyncClient) -> None:
    """A harness stores eventCursor at bootstrap, works, disconnects, and
    later replays exactly what happened after the cursor — no gaps, in order."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)

    context = (await client.get("/api/v1/harness/context", headers=auth(agent_key))).json()
    cursor = context["eventCursor"]
    assert cursor.startswith("ec1_")

    task = await create_task(client, admin_key, title="After cursor")
    session = await open_session(client, agent_key)
    claim = (await claim_task(client, agent_key, task["id"], session["id"])).json()
    assert claim["fencingToken"] == 1

    response = await client.get(
        "/api/v1/events", params={"cursor": cursor}, headers=auth(agent_key)
    )
    body = response.json()
    events = body["items"]
    types = [e["type"] for e in events]
    assert types == ["task.created", "session.opened", "task.claimed"]
    assert all(e["cursor"].startswith("ec1_") for e in events)
    assert body["nextCursor"] == events[-1]["cursor"]
    assert body["hasMore"] is False
