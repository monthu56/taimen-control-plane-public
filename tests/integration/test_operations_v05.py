"""Operator surface: adapter diagnostics/redrive/rebuild and journal retention.

The whole point of these endpoints is that the dangerous operations are the
ones that DON'T exist: nothing here can move a delivery cursor forward, and
nothing can delete an event a consumer still needs (ADR-0037, ADR-0038).
"""

import uuid

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.worker.context_adapter import CONSUMER_NAME
from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
)


def _drain_outbox(sync_engine: Engine) -> None:
    """Pretend the worker delivered everything: retention waits for the outbox."""
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET delivered_at = now() WHERE delivered_at IS NULL"))


async def _seed_cursor(sync_engine: Engine, tenant_id: str, *, tx_id: int, sequence: int) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO event_consumer_cursors (name, tenant_id, tx_id, sequence,"
                " updated_at, metadata) VALUES (:name, :tenant, :tx, :seq, now(), '{}')"
                " ON CONFLICT (name, tenant_id) DO UPDATE SET tx_id = :tx, sequence = :seq"
            ),
            {"name": CONSUMER_NAME, "tenant": tenant_id, "tx": tx_id, "seq": sequence},
        )


def _cursor(sync_engine: Engine, tenant_id: str):
    with sync_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT tx_id, sequence, parked_at, failure_count, next_attempt_at"
                " FROM event_consumer_cursors WHERE name = :name AND tenant_id = :tenant"
            ),
            {"name": CONSUMER_NAME, "tenant": tenant_id},
        ).first()


async def test_diagnostics_are_read_only_and_scoped_to_the_caller_tenant(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await create_task(client, admin_key, title="Lagging")
    await _seed_cursor(sync_engine, tenant_id, tx_id=0, sequence=0)

    response = await client.get("/api/v1/operations/context-adapter", headers=auth(admin_key))
    assert response.status_code == 200
    body = response.json()
    assert body["consumer"] == CONSUMER_NAME
    assert body["tenantId"] == tenant_id
    assert body["cursor"].startswith("ec1_")
    assert body["lagEvents"] > 0
    assert body["parked"] is False
    assert body["journal"]["journalFloorCursor"].startswith("ec1_")

    before = _cursor(sync_engine, tenant_id)
    await client.get("/api/v1/operations/context-adapter", headers=auth(admin_key))
    assert _cursor(sync_engine, tenant_id) == before, "diagnostics must not mutate state"


async def test_redrive_requires_operations_manage(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await _seed_cursor(sync_engine, tenant_id, tx_id=0, sequence=0)

    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["operations.read"]
    )
    _, blind_key = await create_agent_with_key(
        client, admin_key, name="blind", permissions=["tasks.read"]
    )

    assert (
        await client.get("/api/v1/operations/context-adapter", headers=auth(reader_key))
    ).status_code == 200
    denied = await client.get("/api/v1/operations/context-adapter", headers=auth(blind_key))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"

    forbidden = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={},
        headers=auth(reader_key),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["details"]["required"] == ["operations.manage"]


async def test_redrive_for_another_tenant_is_a_404(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Cross-tenant ids must not be distinguishable from non-existent ones."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    other_tenant, _ = make_tenant_directly(sync_engine, "other")
    await _seed_cursor(sync_engine, other_tenant, tx_id=0, sequence=0)

    response = await client.post(
        f"/api/v1/operations/context-adapter/{other_tenant}:redrive",
        json={},
        headers=auth(admin_key),
    )
    assert response.status_code == 404
    unknown = await client.post(
        f"/api/v1/operations/context-adapter/{uuid.uuid4()}:redrive",
        json={},
        headers=auth(admin_key),
    )
    assert unknown.status_code == 404
    assert response.json()["error"]["code"] == unknown.json()["error"]["code"]


async def test_redrive_never_moves_the_cursor_and_is_audited(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await create_task(client, admin_key, title="Poisoned")
    await _seed_cursor(sync_engine, tenant_id, tx_id=0, sequence=0)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE event_consumer_cursors SET parked_at = now(),"
                " parked_reason = 'bad mapping', failure_count = 3,"
                " next_attempt_at = now() + interval '1 hour'"
                " WHERE name = :name AND tenant_id = :tenant"
            ),
            {"name": CONSUMER_NAME, "tenant": tenant_id},
        )

    before = _cursor(sync_engine, tenant_id)
    response = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={"reason": "fixed the mapping"},
        headers=auth(admin_key),
    )
    assert response.status_code == 200, response.text
    after = _cursor(sync_engine, tenant_id)
    assert (after.tx_id, after.sequence) == (before.tx_id, before.sequence)
    assert after.parked_at is None
    assert after.failure_count == 0
    assert after.next_attempt_at is None

    events = (
        await client.get(
            "/api/v1/events",
            params={"limit": 200, "entityType": "event_consumer"},
            headers=auth(admin_key),
        )
    ).json()["items"]
    redriven = [e for e in events if e["type"] == "context_adapter.redriven"]
    assert len(redriven) == 1
    assert redriven[0]["payload"]["reason"] == "fixed the mapping"
    assert redriven[0]["payload"]["wasParked"] is True


