"""EventConsumer SDK against a real test Control Plane (CP-ADR-0069).

The acceptance of N003: a consumer that dies in the middle of a page resumes
after the last handled event — nothing lost, nothing handled twice.
"""

import asyncio
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import Column, Integer, MetaData, String, Table, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from control_plane_client import ControlPlaneClient, NotFoundError
from control_plane_client.events import (
    Event,
    EventConsumer,
    MemoryCursorStore,
    to_cloudevent,
)
from control_plane_client.events.sqlalchemy import SqlAlchemyCursorStore, cursor_tables
from tests.helpers import create_task, create_workspace, do_bootstrap

Make = Callable[[str], ControlPlaneClient]


class _Crash(BaseException):
    """A process death: not an error the consumer could catch and retry."""


def _effects_table(metadata: MetaData) -> Table:
    # The handler's own table: a side effect committed with the dedup record.
    return Table(
        "sdk_test_effects",
        metadata,
        Column("n", Integer, primary_key=True, autoincrement=True),
        Column("event_id", String(64), nullable=False),
        Column("title", String(200), nullable=False),
    )


@pytest.fixture
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sql_tables(engine: AsyncEngine) -> AsyncIterator[MetaData]:
    """The store's tables and the effects table, created and dropped per test."""
    metadata = MetaData()
    cursor_tables(metadata, prefix="sdk_test_")
    _effects_table(metadata)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield metadata
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)


def _store(engine: AsyncEngine) -> tuple[SqlAlchemyCursorStore, Table]:
    """What a (re)started process builds: fresh metadata, same tables."""
    metadata = MetaData()
    store = SqlAlchemyCursorStore(engine, metadata=metadata, prefix="sdk_test_")
    return store, _effects_table(metadata)


async def _seed(client: httpx.AsyncClient) -> tuple[str, str]:
    """Admin key and a workspace with six tasks, plus noise elsewhere."""
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    ops = await create_workspace(client, admin_key, "ops")
    sales = await create_workspace(client, admin_key, "sales")
    for i in range(1, 7):
        await create_task(client, admin_key, title=f"ops-{i}", workspaceId=ops["id"])
        await create_task(client, admin_key, title=f"sales-{i}", workspaceId=sales["id"])
    return admin_key, ops["id"]


async def test_restart_mid_page_loses_and_repeats_nothing(
    client: httpx.AsyncClient, sdk: Make, sql_tables: MetaData, migrated_database: str
) -> None:
    admin_key, ops_id = await _seed(client)
    calls: list[str] = []

    first_engine = create_async_engine(migrated_database)
    store, effects = _store(first_engine)

    async def dying(event: Event) -> None:
        calls.append(event["payload"]["title"])
        await store.session().execute(
            insert(effects).values(event_id=event["id"], title=event["payload"]["title"])
        )
        if len(calls) == 3:
            raise _Crash  # after the handler's own write, before the commit

    async with sdk(admin_key) as api:
        consumer = EventConsumer(
            api, ["task.created"], ops_id, store, dying, name="notify", page_size=4
        )
        with pytest.raises(_Crash):
            await consumer.drain()
    await first_engine.dispose()
    assert calls == ["ops-1", "ops-2", "ops-3"]

    # The restarted process: a new client, a new engine, the same database.
    second_engine = create_async_engine(migrated_database)
    store, effects = _store(second_engine)

    async def handler(event: Event) -> None:
        calls.append(event["payload"]["title"])
        await store.session().execute(
            insert(effects).values(event_id=event["id"], title=event["payload"]["title"])
        )

    async with sdk(admin_key) as api:
        consumer = EventConsumer(api, ["task."], ops_id, store, handler, name="notify", page_size=4)
        assert await consumer.drain() == 4
        assert await consumer.drain() == 0

    async with second_engine.connect() as conn:
        titles = (await conn.execute(select(effects.c.title).order_by(effects.c.n))).scalars()
        assert list(titles) == [f"ops-{i}" for i in range(1, 7)]
    await second_engine.dispose()
    # ops-3 ran twice, but its first run died uncommitted and left no effect.
    assert calls == ["ops-1", "ops-2", "ops-3", "ops-3", "ops-4", "ops-5", "ops-6"]


async def test_redelivered_events_are_skipped_by_id(
    client: httpx.AsyncClient, sdk: Make, sql_tables: MetaData, engine: AsyncEngine
) -> None:
    """A cursor behind the handled events (a retried page, a server that errs
    toward re-delivery) costs a read, not a second effect."""
    admin_key, ops_id = await _seed(client)
    store, _ = _store(engine)
    cursors: list[str] = []
    handled: list[str] = []

    async def handler(event: Event) -> None:
        cursors.append(event["cursor"])
        handled.append(event["id"])

    async with sdk(admin_key) as api:
        consumer = EventConsumer(api, ["task."], ops_id, store, handler, name="notify")
        assert await consumer.drain() == 6
        await store.advance("notify", cursors[1])
        assert await consumer.drain() == 0
        assert await store.load("notify") != cursors[1]
    assert len(handled) == len(set(handled)) == 6


