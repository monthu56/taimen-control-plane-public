"""v0.4 replay correctness under concurrent transactions.

The public guarantee under test: a reader that only advances its cursor along
delivered positions can never permanently skip a committed event, whatever
the interleaving of transaction starts, event inserts and commits.

Writers here are raw sync connections whose xid assignment, insert order and
commit order are controlled explicitly; the reader is the real HTTP API.
"""

import random

import httpx
from sqlalchemy import text

from tests.helpers import auth, create_agent_with_key, do_bootstrap


def _event_sql(tenant_id: str, name: str) -> str:
    return (
        "INSERT INTO events (id, tenant_id, event_type, entity_type, entity_id, "
        f"correlation_id, request_id, payload, occurred_at) VALUES (gen_random_uuid(), "
        f"'{tenant_id}', '{name}', 'test', gen_random_uuid(), 'c', 'r', '{{}}', now()) "
        "RETURNING sequence"
    )


async def _drain(
    client: httpx.AsyncClient,
    key: str,
    cursor: str | None,
    *,
    limit: int = 200,
) -> tuple[list[dict], str]:
    """Page through everything currently stable; return (events, cursor)."""
    collected: list[dict] = []
    while True:
        params: dict = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        body = (await client.get("/api/v1/events", params=params, headers=auth(key))).json()
        collected.extend(body["items"])
        cursor = body["nextCursor"]
        if not body["hasMore"]:
            return collected, cursor


async def test_commit_inversion_replay_complete(client: httpx.AsyncClient, sync_engine) -> None:
    """A inserts before B; B commits before A. Replay is complete."""
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]

    with sync_engine.connect() as a_conn, sync_engine.connect() as b_conn:
        a_seq = a_conn.execute(text(_event_sql(tenant_id, "A.event"))).scalar_one()
        b_seq = b_conn.execute(text(_event_sql(tenant_id, "B.event"))).scalar_one()
        b_conn.commit()  # B (newer xid) commits first

        # While A (older xid) is open it pins the horizon: B is not stable
        # yet, so the reader cannot advance past the hole A would leave.
        events, cursor = await _drain(client, agent_key, None)
        seen = {e["sequence"] for e in events}
        assert a_seq not in seen and b_seq not in seen

        a_conn.commit()

    events, _ = await _drain(client, agent_key, cursor)
    suffix = {e["sequence"] for e in events}
    assert {a_seq, b_seq} <= suffix, "commit inversion lost a committed event"


async def test_many_concurrent_writers_no_event_lost(
    client: httpx.AsyncClient, sync_engine
) -> None:
    """20 interleaved transactions: shuffled insert order, shuffled commit
    order, a reader paginating with a small page size between every commit.
    At the end: set(replayed) == set(committed), each exactly once."""
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]

    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    rng = random.Random(42)
    n = 20
    # The shared fixture pool tops out below 20 concurrent connections;
    # writers get a dedicated unpooled engine.
    writer_engine = create_engine(sync_engine.url, poolclass=NullPool)
    conns = [writer_engine.connect() for _ in range(n)]
    try:
        # Pin xids in connection order (0..n-1).
        for conn in conns:
            conn.execute(text("SELECT pg_current_xact_id()"))

        # Insert events in a different (shuffled) order than the xids.
        insert_order = list(range(n))
        rng.shuffle(insert_order)
        sequences: dict[int, int] = {}
        for i in insert_order:
            sequences[i] = conns[i].execute(text(_event_sql(tenant_id, f"w{i}.event"))).scalar_one()

        # Commit in yet another shuffled order, paginating in between with a
        # deliberately tiny page size.
        commit_order = list(range(n))
        rng.shuffle(commit_order)
        collected: list[dict] = []
        cursor: str | None = None
        for i in commit_order:
            conns[i].commit()
            events, cursor = await _drain(client, agent_key, cursor, limit=3)
            collected.extend(events)

        events, _ = await _drain(client, agent_key, cursor, limit=3)
        collected.extend(events)
    finally:
        for conn in conns:
            conn.close()
        writer_engine.dispose()

    replayed = [e["sequence"] for e in collected if e["type"].startswith("w")]
    committed = sorted(sequences.values())
    assert sorted(replayed) == committed, (
        f"replay mismatch: missing={set(committed) - set(replayed)}, "
        f"duplicated={[s for s in replayed if replayed.count(s) > 1]}"
    )


