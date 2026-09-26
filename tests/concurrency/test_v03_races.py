"""v0.3 races: cancellation vs success, discovery/claim TOCTOU, suspend races,
gate decision commit visibility."""

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


async def _claim_and_run(
    client: httpx.AsyncClient, key: str, task_id: str
) -> tuple[dict, dict, dict]:
    session = await open_session(client, key)
    claim = (await claim_task(client, key, task_id, session["id"])).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task_id}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(key),
        )
    ).json()
    return session, claim, run


async def test_cancel_vs_succeed_exactly_one_terminal_state(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """An admin cancels while the executor succeeds: exactly one wins, and
    after a committed cancellation success is impossible."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    cancel, succeed = await asyncio.gather(
        client.post(f"/api/v1/runs/{run['id']}:cancel", headers=auth(admin_key)),
        client.post(f"/api/v1/runs/{run['id']}:succeed", headers=auth(agent_key)),
    )
    assert sorted([cancel.status_code, succeed.status_code]) == [200, 409]

    with sync_engine.connect() as conn:
        status, task_status = conn.execute(
            text(
                "SELECT r.status, t.status FROM runs r JOIN tasks t ON t.id = r.task_id "
                "WHERE r.id = :id"
            ),
            {"id": run["id"]},
        ).one()
    if cancel.status_code == 200:
        assert status == "cancelled"
        assert task_status != "done"
        # The committed cancellation permanently blocks a late success.
        late = await client.post(f"/api/v1/runs/{run['id']}:succeed", headers=auth(agent_key))
        assert late.status_code == 409
    else:
        assert status == "succeeded"
        assert task_status == "done"


async def test_discovery_claim_toctou(client: httpx.AsyncClient) -> None:
    """Discovery is advisory: a task shown as available may be claimed by a
    rival first; the loser gets a clean 409, never a broken state."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, a_key = await create_agent_with_key(client, admin_key, name="a")
    _, b_key = await create_agent_with_key(client, admin_key, name="b")
    task = await create_task(client, admin_key)

    shown_a = await client.get("/api/v1/work/available", headers=auth(a_key))
    shown_b = await client.get("/api/v1/work/available", headers=auth(b_key))
    assert task["id"] in [t["id"] for t in shown_a.json()["items"]]
    assert task["id"] in [t["id"] for t in shown_b.json()["items"]]

    session_a = await open_session(client, a_key)
    session_b = await open_session(client, b_key)
    first, second = await asyncio.gather(
        claim_task(client, a_key, task["id"], session_a["id"]),
        claim_task(client, b_key, task["id"], session_b["id"]),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [200, 409]
    loser = first if first.status_code == 409 else second
    assert loser.json()["error"]["code"] == "task_already_claimed"

    # And the task disappears from discovery for everyone.
    shown = await client.get("/api/v1/work/available", headers=auth(b_key))
    assert task["id"] not in [t["id"] for t in shown.json()["items"]]


async def test_concurrent_suspend_and_succeed_single_outcome(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    _, _, run = await _claim_and_run(client, agent_key, task["id"])

    suspend, succeed = await asyncio.gather(
        client.post(f"/api/v1/runs/{run['id']}:suspend", headers=auth(agent_key)),
        client.post(f"/api/v1/runs/{run['id']}:succeed", headers=auth(agent_key)),
    )
    assert sorted([suspend.status_code, succeed.status_code]) == [200, 409]
    with sync_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM runs WHERE id = :id"), {"id": run["id"]}
        ).scalar()
    assert status in ("suspended", "succeeded")
    with sync_engine.connect() as conn:
        active = conn.execute(
            text("SELECT count(*) FROM task_claims WHERE status = 'active'")
        ).scalar()
    assert active == 0  # either path released the claim


async def test_uncommitted_gate_decision_keeps_task_blocked(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Approval gate honors commit visibility: an uncommitted approve leaves
    the task unclaimable (same discipline as dependency completion)."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)
    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"task": task["id"], "assignedPrincipalId": admin_id, "gate": True},
            headers=auth(admin_key),
        )
    ).json()

    session = await open_session(client, agent_key)
    with sync_engine.connect() as conn:
        conn.execute(
            text("UPDATE approvals SET status = 'approved' WHERE id = :id"),
            {"id": approval["id"]},
        )
        # Uncommitted decision: still gated.
        response = await claim_task(client, agent_key, task["id"], session["id"])
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "approval_required"
        conn.rollback()

    await client.post(f"/api/v1/approvals/{approval['id']}:approve", headers=auth(admin_key))
    response = await claim_task(client, agent_key, task["id"], session["id"])
    assert response.status_code == 200
