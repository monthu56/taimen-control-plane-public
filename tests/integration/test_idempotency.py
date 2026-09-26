import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.helpers import auth, do_bootstrap


async def test_idempotent_replay_returns_saved_result(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    key = str(uuid.uuid4())
    payload = {"title": "Once"}

    first = await client.post(
        "/api/v1/tasks",
        json=payload,
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert first.status_code == 201
    assert "idempotency-replayed" not in first.headers

    second = await client.post(
        "/api/v1/tasks",
        json=payload,
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert second.status_code == 201
    assert second.headers.get("idempotency-replayed") == "true"
    assert second.json() == first.json()

    with sync_engine.connect() as conn:
        task_count = conn.execute(text("SELECT count(*) FROM tasks")).scalar()
        record_count = conn.execute(text("SELECT count(*) FROM idempotency_keys")).scalar()
    assert task_count == 1
    assert record_count == 1


async def test_key_reuse_with_different_body_conflicts(
    client: httpx.AsyncClient,
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    key = str(uuid.uuid4())

    first = await client.post(
        "/api/v1/tasks",
        json={"title": "Original"},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert first.status_code == 201

    conflict = await client.post(
        "/api/v1/tasks",
        json={"title": "Different"},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_key_reused"


async def test_failed_execution_frees_the_key(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    key = str(uuid.uuid4())

    # Domain-invalid request fails and must not poison the key.
    bad = await client.post(
        "/api/v1/tasks",
        json={"title": "x", "priority": "bogus"},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert bad.status_code == 422

    good = await client.post(
        "/api/v1/tasks",
        json={"title": "x"},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert good.status_code == 201


async def test_key_is_scoped_per_endpoint(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    key = str(uuid.uuid4())

    task = await client.post(
        "/api/v1/tasks",
        json={"title": "T"},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert task.status_code == 201

    # Same key on a different path (different fingerprint) -> conflict, not replay.
    other = await client.post(
        "/api/v1/principals",
        json={"kind": "agent", "displayName": "A"},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "idempotency_key_reused"


async def test_replay_is_scoped_to_principal(client: httpx.AsyncClient) -> None:
    """Another principal reusing the same key must not receive the stored response."""
    from tests.helpers import create_agent_with_key

    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    _, agent_key = await create_agent_with_key(
        client, admin_key, permissions=["tasks.read", "tasks.write"]
    )
    key = str(uuid.uuid4())
    payload = {"title": "Mine"}

    first = await client.post(
        "/api/v1/tasks", json=payload, headers={**auth(admin_key), "Idempotency-Key": key}
    )
    assert first.status_code == 201

    other = await client.post(
        "/api/v1/tasks", json=payload, headers={**auth(agent_key), "Idempotency-Key": key}
    )
    assert other.status_code == 409
    assert other.json()["error"]["code"] == "idempotency_key_reused"


async def test_api_key_secret_not_replayed(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    """The one-time key secret is never stored: a replay returns key=null."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    principal_id = body["adminPrincipal"]["id"]
    key = str(uuid.uuid4())

    first = await client.post(
        f"/api/v1/principals/{principal_id}/api-keys",
        json={"permissions": ["tasks.read"]},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert first.status_code == 201
    full_key = first.json()["key"]
    assert full_key.startswith("cp_")

    replay = await client.post(
        f"/api/v1/principals/{principal_id}/api-keys",
        json={"permissions": ["tasks.read"]},
        headers={**auth(admin_key), "Idempotency-Key": key},
    )
    assert replay.status_code == 201
    assert replay.headers.get("idempotency-replayed") == "true"
    assert replay.json()["key"] is None
    assert replay.json()["id"] == first.json()["id"]

    # And the secret is not at rest in the idempotency store.
    with sync_engine.connect() as conn:
        stored = conn.execute(text("SELECT response_body::text FROM idempotency_keys")).scalar()
    assert full_key not in (stored or "")
