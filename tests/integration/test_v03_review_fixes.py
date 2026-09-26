"""Regression tests for defects confirmed by the v0.3 adversarial review."""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import text

from tests.helpers import (
    ORG_AGENT_PERMISSIONS,
    assign_role,
    auth,
    create_agent_with_key,
    create_role,
    create_task,
    do_bootstrap,
    open_session,
)

pytestmark = pytest.mark.usefixtures("clean_database")


async def test_gated_principal_cannot_cancel_the_gate(client: httpx.AsyncClient) -> None:
    """approvals.manage alone must not void a gate imposed by someone else:
    cancelling a foreign gate requires decision eligibility."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, permissions=[*ORG_AGENT_PERMISSIONS, "approvals.manage"]
    )
    task = await create_task(client, admin_key, title="Supervised")

    # Supervisor gates the task, addressed to the supervisor for decision.
    approval = (
        await client.post(
            "/api/v1/approvals",
            json={"task": task["id"], "assignedPrincipalId": admin_id, "gate": True},
            headers=auth(admin_key),
        )
    ).json()

    # The gated agent holds approvals.manage but is neither the requester nor
    # eligible to decide: cancel is rejected and the gate holds.
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:cancel", headers=auth(agent_key)
    )
    assert response.status_code == 403
    # Either gate: the deciding permission or the organizational eligibility.
    assert response.json()["error"]["code"] in ("permission_denied", "not_eligible")

    session = await open_session(client, agent_key)
    blocked = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": session["id"]},
        headers=auth(agent_key),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "approval_required"

    # The requester itself may always cancel its own gate.
    response = await client.post(
        f"/api/v1/approvals/{approval['id']}:cancel", headers=auth(admin_key)
    )
    assert response.status_code == 200


async def test_own_gate_still_cancellable_and_role_decider_can_cancel(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=[*ORG_AGENT_PERMISSIONS, "approvals.manage"]
    )
    reviewer_role = await create_role(client, admin_key, "reviewer")
    task = await create_task(client, admin_key)

    # Agent gates its own task (self-suspend pattern) — and may cancel it.
    own = (
        await client.post(
            "/api/v1/approvals",
            json={"task": task["id"], "requiredRoleId": reviewer_role["id"], "gate": True},
            headers=auth(agent_key),
        )
    ).json()
    response = await client.post(f"/api/v1/approvals/{own['id']}:cancel", headers=auth(agent_key))
    assert response.status_code == 200

    # Holding the required role is not enough for a FOREIGN gate: cancelling
    # it needs the same authority as deciding it — permission AND eligibility.
    foreign = (
        await client.post(
            "/api/v1/approvals",
            json={"task": task["id"], "requiredRoleId": reviewer_role["id"], "gate": True},
            headers=auth(admin_key),
        )
    ).json()
    await assign_role(client, admin_key, agent["id"], reviewer_role["id"])
    response = await client.post(
        f"/api/v1/approvals/{foreign['id']}:cancel", headers=auth(agent_key)
    )
    assert response.status_code == 403  # role held, but no approvals.decide

    _, decider_key = await create_agent_with_key(
        client,
        admin_key,
        name="decider",
        permissions=["approvals.manage", "approvals.decide", "org.read"],
    )
    decider = (await client.get("/api/v1/principals", headers=auth(admin_key))).json()["items"]
    decider_id = next(p["id"] for p in decider if p["displayName"] == "decider")
    await assign_role(client, admin_key, decider_id, reviewer_role["id"])
    response = await client.post(
        f"/api/v1/approvals/{foreign['id']}:cancel", headers=auth(decider_key)
    )
    assert response.status_code == 200


async def test_gate_creation_serializes_with_completion(
    client: httpx.AsyncClient, sync_engine
) -> None:
    """A gate cannot attach to a task that is concurrently being completed:
    creating it takes the task row lock, so it either loses or wins cleanly."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)

    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    version = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))).json()[
        "version"
    ]

    # Hold the task row from another transaction to prove the gate request
    # waits for it instead of reading a soon-to-be-stale status.
    with sync_engine.connect() as blocker:
        blocker.execute(text("SELECT id FROM tasks WHERE id = :id FOR UPDATE"), {"id": task["id"]})
        pending = asyncio.create_task(
            client.post(
                "/api/v1/approvals",
                json={"task": task["id"], "assignedPrincipalId": admin_id, "gate": True},
                headers=auth(admin_key),
            )
        )
        await asyncio.sleep(0.2)
        assert not pending.done(), "gate creation must block on the locked task row"
        blocker.rollback()
        response = await pending
    assert response.status_code == 201

    # And with the gate in place, completion is refused.
    done = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers={**auth(agent_key), "If-Match": f'"task-{version}"'},
    )
    assert done.status_code == 409
    assert done.json()["error"]["code"] == "approval_required"