async def test_redrive_is_idempotent(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await _seed_cursor(sync_engine, tenant_id, tx_id=0, sequence=0)

    key = str(uuid.uuid4())
    first = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    second = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert second.headers.get("Idempotency-Replayed") == "true"

    # Even without the key, redriving a healthy row is a harmless no-op.
    third = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={},
        headers=auth(admin_key),
    )
    assert third.status_code == 200 and third.json()["parked"] is False


async def test_rebuild_only_moves_backwards(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await create_task(client, admin_key, title="History")
    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])

    current = (
        await client.get("/api/v1/operations/context-adapter", headers=auth(admin_key))
    ).json()["cursor"]

    # Rewind to the origin: the adapter will re-deliver everything.
    rewound = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:rebuild",
        json={"reason": "memory lost"},
        headers=auth(admin_key),
    )
    assert rewound.status_code == 200, rewound.text
    row = _cursor(sync_engine, tenant_id)
    assert (row.tx_id, row.sequence) == (0, 0)

    # Forward is refused: rebuild must not become a way to skip a poison event.
    forward = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:rebuild",
        json={"cursor": current},
        headers=auth(admin_key),
    )
    assert forward.status_code == 422
    assert forward.json()["error"]["code"] == "cursor_must_not_advance"
    row = _cursor(sync_engine, tenant_id)
    assert (row.tx_id, row.sequence) == (0, 0), "a refused rebuild changes nothing"


async def test_archive_moves_history_and_replay_still_spans_it(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    for index in range(3):
        await create_task(client, admin_key, title=f"Archived {index}")

    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])
    _drain_outbox(sync_engine)

    before = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(agent_key))
    ).json()["items"]

    # before_seconds=0 means "as old as right now", i.e. everything confirmed.
    archived = await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived"] > 0

    with sync_engine.connect() as conn:
        hot = conn.execute(text("SELECT count(*) FROM events")).scalar()
        cold = conn.execute(text("SELECT count(*) FROM event_archive")).scalar()
    assert cold > 0

    # Replay from the origin still returns the archived events plus whatever
    # the archive action itself wrote to the hot table.
    after = (
        await client.get("/api/v1/events", params={"limit": 200}, headers=auth(agent_key))
    ).json()["items"]
    assert {e["id"] for e in before} <= {e["id"] for e in after}
    assert hot >= 1, "the archive action's own audit event stays hot"


