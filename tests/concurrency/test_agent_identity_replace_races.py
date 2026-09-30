"""Parallel ``identity:replace`` of one service agent (CP-ADR-0073, amendment 2026-09-30).

The agent row serializes the calls: each one sees the identity the previous
one left, so the registry ends on exactly one identity and that identity is
the only active binding of the principal — no call reopens a binding another
one revoked, and none deadlocks with the others. Two agents racing for one
new identity meet on ``uq_iam_bindings_identity``: the loser gets the 409 it
would get a moment later, not a 500.
"""

import asyncio
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from tests.concurrency.test_principal_disable_races import _wait_until_blocked_by
from tests.helpers import auth, do_bootstrap

ISSUER = "https://iam.example.test"
SPEC = {
    "displayName": "Notifier",
    "identity": {"kind": "service", "permissions": ["tasks.read"]},
    "placement": "none",
}


async def test_parallel_replacements_leave_one_identity_active(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    published = await client.post(
        "/api/v1/agents", json={"key": "notifier", "spec": SPEC}, headers=auth(admin_key)
    )
    assert published.status_code == 201, published.text
    iam_tenant = str(uuid.uuid4())
    linked = await client.put(
        "/api/v1/agents/notifier/identity",
        json={"issuer": ISSUER, "iamTenantId": iam_tenant, "iamPrincipalId": str(uuid.uuid4())},
        headers=auth(admin_key),
    )
    assert linked.status_code == 200, linked.text
    principal_id = linked.json()["principalId"]

    async def replace(identity: str) -> httpx.Response:
        return await client.post(
            "/api/v1/agents/notifier/identity:replace",
            json={
                "issuer": ISSUER,
                "iamTenantId": iam_tenant,
                "iamPrincipalId": identity,
                "reason": "re-created",
            },
            headers=auth(admin_key),
        )

    identities = [str(uuid.uuid4()) for _ in range(4)]
    responses = await asyncio.wait_for(asyncio.gather(*(replace(i) for i in identities)), 30)
    assert [r.status_code for r in responses] == [200] * 4, [r.text for r in responses]

    with sync_engine.connect() as conn:
        registry = conn.execute(
            text("SELECT iam_principal_id FROM agents WHERE key = 'notifier'")
        ).scalar_one()
        active = conn.execute(
            text(
                "SELECT iam_principal_id FROM iam_principal_bindings "
                "WHERE principal_id = :p AND status = 'active'"
            ),
            {"p": principal_id},
        ).all()
        replaced = conn.execute(
            text("SELECT count(*) FROM events WHERE event_type = 'agent.identity_replaced'")
        ).scalar_one()
    assert [str(row[0]) for row in active] == [str(registry)]
    assert str(registry) in identities
    assert replaced == 4


# --- two agents, one new identity (TASK-001120) ----------------------------------


async def _linked_service(client: httpx.AsyncClient, admin_key: str, key: str) -> str:
    published = await client.post(
        "/api/v1/agents", json={"key": key, "spec": SPEC}, headers=auth(admin_key)
    )
    assert published.status_code == 201, published.text
    linked = await client.put(
        f"/api/v1/agents/{key}/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(admin_key),
    )
    assert linked.status_code == 200, linked.text
    principal_id: str = linked.json()["principalId"]
    return principal_id


def _replace_body(identity: str) -> dict[str, str]:
    return {
        "issuer": ISSUER,
        "iamTenantId": str(uuid.uuid4()),
        "iamPrincipalId": identity,
        "reason": "re-created",
    }


@contextmanager
def _identity_being_bound(
    sync_engine: Engine, principal_id: str, identity: str
) -> Iterator[tuple[int, Connection]]:
    """Another transaction inserts a binding of ``identity`` and has not committed yet."""
    with sync_engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO iam_principal_bindings (id, tenant_id, principal_id, issuer, "
                "iam_tenant_id, iam_principal_id, permissions, status, created_at, updated_at) "
                "SELECT gen_random_uuid(), tenant_id, id, :issuer, gen_random_uuid(), :identity, "
                "'[\"tasks.read\"]', 'active', now(), now() FROM principals WHERE id = :p"
            ),
            {"issuer": ISSUER, "identity": identity, "p": principal_id},
        )
        pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        try:
            yield pid, conn
        finally:
            conn.rollback()


async def test_replace_losing_the_unique_index_to_another_agent_is_a_conflict(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The lookup finds nothing, the insert waits on the winner and hits the index."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    notifier = await _linked_service(client, admin_key, "notifier")
    mailer = await _linked_service(client, admin_key, "mailer")
    contested = str(uuid.uuid4())

    with _identity_being_bound(sync_engine, mailer, contested) as (holder, conn):
        request = asyncio.create_task(
            client.post(
                "/api/v1/agents/notifier/identity:replace",
                json=_replace_body(contested),
                headers=auth(admin_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await asyncio.wait_for(request, 30)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "agent_identity_conflict"
    assert response.json()["error"]["details"] == {"agent": "notifier"}
    with sync_engine.connect() as conn:
        active = conn.execute(
            text(
                "SELECT count(*) FROM iam_principal_bindings "
                "WHERE principal_id = :p AND status = 'active'"
            ),
            {"p": notifier},
        ).scalar_one()
        replaced = conn.execute(
            text("SELECT count(*) FROM events WHERE event_type = 'agent.identity_replaced'")
        ).scalar_one()
    # The whole replacement rolled back: the previous binding still lets it in.
    assert (active, replaced) == (1, 0)


async def test_link_losing_the_unique_index_is_a_conflict(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    mailer = await _linked_service(client, admin_key, "mailer")
    published = await client.post(
        "/api/v1/agents", json={"key": "late", "spec": SPEC}, headers=auth(admin_key)
    )
    assert published.status_code == 201, published.text
    contested = str(uuid.uuid4())

    with _identity_being_bound(sync_engine, mailer, contested) as (holder, conn):
        request = asyncio.create_task(
            client.put(
                "/api/v1/agents/late/identity",
                json={
                    "issuer": ISSUER,
                    "iamTenantId": str(uuid.uuid4()),
                    "iamPrincipalId": contested,
                },
                headers=auth(admin_key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await asyncio.wait_for(request, 30)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "agent_identity_conflict"
    with sync_engine.connect() as conn:
        linked = conn.execute(
            text("SELECT principal_id FROM agents WHERE key = 'late'")
        ).scalar_one()
    assert linked is None


async def test_parallel_replacements_of_two_agents_onto_one_identity(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    for key in ("notifier", "mailer"):
        await _linked_service(client, admin_key, key)
    contested = str(uuid.uuid4())

    responses = await asyncio.wait_for(
        asyncio.gather(
            *(
                client.post(
                    f"/api/v1/agents/{key}/identity:replace",
                    json=_replace_body(contested),
                    headers=auth(admin_key),
                )
                for key in ("notifier", "mailer")
            )
        ),
        30,
    )
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    loser = next(r for r in responses if r.status_code == 409)
    assert loser.json()["error"]["code"] == "agent_identity_conflict"
    with sync_engine.connect() as conn:
        holders = conn.execute(
            text("SELECT count(*) FROM agents WHERE iam_principal_id = :i"), {"i": contested}
        ).scalar_one()
    assert holders == 1