async def test_gate_on_terminal_task_rejected(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    admin_id = boot["adminPrincipal"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    task = await create_task(client, admin_key)

    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    version = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(agent_key))).json()[
        "version"
    ]
    done = await client.post(
        f"/api/v1/tasks/{task['id']}:complete",
        json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
        headers={**auth(agent_key), "If-Match": f'"task-{version}"'},
    )
    assert done.status_code == 200

    response = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": admin_id, "gate": True},
        headers=auth(admin_key),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_approval"

    # Non-gate approvals on terminal tasks remain allowed (pure audit records).
    response = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": admin_id},
        headers=auth(admin_key),
    )
    assert response.status_code == 201


async def test_finish_action_is_fenced(client: httpx.AsyncClient, sync_engine) -> None:
    """A zombie cannot keep writing outcomes into the audit trail of work it
    no longer owns: finishing an action re-validates the live claim."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    _, thief_key = await create_agent_with_key(client, admin_key, name="thief")
    task = await create_task(client, admin_key)

    session = await open_session(client, agent_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()
    run = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:start-run",
            json={"claimId": claim["id"], "fencingToken": claim["fencingToken"]},
            headers=auth(agent_key),
        )
    ).json()
    started = (
        await client.post(
            f"/api/v1/runs/{run['id']}/actions",
            json={"action": "long.tool", "status": "started"},
            headers=auth(agent_key),
        )
    ).json()

    # Takeover while the action is in flight.
    from tests.helpers import backdate_expiry

    backdate_expiry(sync_engine, "task_claims", claim["id"])
    thief_session = await open_session(client, thief_key)
    taken = await client.post(
        f"/api/v1/tasks/{task['id']}:claim",
        json={"sessionId": thief_session["id"]},
        headers=auth(thief_key),
    )
    assert taken.status_code == 200

    response = await client.post(
        f"/api/v1/runs/{run['id']}/actions/{started['id']}:finish",
        json={"status": "completed"},
        headers=auth(agent_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] in ("stale_claim", "run_not_active")

    # The record stays honestly unfinished.
    actions = (
        await client.get(f"/api/v1/runs/{run['id']}/actions", headers=auth(admin_key))
    ).json()["items"]
    assert actions[0]["status"] == "started"
    assert actions[0]["finishedAt"] is None


async def test_context_approvals_respect_role_scope(client: httpx.AsyncClient) -> None:
    """A role held in one workspace must not surface approvals of another."""
    from tests.helpers import create_workspace

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    agent, agent_key = await create_agent_with_key(
        client, admin_key, permissions=[*ORG_AGENT_PERMISSIONS, "approvals.read"]
    )
    alpha = await create_workspace(client, admin_key, "alpha")
    beta = await create_workspace(client, admin_key, "beta")
    child = await create_workspace(client, admin_key, "backend", parent_id=alpha["id"])
    reviewer = await create_role(client, admin_key, "reviewer")
    await assign_role(client, admin_key, agent["id"], reviewer["id"], workspace_id=alpha["id"])

    mine_task = await create_task(client, admin_key, workspaceId=child["id"])
    other_task = await create_task(client, admin_key, workspaceId=beta["id"])
    mine = (
        await client.post(
            "/api/v1/approvals",
            json={
                "task": mine_task["id"],
                "workspaceId": child["id"],
                "requiredRoleId": reviewer["id"],
            },
            headers=auth(admin_key),
        )
    ).json()
    other = (
        await client.post(
            "/api/v1/approvals",
            json={
                "task": other_task["id"],
                "workspaceId": beta["id"],
                "requiredRoleId": reviewer["id"],
            },
            headers=auth(admin_key),
        )
    ).json()

    context = (await client.get("/api/v1/harness/context", headers=auth(agent_key))).json()
    shown = {a["id"] for a in context["pendingApprovals"]}
    assert mine["id"] in shown  # role scope covers the descendant workspace
    assert other["id"] not in shown  # different subtree: not decidable, not shown


@pytest.mark.raw_journal
async def test_event_prefix_is_complete_under_xid_inversion(
    client: httpx.AsyncClient, sync_engine
) -> None:
    """Two writers whose xid order is the REVERSE of their sequence order.

    R takes the older xid but appends its event second; E takes the newer xid
    and appends first, then stays open. This inverted the v0.3
    sequence-ordered cursor for good (strict xfail until v0.4). Under the
    (tx_id, sequence) delivery order the reader may hand out R's event while
    E is pending — but E's position sorts after R's, so a cursor advanced to
    R still replays E once it commits. Nothing is permanently skipped.
    """
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    tenant_id = boot["tenant"]["id"]

    def event_sql(name: str) -> str:
        return (
            "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id, "
            f"correlation_id, request_id, payload, occurred_at) VALUES (gen_random_uuid(), "
            f"'{tenant_id}', '{name}', 'test', gen_random_uuid(), 'c', 'r', '{{}}', now()) "
            "RETURNING sequence"
        )

    with sync_engine.connect() as r_conn, sync_engine.connect() as e_conn:
        # Assign xids in R-then-E order without taking locks the other needs.
        r_conn.execute(text("SELECT pg_current_xact_id()"))
        e_conn.execute(text("SELECT pg_current_xact_id()"))
        e_seq = e_conn.execute(text(event_sql("E.event"))).scalar_one()
        r_seq = r_conn.execute(text(event_sql("R.event"))).scalar_one()
        r_conn.commit()  # R (older xid, higher sequence) commits; E still open

        # Reader drains everything stable and advances its cursor to the end.
        page = (
            await client.get("/api/v1/events", params={"limit": 200}, headers=auth(agent_key))
        ).json()
        delivered_before = {e["sequence"] for e in page["items"]}
        assert r_seq in delivered_before  # R is stable and delivered
        assert e_seq not in delivered_before  # E is pending, not delivered
        cursor_at_r = page["nextCursor"]

        e_conn.commit()

    # E committed AFTER the cursor moved past R. The old sequence cursor lost
    # it forever; the (tx_id, sequence) cursor must now deliver it.
    suffix = (
        await client.get(
            "/api/v1/events",
            params={"cursor": cursor_at_r, "limit": 200},
            headers=auth(agent_key),
        )
    ).json()["items"]
    suffix_sequences = [e["sequence"] for e in suffix]
    assert e_seq in suffix_sequences, (
        f"event at sequence {e_seq} was permanently skipped after the cursor "
        f"advanced to {cursor_at_r!r}: the v0.3 inversion defect is back"
    )


async def test_event_cursor_stops_before_uncommitted_gap(
    client: httpx.AsyncClient, sync_engine
) -> None:
    """eventCursor must never point past a hole: with an uncommitted event
    pinning the stable horizon, later-committed events stay ahead of the
    cursor and are replayed from it once the horizon moves."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    tenant_id = boot["tenant"]["id"]

    with sync_engine.connect() as conn:
        # Uncommitted event (transaction stays open, pinning xmin).
        conn.execute(
            text(
                "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id, "
                "correlation_id, request_id, payload, occurred_at) "
                "VALUES (:id, :tenant, 'test.uncommitted', 'test', :eid, 'c', 'r', '{}', now()) "
                "RETURNING sequence"
            ),
            {"id": str(uuid.uuid4()), "tenant": tenant_id, "eid": str(uuid.uuid4())},
        ).scalar_one()

        # Committed event in a newer transaction (via HTTP).
        task = await create_task(client, admin_key, title="After the gap")

        # While the older transaction is open, neither event is stable: the
        # cursor stays put and a replay from it delivers nothing yet.
        context = (await client.get("/api/v1/harness/context", headers=auth(agent_key))).json()
        cursor_during_gap = context["eventCursor"]
        held_back = (
            await client.get(
                "/api/v1/events",
                params={"cursor": cursor_during_gap, "limit": 200},
                headers=auth(agent_key),
            )
        ).json()["items"]
        assert all(e["entityId"] != task["id"] for e in held_back)
        conn.rollback()

    # After the in-flight transaction is gone the horizon advances: a replay
    # from the SAME cursor now sees the committed event — nothing was skipped.
    events = (
        await client.get(
            "/api/v1/events",
            params={"cursor": cursor_during_gap, "limit": 200},
            headers=auth(agent_key),
        )
    ).json()["items"]
    assert any(e["type"] == "task.created" and e["entityId"] == task["id"] for e in events)
    context = (await client.get("/api/v1/harness/context", headers=auth(agent_key))).json()
    all_events = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(agent_key))
    ).json()
    assert context["eventCursor"] == all_events["nextCursor"]