class _FailingTransport(httpx.AsyncBaseTransport):
    """Answers the first ``failures`` reads of /events with a 503."""

    def __init__(self, inner: httpx.AsyncBaseTransport, failures: int) -> None:
        self.inner = inner
        self.failures = failures

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events") and self.failures:
            self.failures -= 1
            return httpx.Response(503, json={"error": {"code": "unavailable", "message": "x"}})
        return await self.inner.handle_async_request(request)


async def test_failed_page_and_failed_handler_are_retried(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    admin_key, ops_id = await _seed(client)
    transport = _FailingTransport(httpx.ASGITransport(app=app), failures=2)
    store = MemoryCursorStore()
    attempts: list[str] = []
    done: list[str] = []
    consumer: EventConsumer

    async def handler(event: Event) -> None:
        title = event["payload"]["title"]
        attempts.append(title)
        if title == "ops-2" and attempts.count(title) == 1:
            raise RuntimeError("downstream unavailable")
        done.append(title)
        if len(done) == 6:
            consumer.stop()

    async with ControlPlaneClient("http://testserver", admin_key, transport=transport) as api:
        consumer = EventConsumer(
            api,
            ["task."],
            ops_id,
            store,
            handler,
            name="n",
            websocket=False,
            retry_initial=0.01,
            page_size=2,
        )
        await asyncio.wait_for(consumer.run(), 10)
    assert transport.failures == 0
    assert done == [f"ops-{i}" for i in range(1, 7)]
    assert attempts == ["ops-1", "ops-2", "ops-2", "ops-3", "ops-4", "ops-5", "ops-6"]


async def test_refused_subscription_stops_the_consumer(
    client: httpx.AsyncClient, sdk: Make
) -> None:
    boot = await do_bootstrap(client)

    async def handler(event: Event) -> None:
        raise AssertionError("no events expected")

    async with sdk(boot["apiKey"]["key"]) as api:
        consumer = EventConsumer(
            api,
            ["task."],
            "00000000-0000-0000-0000-000000000000",
            MemoryCursorStore(),
            handler,
            name="n",
            websocket=False,
        )
        with pytest.raises(NotFoundError):
            await asyncio.wait_for(consumer.run(), 10)


async def test_start_latest_skips_history(client: httpx.AsyncClient, sdk: Make) -> None:
    admin_key, ops_id = await _seed(client)
    store = MemoryCursorStore()
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append(event["payload"]["title"])

    async with sdk(admin_key) as api:
        consumer = EventConsumer(api, ["task."], ops_id, store, handler, name="n", start="latest")
        assert await consumer.drain() == 0
        await create_task(client, admin_key, title="ops-new", workspaceId=ops_id)
        assert await consumer.drain() == 1
    assert seen == ["ops-new"]


@pytest.mark.timeout(60)
async def test_websocket_wakes_the_consumer_before_the_poll(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    """With a 30 s poll, an event arriving within seconds means the socket woke it."""
    admin_key, ops_id = await _seed(client)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    try:
        for _ in range(500):  # uvicorn exposes no "started" event
            if server.started:
                break
            await asyncio.sleep(0.01)
        port = server.servers[0].sockets[0].getsockname()[1]
        caught_up, arrived = asyncio.Event(), asyncio.Event()
        seen: list[str] = []

        async def handler(event: Event) -> None:
            seen.append(event["payload"]["title"])
            if len(seen) == 6:
                caught_up.set()
            if event["payload"]["title"] == "ops-live":
                arrived.set()

        async with ControlPlaneClient(f"http://127.0.0.1:{port}", admin_key) as api:
            consumer = EventConsumer(
                api, ["task."], ops_id, MemoryCursorStore(), handler, name="n", poll_interval=30
            )
            running = asyncio.create_task(consumer.run())
            await asyncio.wait_for(caught_up.wait(), 10)
            await asyncio.sleep(1)  # the initial read is done; the consumer sleeps
            await create_task(client, admin_key, title="ops-live", workspaceId=ops_id)
            await asyncio.wait_for(arrived.wait(), 10)
            consumer.stop()
            await asyncio.wait_for(running, 10)
        assert seen == [*(f"ops-{i}" for i in range(1, 7)), "ops-live"]
    finally:
        server.should_exit = True
        await serving


async def test_cloudevent_export(client: httpx.AsyncClient, sdk: Make) -> None:
    admin_key, ops_id = await _seed(client)
    async with sdk(admin_key) as api:
        event = (await api.list_events(types=["task."], workspace_id=ops_id, limit=1))["items"][0]
    cloudevent = to_cloudevent(event, source="https://cp.example.com")
    assert cloudevent == {
        "specversion": "1.0",
        "id": event["id"],
        "source": "https://cp.example.com",
        "type": "task.created",
        "time": event["occurredAt"],
        "subject": f"task/{event['entityId']}",
        "datacontenttype": "application/json",
        "tenantid": event["tenantId"],
        "workspaceid": ops_id,
        "entitytype": "task",
        "entityid": event["entityId"],
        "schemaversion": 1,
        "actorid": event["actorId"],
        "correlationid": event["correlationId"],
        "data": event["payload"],
    }
    assert to_cloudevent(event)["source"] == f"/control-plane/tenants/{event['tenantId']}"