async def test_pagination_under_concurrent_commits_never_skips(
    client: httpx.AsyncClient, sync_engine
) -> None:
    """Small pages while new transactions commit mid-pagination: the pages
    concatenate into the complete committed set."""
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]

    committed: set[int] = set()
    with sync_engine.connect() as conn:
        for i in range(6):
            committed.add(conn.execute(text(_event_sql(tenant_id, f"p{i}.event"))).scalar_one())
            conn.commit()

    collected: list[dict] = []
    cursor: str | None = None
    for i in range(6, 12):
        params: dict = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = (await client.get("/api/v1/events", params=params, headers=auth(agent_key))).json()
        collected.extend(body["items"])
        cursor = body["nextCursor"]
        # New commits land while we are mid-pagination.
        with sync_engine.connect() as conn:
            committed.add(conn.execute(text(_event_sql(tenant_id, f"p{i}.event"))).scalar_one())
            conn.commit()

    events, _ = await _drain(client, agent_key, cursor, limit=2)
    collected.extend(events)

    replayed = {e["sequence"] for e in collected if e["type"].startswith("p")}
    assert committed <= replayed, f"pagination skipped {committed - replayed}"
    all_sequences = [e["sequence"] for e in collected]
    assert len(all_sequences) == len(set(all_sequences)), "duplicate delivery within one replay"


async def test_long_open_transaction_delays_but_never_loses(
    client: httpx.AsyncClient, sync_engine
) -> None:
    """One long-open writing transaction pins the horizon: later commits are
    DELAYED (documented semantics), then delivered completely when it ends."""
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]

    with sync_engine.connect() as long_conn:
        long_seq = long_conn.execute(text(_event_sql(tenant_id, "long.event"))).scalar_one()

        later: set[int] = set()
        with sync_engine.connect() as conn:
            for i in range(3):
                later.add(conn.execute(text(_event_sql(tenant_id, f"later{i}.event"))).scalar_one())
                conn.commit()

        # Reader keeps working; the tail is simply not stable yet.
        events, cursor = await _drain(client, agent_key, None)
        held = {e["sequence"] for e in events}
        assert not (later & held) and long_seq not in held

        long_conn.commit()

    events, _ = await _drain(client, agent_key, cursor)
    suffix = {e["sequence"] for e in events}
    assert later | {long_seq} <= suffix, "events lost after the long transaction finished"


async def test_legacy_integer_after_is_adapted(client: httpx.AsyncClient, sync_engine) -> None:
    """v0.3 ``after=<sequence>`` keeps working: the first page filters by
    sequence, the returned nextCursor is already a safe opaque position."""
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]

    with sync_engine.connect() as conn:
        first = conn.execute(text(_event_sql(tenant_id, "legacy.one"))).scalar_one()
        second = conn.execute(text(_event_sql(tenant_id, "legacy.two"))).scalar_one()
        conn.commit()

    body = (
        await client.get(
            "/api/v1/events", params={"after": first, "limit": 200}, headers=auth(agent_key)
        )
    ).json()
    sequences = [e["sequence"] for e in body["items"]]
    assert second in sequences and first not in sequences
    assert body["nextCursor"].startswith("ec1_")

    # Empty page under a legacy floor: nextCursor echoes a resumable cursor.
    empty = (
        await client.get(
            "/api/v1/events", params={"after": second, "limit": 200}, headers=auth(agent_key)
        )
    ).json()
    assert empty["items"] == []
    assert empty["hasMore"] is False
    assert empty["nextCursor"].startswith("ec1_")


async def test_legacy_v03_next_cursor_encoding_is_accepted(
    client: httpx.AsyncClient, sync_engine
) -> None:
    """A stored v0.3 nextCursor (base64url of {"s": N}) still resumes."""
    import base64
    import json as jsonlib

    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]

    with sync_engine.connect() as conn:
        first = conn.execute(text(_event_sql(tenant_id, "old.one"))).scalar_one()
        second = conn.execute(text(_event_sql(tenant_id, "old.two"))).scalar_one()
        conn.commit()

    legacy = base64.urlsafe_b64encode(jsonlib.dumps({"s": first}).encode()).decode().rstrip("=")
    body = (
        await client.get(
            "/api/v1/events", params={"cursor": legacy, "limit": 200}, headers=auth(agent_key)
        )
    ).json()
    sequences = [e["sequence"] for e in body["items"]]
    assert second in sequences and first not in sequences


