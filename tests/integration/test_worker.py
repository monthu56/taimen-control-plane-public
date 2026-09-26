from collections.abc import AsyncIterator
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.common import utcnow
from control_plane.config import Settings
from control_plane.infrastructure.db.models import OutboxRecord
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    backdate_expiry,
    create_agent_with_key,
    create_task,
    do_bootstrap,
    open_session,
)


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def test_outbox_is_delivered(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    await create_task(client, body["apiKey"]["key"])

    stats = await worker.run_once()
    assert stats["outbox_delivered"] == 2  # tenant.bootstrapped + task.created

    with sync_engine.connect() as conn:
        undelivered = conn.execute(
            text("SELECT count(*) FROM outbox WHERE delivered_at IS NULL")
        ).scalar()
        locked_by = conn.execute(text("SELECT DISTINCT locked_by FROM outbox")).scalar()
    assert undelivered == 0
    assert locked_by == worker.name


async def test_outbox_retry_with_backoff_and_diagnostics(
    client: httpx.AsyncClient,
    worker: Worker,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = await do_bootstrap(client)

    async def failing_deliver(record: OutboxRecord) -> None:
        raise ConnectionError("downstream unavailable")

    monkeypatch.setattr(worker, "deliver", failing_deliver)
    stats = await worker.run_once()
    assert stats["outbox_delivered"] == 0

    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT attempt_count, last_error, available_at, delivered_at FROM outbox")
        ).one()
    assert row.attempt_count == 1
    assert "downstream unavailable" in row.last_error
    assert row.available_at > utcnow()  # backed off into the future
    assert row.delivered_at is None

    # Not picked up again before the backoff elapses.
    stats = await worker.run_once()
    assert stats["outbox_delivered"] == 0

    # After the backoff (simulated) and with delivery restored, it goes through.
    monkeypatch.undo()
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET available_at = now() - interval '1 hour'"))
    stats = await worker.run_once()
    assert stats["outbox_delivered"] == 1
    _ = body


async def test_outbox_dead_letter_after_max_attempts(
    client: httpx.AsyncClient,
    worker: Worker,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await do_bootstrap(client)

    async def failing_deliver(record: OutboxRecord) -> None:
        raise ConnectionError("still down")

    monkeypatch.setattr(worker, "deliver", failing_deliver)
    for _ in range(worker.settings.outbox_max_attempts):
        with sync_engine.begin() as conn:
            conn.execute(text("UPDATE outbox SET available_at = now() - interval '1 hour'"))
        await worker.run_once()

    with sync_engine.connect() as conn:
        row = conn.execute(text("SELECT attempt_count, last_error FROM outbox")).one()
    assert row.attempt_count == worker.settings.outbox_max_attempts
    assert "still down" in row.last_error

    # Exhausted records are left alone (diagnosable, not retried).
    monkeypatch.undo()
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET available_at = now() - interval '1 hour'"))
    stats = await worker.run_once()
    assert stats["outbox_delivered"] == 0


async def test_expired_claim_is_swept(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Claim expires while its session is still alive: claim sweep marks stale."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    task = await create_task(client, admin_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()

    backdate_expiry(sync_engine, "task_claims", claim["id"])

    stats = await worker.run_once()
    assert stats["claims_expired"] == 1

    claim_now = (await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(admin_key))).json()
    task_now = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert claim_now["status"] == "stale"
    assert task_now["activeClaimId"] is None
    assert task_now["status"] == "todo"

    events = (await client.get("/api/v1/events", headers=auth(admin_key))).json()["items"]
    assert "claim.expired" in [e["type"] for e in events]

    # Idempotent: a second sweep does nothing.
    stats = await worker.run_once()
    assert stats["claims_expired"] == 0


async def test_expired_session_sweep_releases_its_claims(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    """Session expires while its claim is still live: the sweep frees the task."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)
    task = await create_task(client, admin_key)
    claim = (
        await client.post(
            f"/api/v1/tasks/{task['id']}:claim",
            json={"sessionId": session["id"]},
            headers=auth(agent_key),
        )
    ).json()

    backdate_expiry(sync_engine, "sessions", session["id"])

    stats = await worker.run_once()
    assert stats["sessions_expired"] == 1

    session_now = (
        await client.get(f"/api/v1/sessions/{session['id']}", headers=auth(admin_key))
    ).json()
    claim_now = (await client.get(f"/api/v1/claims/{claim['id']}", headers=auth(admin_key))).json()
    task_now = (await client.get(f"/api/v1/tasks/{task['id']}", headers=auth(admin_key))).json()
    assert session_now["status"] == "stale"
    assert claim_now["status"] == "released"
    assert claim_now["releaseReason"] == "session_expired"
    assert task_now["activeClaimId"] is None
    assert task_now["status"] == "todo"

    events = (await client.get("/api/v1/events", headers=auth(admin_key))).json()["items"]
    types = [e["type"] for e in events]
    assert "session.expired" in types
    assert "claim.released" in types

    stats = await worker.run_once()
    assert stats["sessions_expired"] == 0
    assert stats["claims_expired"] == 0


async def test_idempotency_records_are_cleaned(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    response = await client.post(
        "/api/v1/tasks",
        json={"title": "T"},
        headers={**auth(admin_key), "Idempotency-Key": "cleanup-me"},
    )
    assert response.status_code == 201

    past = utcnow() - timedelta(seconds=1)
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE idempotency_keys SET expires_at = :past"), {"past": past})

    stats = await worker.run_once()
    assert stats["idempotency_cleaned"] == 1
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar() == 0
