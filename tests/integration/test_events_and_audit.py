import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from tests.helpers import auth, create_task, do_bootstrap


async def test_every_mutation_writes_an_event_and_outbox(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    task = await create_task(client, admin_key)
    await client.patch(
        f"/api/v1/tasks/{task['id']}",
        json={"title": "Renamed"},
        headers={**auth(admin_key), "If-Match": '"task-1"'},
    )

    response = await client.get("/api/v1/events", headers=auth(admin_key))
    assert response.status_code == 200
    events = response.json()["items"]
    types = [e["type"] for e in events]
    assert types == ["tenant.bootstrapped", "task.created", "task.updated"]
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)
    assert all(e["requestId"] for e in events)

    with sync_engine.connect() as conn:
        outbox_count = conn.execute(text("SELECT count(*) FROM outbox")).scalar()
        event_count = conn.execute(text("SELECT count(*) FROM events")).scalar()
    assert event_count == 3
    assert outbox_count == 3


async def test_events_after_and_cursor(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    for i in range(3):
        await create_task(client, admin_key, title=f"T{i}")

    all_events = (await client.get("/api/v1/events", headers=auth(admin_key))).json()["items"]
    pivot = all_events[1]["sequence"]

    after = (await client.get(f"/api/v1/events?after={pivot}", headers=auth(admin_key))).json()[
        "items"
    ]
    assert [e["sequence"] for e in after] == [
        e["sequence"] for e in all_events if e["sequence"] > pivot
    ]

    paged = (await client.get("/api/v1/events?limit=2", headers=auth(admin_key))).json()
    assert len(paged["items"]) == 2
    rest = (
        await client.get(f"/api/v1/events?cursor={paged['nextCursor']}", headers=auth(admin_key))
    ).json()
    assert [e["sequence"] for e in paged["items"] + rest["items"]] == [
        e["sequence"] for e in all_events
    ]


async def test_events_require_permission(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]
    from tests.helpers import create_agent_with_key

    _, key = await create_agent_with_key(client, admin_key, permissions=["tasks.read"])
    assert (await client.get("/api/v1/events", headers=auth(key))).status_code == 403


async def test_events_are_immutable_in_db(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    await do_bootstrap(client)
    with sync_engine.connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("UPDATE events SET event_type = 'tampered'"))
    with sync_engine.connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("DELETE FROM events"))


async def test_transactional_audit_rollback(
    client: httpx.AsyncClient,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure inside the command transaction leaves no state, event or outbox."""
    body = await do_bootstrap(client)
    admin_key = body["apiKey"]["key"]

    import control_plane.application.commands.tasks as tasks_module

    original = tasks_module.record_event

    async def exploding_record_event(*args: object, **kwargs: object) -> object:
        # The task INSERT is already flushed at this point; the event write blows up.
        raise RuntimeError("simulated failure between state change and commit")

    monkeypatch.setattr(tasks_module, "record_event", exploding_record_event)
    # httpx's ASGITransport re-raises server exceptions instead of returning
    # the 500 response; the envelope itself is covered in test_internal_error.
    with pytest.raises(RuntimeError, match="simulated"):
        await client.post("/api/v1/tasks", json={"title": "Doomed"}, headers=auth(admin_key))

    monkeypatch.setattr(tasks_module, "record_event", original)
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks")).scalar() == 0
        assert (
            conn.execute(
                text("SELECT count(*) FROM events WHERE event_type = 'task.created'")
            ).scalar()
            == 0
        )
        assert (
            conn.execute(text("SELECT count(*) FROM outbox WHERE topic = 'task.created'")).scalar()
            == 0
        )

    # The same command succeeds afterwards, and the rolled-back attempt did not
    # consume a task number (the counter lives in the same transaction).
    response = await client.post(
        "/api/v1/tasks", json={"title": "Survivor"}, headers=auth(admin_key)
    )
    assert response.status_code == 201
    assert response.json()["publicId"] == "TASK-000001"


def test_internal_error_envelope_hides_details(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, clean_database: None
) -> None:
    """Unexpected errors return the envelope with no stack trace leaked."""
    from fastapi.testclient import TestClient

    import control_plane.application.commands.tasks as tasks_module
    from control_plane.main import create_app
    from tests.helpers import BOOTSTRAP_TOKEN

    async def exploding(*args: object, **kwargs: object) -> object:
        raise RuntimeError("secret internal detail")

    with TestClient(create_app(settings), raise_server_exceptions=False) as tc:
        response = tc.post(
            "/api/v1/bootstrap",
            json={"tenantSlug": "acme", "tenantName": "A", "adminDisplayName": "B"},
            headers=auth(BOOTSTRAP_TOKEN),
        )
        admin_key = response.json()["apiKey"]["key"]
        monkeypatch.setattr(tasks_module, "record_event", exploding)
        response = tc.post("/api/v1/tasks", json={"title": "Doomed"}, headers=auth(admin_key))
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in response.text


async def test_events_truncate_is_forbidden(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    await do_bootstrap(client)
    with sync_engine.connect() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute(text("TRUNCATE events CASCADE"))