async def test_malformed_and_unsupported_cursors_are_rejected(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])

    async def get_error(cursor: str) -> tuple[int, str]:
        response = await client.get(
            "/api/v1/events", params={"cursor": cursor}, headers=auth(agent_key)
        )
        return response.status_code, response.json()["error"]["code"]

    # Structurally broken payloads.
    for bad in ("ec1_garbage!", "ec1_", "not-base64-!!", "ec1_e30"):  # e30 = {}
        status, code = await get_error(bad)
        assert (status, code) == (422, "invalid_cursor"), (bad, status, code)

    # Tampered payload with the right shape but wrong types.
    import base64

    tampered = "ec1_" + base64.urlsafe_b64encode(b'{"t":"x","s":1}').decode().rstrip("=")
    assert await get_error(tampered) == (422, "invalid_cursor")

    # A future cursor version is refused explicitly, not misparsed.
    status, code = await get_error("ec2_AAAA")
    assert (status, code) == (422, "unsupported_cursor_version")


async def test_ws_reconnect_inside_inversion_window(app, sync_engine) -> None:
    """Disconnect exactly inside the inversion window: the WS client's last
    cursor sits past R (older xid, higher sequence) while E (newer xid, lower
    sequence) is still pending. Reconnecting with that cursor must deliver E
    once it commits."""
    from fastapi.testclient import TestClient

    from tests.helpers import BOOTSTRAP_TOKEN

    tc = TestClient(app)
    with tc:
        boot = tc.post(
            "/api/v1/bootstrap",
            json={"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "A"},
            headers=auth(BOOTSTRAP_TOKEN),
        ).json()
        admin_key = boot["apiKey"]["key"]
        tenant_id = boot["tenant"]["id"]

        with sync_engine.connect() as r_conn, sync_engine.connect() as e_conn:
            r_conn.execute(text("SELECT pg_current_xact_id()"))
            e_conn.execute(text("SELECT pg_current_xact_id()"))
            e_seq = e_conn.execute(text(_event_sql(tenant_id, "E.event"))).scalar_one()
            r_seq = r_conn.execute(text(_event_sql(tenant_id, "R.event"))).scalar_one()
            r_conn.commit()  # R stable; E pending with a LOWER sequence

            cursor = None
            with tc.websocket_connect("/api/v1/events/ws", headers=auth(admin_key)) as ws:
                while True:
                    event = ws.receive_json()
                    cursor = event["cursor"]
                    if event["sequence"] == r_seq:
                        break  # disconnect exactly past R, inside the window

            e_conn.commit()

        assert cursor is not None
        with tc.websocket_connect(
            f"/api/v1/events/ws?after={cursor}", headers=auth(admin_key)
        ) as ws:
            recovered = ws.receive_json()
            assert recovered["sequence"] == e_seq, "reconnect lost the inverted event"


async def test_new_visibility_is_strictly_after_any_delivered_position(
    client: httpx.AsyncClient, sync_engine
) -> None:
    """Whatever becomes stable later always sorts after every position a
    reader could already hold (tx_id >= old xmin > delivered tx_id)."""
    boot = await do_bootstrap(client)
    _, agent_key = await create_agent_with_key(client, boot["apiKey"]["key"])
    tenant_id = boot["tenant"]["id"]

    with sync_engine.connect() as conn:
        conn.execute(text(_event_sql(tenant_id, "base.event")))
        conn.commit()

    _, cursor = await _drain(client, agent_key, None)

    seen_after: list[int] = []
    for i in range(5):
        with sync_engine.connect() as conn:
            conn.execute(text(_event_sql(tenant_id, f"later{i}.event")))
            conn.commit()
        events, cursor = await _drain(client, agent_key, cursor)
        seen_after.extend(e["sequence"] for e in events)

    assert len(seen_after) == 5
    assert seen_after == sorted(seen_after)
