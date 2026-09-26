"""v0.2 races: eligible-claim, dependency-commit, approval decision, idempotency."""

import asyncio
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import (
    assign_role,
    auth,
    claim_task,
    create_agent_with_key,
    create_role,
    create_task,
    do_bootstrap,
    open_session,
)


async def test_concurrent_eligible_claims_exactly_one_wins(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Several ELIGIBLE principals race for one task with requirements."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    role = await create_role(client, admin_key, "engineer")

    contenders = []
    for i in range(6):
        principal, key = await create_agent_with_key(client, admin_key, name=f"agent-{i}")
        await assign_role(client, admin_key, principal["id"], role["id"])
        session = await open_session(client, key, client_name=f"agent-{i}")
        contenders.append((key, session["id"]))

    task = await create_task(client, admin_key, requirements={"roles": ["engineer"]})

    responses = await asyncio.gather(
        *[claim_task(client, key, task["id"], session_id) for key, session_id in contenders]
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == 5, statuses

    with sync_engine.connect() as conn:
        active = conn.execute(
            text("SELECT count(*) FROM task_claims WHERE status = 'active'")
        ).scalar()
    assert active == 1


async def test_dependency_completion_must_be_committed(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """A dependent task is claimable only after the prerequisite completion
    has actually COMMITTED — an in-flight (uncommitted) completion is invisible."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)

    dependent = await create_task(client, admin_key, title="Dependent")
    prerequisite = await create_task(client, admin_key, title="Prerequisite")
    await client.post(
        f"/api/v1/tasks/{dependent['id']}/relations",
        json={"toTask": prerequisite["id"], "type": "depends_on"},
        headers=auth(admin_key),
    )

    # Open a transaction that completes the prerequisite but do NOT commit yet.
    with sync_engine.connect() as conn:
        conn.execute(
            text(
                "UPDATE tasks SET status = 'done',"
                " system_status_category = 'terminal_success',"
                " completed_at = now() WHERE id = :id"
            ),
            {"id": prerequisite["id"]},
        )
        # Uncommitted completion: the dependent must still be blocked.
        response = await claim_task(client, agent_key, dependent["id"], session["id"])
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "task_not_ready"
        conn.commit()

    # Committed: now claimable.
    response = await claim_task(client, agent_key, dependent["id"], session["id"])
    assert response.status_code == 200, response.text


async def test_concurrent_approval_decisions_single_terminal_state(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    role = await create_role(client, admin_key, "approver")
    deciders = []
    for i in range(4):
        principal, key = await create_agent_with_key(
            client,
            admin_key,
            name=f"decider-{i}",
            permissions=["approvals.read", "approvals.decide"],
        )
        await assign_role(client, admin_key, principal["id"], role["id"])
        deciders.append(key)

    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"requiredRoleId": role["id"]},
            headers=auth(admin_key),
        )
    ).json()

    async def decide(key: str, approve: bool) -> httpx.Response:
        action = "approve" if approve else "reject"
        return await client.post(f"/api/v1/approvals/{approval['id']}:{action}", headers=auth(key))

    responses = await asyncio.gather(*[decide(key, i % 2 == 0) for i, key in enumerate(deciders)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) == 1, statuses
    assert statuses.count(409) == 3, statuses

    final = (
        await client.get(f"/api/v1/approvals/{approval['id']}", headers=auth(admin_key))
    ).json()
    assert final["status"] in ("approved", "rejected")
    with sync_engine.connect() as conn:
        decision_events = conn.execute(
            text(
                "SELECT count(*) FROM events "
                "WHERE event_type IN ('approval.approved', 'approval.rejected')"
            )
        ).scalar()
    assert decision_events == 1


async def test_parallel_artifact_creation_with_same_idempotency_key(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    key = str(uuid.uuid4())

    async def create() -> httpx.Response:
        return await client.post(
            "/api/v1/artifacts",
            json={"type": "report", "name": "Q3"},
            headers={**auth(admin_key), "Idempotency-Key": key},
        )

    responses = await asyncio.gather(*[create() for _ in range(3)])
    assert [r.status_code for r in responses] == [201, 201, 201]
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1  # one logical artifact

    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM artifacts")).scalar() == 1


async def test_concurrent_relation_inserts_cannot_form_cycle(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Two individually-acyclic edges inserted concurrently must not compose
    into a cycle (the per-tenant graph advisory lock serializes them)."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    a = await create_task(client, admin_key, title="A")
    b = await create_task(client, admin_key, title="B")

    async def relate(from_id: str, to_id: str) -> httpx.Response:
        return await client.post(
            f"/api/v1/tasks/{from_id}/relations",
            json={"toTask": to_id, "type": "depends_on"},
            headers=auth(admin_key),
        )

    first, second = await asyncio.gather(relate(a["id"], b["id"]), relate(b["id"], a["id"]))
    statuses = sorted((first.status_code, second.status_code))
    assert statuses == [201, 422], statuses

    with sync_engine.connect() as conn:
        edges = conn.execute(
            text("SELECT count(*) FROM task_relations WHERE relation_type = 'depends_on'")
        ).scalar()
    assert edges == 1