async def test_prune_makes_old_cursors_report_the_floor(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    await create_task(client, admin_key, title="Will be pruned")

    origin_page = await client.get("/api/v1/events", params={"limit": 1}, headers=auth(agent_key))
    assert origin_page.status_code == 200

    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])
    _drain_outbox(sync_engine)

    await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    pruned = await client.post(
        "/api/v1/operations/journal:prune",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    assert pruned.status_code == 200, pruned.text
    assert pruned.json()["pruned"] > 0

    # A cursor below the archive floor is a machine-readable error, never a
    # silently short page.
    origin_cursor = "ec1_eyJzIjowLCJ0IjowfQ"  # {"t":0,"s":0}
    response = await client.get(
        "/api/v1/events", params={"cursor": origin_cursor}, headers=auth(agent_key)
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "cursor_below_journal_floor"
    assert error["details"]["floorCursor"].startswith("ec1_")


async def test_retention_refuses_to_outrun_a_consumer(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Events a consumer has not confirmed are never archived away."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await create_task(client, admin_key, title="Not yet delivered")
    # Cursor left at the origin: nothing is confirmed.
    await _seed_cursor(sync_engine, tenant_id, tx_id=0, sequence=0)

    with sync_engine.connect() as conn:
        before = conn.execute(text("SELECT count(*) FROM events")).scalar()

    response = await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    assert response.status_code == 200
    assert response.json()["archived"] == 0

    with sync_engine.connect() as conn:
        after = conn.execute(text("SELECT count(*) FROM events")).scalar()
    assert after >= before, "unconfirmed events must stay in the hot journal"


async def test_undelivered_outbox_blocks_archiving(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """Work still in flight in the outbox pins the retention horizon."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await create_task(client, admin_key, title="Outbox pending")
    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
    # Everything is confirmed by the consumer, but the outbox is untouched.
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])

    response = await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    assert response.status_code == 200
    assert response.json()["archived"] == 0

    _drain_outbox(sync_engine)
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])
    retried = await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    assert retried.json()["archived"] > 0


async def test_retention_without_any_consumer_is_refused(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    with sync_engine.begin() as conn:
        conn.execute(text("DELETE FROM event_consumer_cursors"))

    response = await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "retention_blocked_by_consumer"


# --- regressions from the v0.5 adversarial review -----------------------------


async def test_rebuild_below_the_journal_floor_still_replays_the_archive(
    client: httpx.AsyncClient, sync_engine: Engine, settings, app
) -> None:
    """The adapter must read the archive, not just the hot table.

    Reading only ``events`` would make a rebuild past the journal floor
    deliver nothing for the archived stretch and then jump forward — a silent,
    permanent gap in the rebuilt memory (ADR-0038).
    """
    from control_plane.worker.context_adapter import ContextAdapter
    from tests.integration.test_context_adapter_v05 import FakeMemory

    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    for index in range(3):
        await create_task(client, admin_key, title=f"Archived work {index}")

    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])
    _drain_outbox(sync_engine)

    archived = await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    assert archived.status_code == 200 and archived.json()["archived"] > 0

    # Rewind to the origin: everything the adapter must now re-deliver lives
    # in event_archive, not in events.
    rebuilt = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:rebuild",
        json={"reason": "memory lost"},
        headers=auth(admin_key),
    )
    assert rebuilt.status_code == 200, rebuilt.text

    memory = FakeMemory()
    adapter = ContextAdapter(settings, engine=app.state.engine, provider=memory)
    for _ in range(10):
        if await adapter.deliver_once() == 0:
            break
    kinds = [o["kind"] for o in memory.store.values()]
    assert kinds, "the archived stretch must be re-delivered"
    assert kinds.count("task.created") >= 3

    row = _cursor(sync_engine, tenant_id)
    assert (row.tx_id, row.sequence) >= (latest[0], latest[1])


async def test_an_in_flight_batch_does_not_undo_an_operator_rebuild(
    client: httpx.AsyncClient, sync_engine: Engine, settings, app
) -> None:
    """A commit computed before the rebuild must be discarded, not applied."""
    from control_plane.application.event_cursor import EventPosition
    from control_plane.worker.context_adapter import ContextAdapter
    from tests.integration.test_context_adapter_v05 import FakeMemory

    boot = await do_bootstrap(client)
    tenant_id = boot["tenant"]["id"]
    await create_task(client, boot["apiKey"]["key"], title="Racing batch")
    await _seed_cursor(sync_engine, tenant_id, tx_id=0, sequence=0)

    adapter = ContextAdapter(settings, engine=app.state.engine, provider=FakeMemory())
    stale_start = EventPosition(0, 0)

    # The operator rebuilds to a DIFFERENT position while the batch is running.
    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])

    await adapter.commit_cursor(
        tenant_id, EventPosition(latest[0], latest[1] - 1), expected_from=stale_start
    )
    row = _cursor(sync_engine, tenant_id)
    assert (row.tx_id, row.sequence) == (latest[0], latest[1]), "the rebuild must stand"


@pytest.mark.raw_journal
async def test_retention_is_scoped_to_the_calling_tenant(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """One tenant's operator must not be able to prune another tenant's history.

    ``operations.manage`` is a tenant-scoped permission; before this was fixed
    archive/prune acted on the whole deployment (review finding).
    """
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_a = boot["tenant"]["id"]
    await create_task(client, admin_key, title="Tenant A history")

    tenant_b, _ = make_tenant_directly(sync_engine, "tenant-b")
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
                " correlation_id, request_id, payload, occurred_at)"
                " VALUES (:id, :t, 'task.created', 'task', :e, 'c', 'r', '{}', now())"
            ),
            {"id": str(uuid.uuid4()), "t": tenant_b, "e": str(uuid.uuid4())},
        )
    await _seed_cursor(sync_engine, tenant_b, tx_id=0, sequence=0)

    with sync_engine.connect() as conn:
        latest = conn.execute(
            text(
                "SELECT tx_id, sequence FROM events WHERE tenant_id = :t"
                " ORDER BY tx_id DESC, sequence DESC LIMIT 1"
            ),
            {"t": tenant_a},
        ).one()
        before_b = conn.execute(
            text("SELECT count(*) FROM events WHERE tenant_id = :t"), {"t": tenant_b}
        ).scalar()
    await _seed_cursor(sync_engine, tenant_a, tx_id=latest[0], sequence=latest[1])
    _drain_outbox(sync_engine)

    archived = await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    assert archived.status_code == 200 and archived.json()["archived"] > 0

    with sync_engine.connect() as conn:
        after_b = conn.execute(
            text("SELECT count(*) FROM events WHERE tenant_id = :t"), {"t": tenant_b}
        ).scalar()
        archived_b = conn.execute(
            text("SELECT count(*) FROM event_archive WHERE tenant_id = :t"), {"t": tenant_b}
        ).scalar()
    assert after_b == before_b, "tenant B's journal must be untouched"
    assert archived_b == 0

    pruned = await client.post(
        "/api/v1/operations/journal:prune", json={"beforeSeconds": 0}, headers=auth(admin_key)
    )
    assert pruned.status_code == 200
    with sync_engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE tenant_id = :t"), {"t": tenant_b}
            ).scalar()
            == before_b
        )
        floors = conn.execute(text("SELECT count(*) FROM event_journal_floor")).scalar()
    assert floors >= 2, "each tenant carries its own floor"


async def test_a_fresh_reader_is_not_locked_out_after_a_prune(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """`GET /events` with no cursor means "from what still exists"."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    await create_task(client, admin_key, title="Will be pruned")

    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])
    _drain_outbox(sync_engine)
    await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    await client.post(
        "/api/v1/operations/journal:prune", json={"beforeSeconds": 0}, headers=auth(admin_key)
    )

    fresh = await client.get("/api/v1/events", params={"limit": 50}, headers=auth(agent_key))
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["nextCursor"].startswith("ec1_")


async def test_a_legacy_sequence_cursor_below_the_floor_is_rejected(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The legacy integer cursor gets the same protection as an opaque one."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    await create_task(client, admin_key, title="Will be pruned")

    with sync_engine.connect() as conn:
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
    await _seed_cursor(sync_engine, tenant_id, tx_id=latest[0], sequence=latest[1])
    _drain_outbox(sync_engine)
    await client.post(
        "/api/v1/operations/journal:archive",
        json={"beforeSeconds": 0},
        headers=auth(admin_key),
    )
    await client.post(
        "/api/v1/operations/journal:prune", json={"beforeSeconds": 0}, headers=auth(admin_key)
    )

    response = await client.get("/api/v1/events", params={"after": 0}, headers=auth(agent_key))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "cursor_below_journal_floor"


async def test_a_manage_only_key_can_actually_redrive(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """`operations.manage` alone must work end to end (review finding).

    The write endpoints answer with the diagnostics body; if that body
    required `operations.read` separately, the permission the docs prescribe
    would 403 AFTER the mutation and roll the un-park back.
    """
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await _seed_cursor(sync_engine, tenant_id, tx_id=0, sequence=0)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE event_consumer_cursors SET parked_at = now(),"
                " parked_reason = 'poison', failure_count = 2"
                " WHERE name = :name AND tenant_id = :tenant"
            ),
            {"name": CONSUMER_NAME, "tenant": tenant_id},
        )

    _, manage_key = await create_agent_with_key(
        client, admin_key, name="ops", permissions=["operations.manage"]
    )
    response = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={"reason": "cause fixed"},
        headers=auth(manage_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["parked"] is False
    row = _cursor(sync_engine, tenant_id)
    assert row.parked_at is None, "the un-park must have been committed"

    rebuilt = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:rebuild",
        json={},
        headers=auth(manage_key),
    )
    assert rebuilt.status_code == 200, rebuilt.text


async def test_diagnostics_shape_is_stable_before_the_first_delivery(
    client: httpx.AsyncClient,
) -> None:
    """No cursor row yet must not mean a different response shape."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]

    empty = (await client.get("/api/v1/operations/context-adapter", headers=auth(admin_key))).json()
    assert empty["cursor"] is None
    assert empty["parked"] is False
    assert empty["failureCount"] == 0
    assert empty["journal"]["archiveFloorCursor"].startswith("ec1_")

    await create_task(client, admin_key, title="Now there is work")
    with_cursor = (
        await client.get("/api/v1/operations/context-adapter", headers=auth(admin_key))
    ).json()
    assert set(with_cursor) == set(empty), "the shape must not depend on adapter state"
