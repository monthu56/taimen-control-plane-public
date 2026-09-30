"""Reading the journal backward: ``before`` and ``prevCursor`` (CP-ADR-0024,
amendment 2026-09-29).

The console opens at the tail and scrolls back: ``tail`` hands out the
``prevCursor`` of its oldest event, ``before=<cursor>`` gives the page strictly
before it, and the walk reaches the start of the journal — through the archive
too — without a gap or a repeat, under the same filters and rights as a
forward read.
"""

import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.api.v1.router import api_v1_router
from control_plane.worker.context_adapter import CONSUMER_NAME
from tests.helpers import (
    auth,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)


async def _get(client: httpx.AsyncClient, key: str, **params: Any) -> httpx.Response:
    return await client.get("/api/v1/events", params=params, headers=auth(key))


async def _page(client: httpx.AsyncClient, key: str, **params: Any) -> dict[str, Any]:
    response = await _get(client, key, **params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _forward_ids(client: httpx.AsyncClient, key: str, **params: Any) -> list[str]:
    ids: list[str] = []
    cursor: str | None = None
    while True:
        query = dict(params, limit=200)
        if cursor:
            query["cursor"] = cursor
        page = await _page(client, key, **query)
        ids += [e["id"] for e in page["items"]]
        cursor = page["nextCursor"]
        if not page["hasMore"]:
            return ids


async def _backward_ids(
    client: httpx.AsyncClient, key: str, *, size: int, **params: Any
) -> tuple[list[str], int]:
    """Walk from ``tail`` back to the start; ids in delivery order, page count."""
    page = await _page(client, key, tail=size, **params)
    pages = [page["items"]]
    while page["prevCursor"] is not None:
        page = await _page(client, key, before=page["prevCursor"], limit=size, **params)
        assert 0 < len(page["items"]) <= size
        assert page["hasMore"] is (page["prevCursor"] is not None)
        pages.append(page["items"])
    ids = [e["id"] for items in reversed(pages) for e in items]
    return ids, len(pages)


async def test_walk_back_from_tail_reaches_the_start_without_gaps_or_repeats(
    client: httpx.AsyncClient,
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    for n in range(9):
        await create_task(client, key, title=f"t{n}")

    everything = await _forward_ids(client, key)
    assert len(everything) > 9

    for size in (1, 3, len(everything) - 1, len(everything), len(everything) + 5):
        walked, pages = await _backward_ids(client, key, size=size)
        assert walked == everything, size
        assert pages == -(-len(everything) // size)


async def test_page_boundary_is_exclusive_and_ordered(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    for n in range(5):
        await create_task(client, key, title=f"t{n}")
    everything = (await _page(client, key, limit=200))["items"]

    # The cursor of an event: the page before it ends with its predecessor.
    pivot = everything[4]
    page = await _page(client, key, before=pivot["cursor"], limit=2)
    assert [e["id"] for e in page["items"]] == [e["id"] for e in everything[2:4]]
    assert page["prevCursor"] == everything[2]["cursor"]
    assert page["hasMore"] is True
    # Reading forward from nextCursor reaches the pivot again: nothing between.
    assert page["nextCursor"] == everything[3]["cursor"]
    forward = await _page(client, key, cursor=page["nextCursor"], limit=1)
    assert forward["items"][0]["id"] == pivot["id"]

    # The first page back holds the very first events and says it is the start.
    first = await _page(client, key, before=everything[2]["cursor"], limit=2)
    assert [e["id"] for e in first["items"]] == [e["id"] for e in everything[:2]]
    assert first["prevCursor"] is None
    assert first["hasMore"] is False

    # Before the very first event there is nothing; the echo keeps the reader put.
    empty = await _page(client, key, before=everything[0]["cursor"])
    assert empty["items"] == []
    assert empty["prevCursor"] is None
    assert empty["nextCursor"] == everything[0]["cursor"]


async def test_order_desc_only_flips_the_page(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    for n in range(3):
        await create_task(client, key, title=f"t{n}")
    everything = (await _page(client, key, limit=200))["items"]
    before = everything[-1]["cursor"]

    asc = await _page(client, key, before=before, limit=3)
    desc = await _page(client, key, before=before, limit=3, order="desc")
    assert [e["id"] for e in desc["items"]] == [e["id"] for e in reversed(asc["items"])]
    assert (desc["prevCursor"], desc["nextCursor"], desc["hasMore"]) == (
        asc["prevCursor"],
        asc["nextCursor"],
        asc["hasMore"],
    )

    tail = await _page(client, key, tail=2, order="desc")
    assert [e["id"] for e in tail["items"]] == [e["id"] for e in reversed(everything[-2:])]

    bad = await _get(client, key, tail=2, order="newest")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_request"


async def test_forward_page_offers_the_way_back(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    for n in range(3):
        await create_task(client, key, title=f"t{n}")
    everything = (await _page(client, key, limit=200))["items"]

    middle = await _page(client, key, cursor=everything[1]["cursor"], limit=2)
    assert middle["prevCursor"] == everything[2]["cursor"]
    back = await _page(client, key, before=middle["prevCursor"], limit=200)
    assert [e["id"] for e in back["items"]] == [e["id"] for e in everything[:2]]

    idle = await _page(client, key, cursor=(await _page(client, key, tail=1))["nextCursor"])
    assert idle["items"] == []
    assert idle["prevCursor"] is None


async def test_empty_journal(client: httpx.AsyncClient, sync_engine: Engine) -> None:
    await do_bootstrap(client)
    _, key = make_tenant_directly(sync_engine, "silent")

    tail = await _page(client, key, tail=10)
    assert tail["items"] == []
    assert tail["prevCursor"] is None
    assert tail["hasMore"] is False
    assert tail["nextCursor"].startswith("ec1_")

    origin = "ec1_eyJzIjowLCJ0IjowfQ"  # {"t":0,"s":0}
    page = await _page(client, key, before=origin)
    assert page["items"] == []
    assert page["prevCursor"] is None
    assert page["hasMore"] is False


async def test_another_tenants_cursor_reads_only_the_callers_journal(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    await create_task(client, key, title="mine")
    foreign_cursor = (await _page(client, key, tail=1))["nextCursor"]

    _, other_key = make_tenant_directly(sync_engine, "other")
    page = await _page(client, other_key, before=foreign_cursor)
    assert page["items"] == []
    assert page["prevCursor"] is None


async def test_filters_narrow_the_backward_walk(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ops = await create_workspace(client, key, "ops")
    child = await create_workspace(client, key, "ops-child", parent_id=ops["id"])
    sales = await create_workspace(client, key, "sales")
    for n in range(4):
        await create_task(client, key, title=f"child-{n}", workspaceId=child["id"])
        await create_task(client, key, title=f"sales-{n}", workspaceId=sales["id"])
    await create_task(client, key, title="tenant-level")

    params = {"types": "task.created", "workspaceId": ops["id"]}
    walked, _ = await _backward_ids(client, key, size=3, **params)
    assert walked == await _forward_ids(client, key, **params)
    titles = [
        e["payload"]["title"] for e in (await _page(client, key, limit=200, **params))["items"]
    ]
    assert titles == [f"child-{n}" for n in range(4)]

    # A filtered page back from any event holds the matching events before it.
    everything = (await _page(client, key, limit=200))["items"]
    pivot = everything[-3]
    matching = [e["id"] for e in everything[:-3] if e["type"] == "task.created"]
    back = await _page(client, key, before=pivot["cursor"], limit=2, types="task.created")
    assert [e["id"] for e in back["items"]] == matching[-2:]
    assert back["prevCursor"] is not None


async def test_backward_read_keeps_the_rights_of_a_forward_read(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    ops = await create_workspace(client, key, "ops")
    await create_task(client, key, title="t", workspaceId=ops["id"])
    cursor = (await _page(client, key, tail=1))["nextCursor"]

    _, no_events = await create_agent_with_key(
        client, key, name="no-events", permissions=["tasks.read"]
    )
    assert (await _get(client, no_events, before=cursor)).status_code == 403
    assert (await _get(client, no_events, before=cursor, workspaceId=ops["id"])).status_code == 403

    # A workspace of another tenant does not exist for this caller.
    _, other_key = make_tenant_directly(sync_engine, "other")
    foreign = await _get(client, other_key, before=cursor, workspaceId=ops["id"])
    assert foreign.status_code == 404
    unknown = await _get(client, key, before=cursor, workspaceId=str(uuid.uuid4()))
    assert unknown.status_code == 404


async def test_before_does_not_combine_with_a_forward_read(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    cursor = (await _page(client, key, tail=1))["nextCursor"]

    for extra in ({"after": 0}, {"cursor": cursor}, {"tail": 5}):
        response = await _get(client, key, before=cursor, **extra)
        assert response.status_code == 422, extra
        error = response.json()["error"]
        assert error["code"] == "conflicting_cursors"
        assert error["details"]["parameters"] == ["before", *extra]

    for bad in ("not-a-cursor", "ec2_eyJ0IjoxfQ", "12", "ec1_eyJxIjozfQ"):  # last: {"q":3}
        response = await _get(client, key, before=bad)
        assert response.status_code == 422, bad
        assert response.json()["error"]["code"] in {"invalid_cursor", "unsupported_cursor_version"}


def _drain_outbox_and_confirm(sync_engine: Engine, tenant_id: str) -> None:
    """Let retention move everything: delivered outbox, consumer at the head."""
    with sync_engine.begin() as conn:
        conn.execute(text("UPDATE outbox SET delivered_at = now() WHERE delivered_at IS NULL"))
        latest = conn.execute(
            text("SELECT tx_id, sequence FROM events ORDER BY tx_id DESC, sequence DESC LIMIT 1")
        ).one()
        conn.execute(
            text(
                "INSERT INTO event_consumer_cursors (name, tenant_id, tx_id, sequence,"
                " updated_at, metadata) VALUES (:name, :tenant, :tx, :seq, now(), '{}')"
                " ON CONFLICT (name, tenant_id) DO UPDATE SET tx_id = :tx, sequence = :seq"
            ),
            {"name": CONSUMER_NAME, "tenant": tenant_id, "tx": latest[0], "seq": latest[1]},
        )


async def test_walk_back_crosses_into_the_archive_and_stops_at_the_prune_floor(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    for n in range(4):
        await create_task(client, key, title=f"old-{n}")
    _drain_outbox_and_confirm(sync_engine, boot["tenant"]["id"])
    archived = await client.post(
        "/api/v1/operations/journal:archive", json={"beforeSeconds": 0}, headers=auth(key)
    )
    assert archived.status_code == 200, archived.text
    for n in range(2):
        await create_task(client, key, title=f"new-{n}")

    everything = await _forward_ids(client, key)
    for size in (2, 5):
        walked, _ = await _backward_ids(client, key, size=size)
        assert walked == everything

    # After a prune the walk ends at the oldest retained event; a cursor at or
    # below the prune floor is refused like a forward read below it.
    oldest_archived = (await _page(client, key, limit=2))["items"][1]["cursor"]
    _drain_outbox_and_confirm(sync_engine, boot["tenant"]["id"])
    pruned = await client.post(
        "/api/v1/operations/journal:prune", json={"beforeSeconds": 0}, headers=auth(key)
    )
    assert pruned.status_code == 200, pruned.text
    assert pruned.json()["pruned"] > 0

    remaining = await _forward_ids(client, key)
    walked, _ = await _backward_ids(client, key, size=2)
    assert walked == remaining

    refused = await _get(client, key, before=oldest_archived)
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "cursor_below_journal_floor"
    assert error["details"]["floorCursor"].startswith("ec1_")


def test_openapi_declares_before_order_and_prev_cursor() -> None:
    app = FastAPI()
    app.include_router(api_v1_router)
    spec = app.openapi()
    operation = spec["paths"]["/api/v1/events"]["get"]
    parameters = {p["name"]: p for p in operation["parameters"]}
    assert parameters["before"]["in"] == "query"
    assert parameters["order"]["schema"]["enum"] == ["asc", "desc"]
    assert parameters["order"]["schema"]["default"] == "asc"
    page = spec["components"]["schemas"]["EventPageOut"]
    assert "prevCursor" in page["properties"]
    assert "prevCursor" in page["required"]
