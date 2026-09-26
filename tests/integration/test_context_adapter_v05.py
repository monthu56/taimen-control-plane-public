"""Context Adapter: at-least-once delivery, per-tenant cursors, poison parking.

v0.5 changes the failure blast radius: a provider rejection parks ONE tenant's
row instead of raising out of the cycle, and every other tenant keeps
flowing (ADR-0036). The delivery guarantees themselves are unchanged — the
cursor still advances only after a fully confirmed batch, and no code path can
step over a poison event.
"""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from control_plane.infrastructure.context_provider.base import (
    ContextProviderError,
    IngestResult,
)
from control_plane.worker.context_adapter import CONSUMER_NAME, ContextAdapter
from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    make_tenant_directly,
)


class FakeMemory:
    """In-memory Memory Service double with real duplicate semantics."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, int]] = []
        self.fail_mode: str | None = None  # None | "down" | "reject"
        self.fail_namespaces: set[str] = set()
        self.trace_ids: list[str | None] = []
        self.duplicates = 0

    def _should_fail(self, namespace: str) -> bool:
        if self.fail_mode is None:
            return False
        return not self.fail_namespaces or namespace in self.fail_namespaces

    async def ingest_batch(
        self,
        *,
        namespace: str,
        observations: list[dict[str, Any]],
        trace_run_id: str | None = None,
    ) -> IngestResult:
        self.calls.append((namespace, len(observations)))
        self.trace_ids.append(trace_run_id)
        if self._should_fail(namespace):
            if self.fail_mode == "down":
                raise ContextProviderError("connection refused", retryable=True)
            return IngestResult(failed=1, errors=[{"index": 0, "error": "invalid kind"}])
        accepted = 0
        duplicates = 0
        for observation in observations:
            key = (namespace, observation["source"]["external_id"])
            if key in self.store:
                duplicates += 1
                self.duplicates += 1
            else:
                self.store[key] = observation
                accepted += 1
        return IngestResult(accepted=accepted, duplicates=duplicates)

    async def build_context(
        self,
        *,
        namespace: str,
        request: dict[str, Any],
        trace_run_id: str | None = None,
        namespaces: list[str] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def healthy(self) -> bool:
        return self.fail_mode is None

    async def aclose(self) -> None:
        pass


@pytest.fixture
def adapter(settings, app):
    """A ContextAdapter over the app's engine with a FakeMemory provider."""
    memory = FakeMemory()
    instance = ContextAdapter(settings, engine=app.state.engine, provider=memory)
    instance.memory = memory  # type: ignore[attr-defined]
    return instance


def _cursor_row(sync_engine, tenant_id: str) -> Any:
    with sync_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT tx_id, sequence, metadata, parked_at, parked_reason, failure_count,"
                " parked_event_id FROM event_consumer_cursors"
                " WHERE name = :name AND tenant_id = :tenant"
            ),
            {"name": CONSUMER_NAME, "tenant": tenant_id},
        ).first()


async def test_adapter_delivers_and_advances_cursor(
    client: httpx.AsyncClient, adapter, sync_engine
) -> None:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await create_task(client, admin_key, title="Deliver me")

    moved = await adapter.deliver_once()
    assert moved > 0
    memory = adapter.memory
    keys = {external_id for (_ns, external_id) in memory.store}
    assert any(k.startswith("event:") for k in keys)
    assert {ns for (ns, _k) in memory.store} == {f"tenant:{tenant_id}"}
    # Every ingest call carries an adapter-scoped trace id (ADR-0039).
    assert all(t and t.startswith("adapter_") for t in memory.trace_ids)

    row = _cursor_row(sync_engine, tenant_id)
    assert row is not None
    assert row.metadata["delivered_total"] >= 2  # bootstrap + task.created at least
    assert row.parked_at is None
    # Idle cycle: nothing new, cursor stays.
    assert await adapter.deliver_once() == 0


async def test_noise_advances_cursor_without_delivery(
    client: httpx.AsyncClient, adapter, sync_engine
) -> None:
    boot = await do_bootstrap(client)
    await adapter.deliver_once()
    before = len(adapter.memory.store)
    # session.opened is noise: journal grows, memory does not.
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    await client.post("/api/v1/sessions", json={"clientName": "noise"}, headers=auth(agent_key))
    moved = await adapter.deliver_once()
    assert moved > 0
    stored_kinds = {o["kind"] for o in adapter.memory.store.values()}
    assert "session.opened" not in stored_kinds
    assert len(adapter.memory.store) >= before  # principal.created retained


async def test_crash_between_confirm_and_cursor_deduplicates(
    client: httpx.AsyncClient, adapter, sync_engine
) -> None:
    """Deliver, then simulate the crash-before-cursor-commit by resetting the
    cursor and delivering again: the provider reports duplicates, exactly one
    logical observation exists, the cursor finally advances."""
    boot = await do_bootstrap(client)
    tenant_id = boot["tenant"]["id"]
    await create_task(client, boot["apiKey"]["key"], title="Ambiguous delivery")

    assert await adapter.deliver_once() > 0
    first_store = dict(adapter.memory.store)
    row = _cursor_row(sync_engine, tenant_id)
    assert row is not None

    # Crash amnesia: cursor back to origin, same provider state.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE event_consumer_cursors SET tx_id = 0, sequence = 0 "
                "WHERE name = :name AND tenant_id = :tenant"
            ),
            {"name": CONSUMER_NAME, "tenant": tenant_id},
        )
    assert await adapter.deliver_once() > 0  # re-delivery of the same batch

    assert adapter.memory.duplicates > 0, "re-delivery must be seen as duplicates"
    assert dict(adapter.memory.store) == first_store, "no second logical observation"
    row_after = _cursor_row(sync_engine, tenant_id)
    assert row_after is not None and (row_after.tx_id, row_after.sequence) == (
        row.tx_id,
        row.sequence,
    )


async def test_outage_holds_cursor_then_catches_up(
    client: httpx.AsyncClient, adapter, sync_engine
) -> None:
    boot = await do_bootstrap(client)
    tenant_id = boot["tenant"]["id"]
    await create_task(client, boot["apiKey"]["key"], title="During outage")

    adapter.memory.fail_mode = "down"
    assert await adapter.deliver_once() == 0
    row = _cursor_row(sync_engine, tenant_id)
    assert row is not None and (row.tx_id, row.sequence) == (0, 0)
    assert row.parked_at is not None
    assert adapter.memory.store == {}

    adapter.memory.fail_mode = None
    # Clear the backoff the way an operator would, then catch up.
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE event_consumer_cursors SET next_attempt_at = NULL"))
    assert await adapter.deliver_once() > 0
    assert len(adapter.memory.store) >= 2
    row = _cursor_row(sync_engine, tenant_id)
    assert row is not None and row.sequence > 0 and row.parked_at is None


async def test_poison_observation_never_advances_cursor(
    client: httpx.AsyncClient, adapter, sync_engine
) -> None:
    """A permanent provider rejection parks the tenant with diagnostics; the
    cursor does not move and nothing is silently dropped."""
    boot = await do_bootstrap(client)
    tenant_id = boot["tenant"]["id"]
    await create_task(client, boot["apiKey"]["key"], title="Poison")

    adapter.memory.fail_mode = "reject"
    assert await adapter.deliver_once() == 0

    row = _cursor_row(sync_engine, tenant_id)
    assert row is not None
    assert (row.tx_id, row.sequence) == (0, 0), "cursor must not advance past a poison unit"
    assert row.failure_count >= 1
    assert row.parked_at is not None
    assert "invalid kind" in row.parked_reason
    # The operator is told exactly which event is stuck.
    assert row.parked_event_id is not None


async def test_poison_tenant_does_not_block_other_tenants(
    client: httpx.AsyncClient, adapter, sync_engine
) -> None:
    """Head-of-line blocking between tenants is gone (ADR-0036)."""
    boot = await do_bootstrap(client)
    tenant_a = boot["tenant"]["id"]
    tenant_b, _ = make_tenant_directly(sync_engine, "tenant-b")
    # Give tenant B a journal entry of its own.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id,"
                " correlation_id, request_id, payload, occurred_at)"
                " VALUES (:id, :t, 'principal.created', 'principal', :e, 'c', 'r',"
                ' \'{"kind": "agent", "displayName": "B"}\', now())'
            ),
            {"id": str(uuid.uuid4()), "t": tenant_b, "e": str(uuid.uuid4())},
        )

    adapter.memory.fail_mode = "reject"
    adapter.memory.fail_namespaces = {f"tenant:{tenant_a}"}
    # Drain rather than assume a single cycle serves both tenants: the point
    # under test is that A's poison never stops B, not the batching schedule.
    moved = 0
    for _ in range(5):
        step = await adapter.deliver_once()
        moved += step
        if step == 0:
            break

    assert moved > 0, "tenant B must still be delivered"
    row_a = _cursor_row(sync_engine, tenant_a)
    row_b = _cursor_row(sync_engine, tenant_b)
    assert row_a is not None and (row_a.tx_id, row_a.sequence) == (0, 0)
    assert row_a.parked_at is not None
    assert row_b is not None and row_b.sequence > 0
    assert row_b.parked_at is None
    assert {ns for (ns, _k) in adapter.memory.store} == {f"tenant:{tenant_b}"}


async def test_redrive_resumes_from_the_same_position(
    client: httpx.AsyncClient, adapter, sync_engine
) -> None:
    """After the cause is fixed, redrive retries the parked position exactly."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    tenant_id = boot["tenant"]["id"]
    await create_task(client, admin_key, title="Poison then fixed")

    adapter.memory.fail_mode = "reject"
    await adapter.deliver_once()
    parked = _cursor_row(sync_engine, tenant_id)
    assert parked is not None and parked.parked_at is not None

    status = (
        await client.get("/api/v1/operations/context-adapter", headers=auth(admin_key))
    ).json()
    assert status["parked"] is True
    assert status["parkedEventId"] == str(parked.parked_event_id)

    adapter.memory.fail_mode = None
    response = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={"reason": "mapping fixed"},
        headers=auth(admin_key),
    )
    assert response.status_code == 200, response.text
    assert response.json()["parked"] is False
    # Redrive does NOT move the cursor: the same events are retried.
    after = _cursor_row(sync_engine, tenant_id)
    assert (after.tx_id, after.sequence) == (parked.tx_id, parked.sequence)

    assert await adapter.deliver_once() > 0
    assert len(adapter.memory.store) >= 2
    # Repeating a redrive on a healthy row is a harmless no-op.
    again = await client.post(
        f"/api/v1/operations/context-adapter/{tenant_id}:redrive",
        json={},
        headers=auth(admin_key),
    )
    assert again.status_code == 200 and again.json()["parked"] is False


async def test_explicit_observation_flows_to_memory(client: httpx.AsyncClient, adapter) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    task = await create_task(client, boot["apiKey"]["key"], title="With finding")
    await client.post(
        "/api/v1/observations",
        json={"kind": "finding", "content": "Root cause: inversion", "task": task["id"]},
        headers=auth(agent_key),
    )
    await adapter.deliver_once()
    findings = [o for o in adapter.memory.store.values() if o["kind"] == "finding"]
    assert len(findings) == 1
    assert findings[0]["content"] == "Root cause: inversion"
    assert {"type": "task", "id": task["id"]} in findings[0]["scopes"]
