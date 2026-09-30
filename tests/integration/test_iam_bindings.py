"""Management API over ``iam_principal_bindings`` (ADR-0053).

The binding is the IAM-era API key: the place where a federated identity gets
its Control Plane permissions. So the cases here mirror the API key ones —
no escalation, only an admin makes an admin — and add what is specific to a
binding: a non-human never holds a human-only right, the first admin can be
bound at bootstrap, and a revocation closes entry now, not after the cache.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from platform_auth.testing import SigningKey
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.infrastructure.auth.iam import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE
from control_plane.main import create_app
from tests.helpers import BOOTSTRAP_TOKEN, auth, create_agent_with_key, do_bootstrap
from tests.integration.test_iam_enforcement import ISSUER, enable_iam


@pytest.fixture
def signing_key() -> SigningKey:
    return SigningKey.generate()


@pytest.fixture
def iam_settings(migrated_database: str) -> Settings:
    return Settings(
        database_url=migrated_database,
        bootstrap_token=BOOTSTRAP_TOKEN,
        log_level="WARNING",
        session_ttl_seconds=60,
        claim_ttl_seconds=60,
        ws_poll_interval_seconds=0.5,
    )


@pytest.fixture
async def iam_app(iam_settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(iam_settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def iam_client(iam_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=iam_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def identity(iam_tenant: uuid.UUID | None = None) -> dict[str, Any]:
    return {
        "issuer": ISSUER,
        "iamTenantId": str(iam_tenant or uuid.uuid4()),
        "iamPrincipalId": str(uuid.uuid4()),
    }


async def create_principal(
    client: httpx.AsyncClient, key: str, *, kind: str, name: str = "p"
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/principals", json={"kind": kind, "displayName": name}, headers=auth(key)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def upsert(
    client: httpx.AsyncClient,
    key: str,
    principal_id: str,
    *,
    permissions: list[str],
    **spec: Any,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/principals/{principal_id}/iam-bindings",
        json={**spec, "permissions": permissions},
        headers=auth(key),
    )


# --- bootstrap ----------------------------------------------------------------


async def test_bootstrap_with_binding_admits_the_admin_by_iam_token(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """The chicken-and-egg of an IAM-only deployment: no SQL before first login."""
    enable_iam(iam_app, signing_key)
    iam_tenant, iam_principal = uuid.uuid4(), uuid.uuid4()

    response = await iam_client.post(
        "/api/v1/bootstrap",
        json={
            "tenantSlug": "acme",
            "tenantName": "Acme",
            "adminDisplayName": "Admin",
            "iamBinding": {
                "issuer": ISSUER,
                "iamTenantId": str(iam_tenant),
                "iamPrincipalId": str(iam_principal),
            },
        },
        headers=auth(BOOTSTRAP_TOKEN),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    binding = body["iamBinding"]
    assert binding["issuer"] == ISSUER
    assert binding["iamPrincipalId"] == str(iam_principal)
    assert binding["principalId"] == body["adminPrincipal"]["id"]
    assert binding["status"] == "active"
    assert "admin" in binding["permissions"]
    assert "tasks.write" in binding["permissions"]

    # A read+write token narrows the binding to its concrete permissions and
    # still lets the administrator do the everyday work ...
    token = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ, SCOPE_WRITE]
    )
    created = await iam_client.post(
        "/api/v1/tasks", json={"title": "first task"}, headers=auth(token)
    )
    assert created.status_code == 201, created.text

    # ... and an admin-scoped one makes them an administrator here.
    admin_token = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_ADMIN]
    )
    listed = await iam_client.get(
        f"/api/v1/principals/{body['adminPrincipal']['id']}/iam-bindings",
        headers=auth(admin_token),
    )
    assert listed.status_code == 200, listed.text
    assert [b["id"] for b in listed.json()["items"]] == [binding["id"]]


async def test_bootstrap_without_binding_reports_none(client: httpx.AsyncClient) -> None:
    body = await do_bootstrap(client)
    assert body["iamBinding"] is None


async def test_bootstrap_takes_the_installer_tenant_id(client: httpx.AsyncClient) -> None:
    """One tenant, one UUID across IAM and the Control Plane (superproject ADR-0030)."""
    wanted = "3f2b9a6e-1c4d-4e8f-9a0b-5c6d7e8f9a0b"
    response = await client.post(
        "/api/v1/bootstrap",
        json={
            "tenantSlug": "acme",
            "tenantName": "Acme",
            "adminDisplayName": "Admin",
            "tenantId": wanted,
        },
        headers=auth(BOOTSTRAP_TOKEN),
    )
    assert response.status_code == 201, response.text
    assert response.json()["tenant"]["id"] == wanted


# --- CRUD ---------------------------------------------------------------------


async def test_upsert_creates_then_repoints_the_same_identity(
    client: httpx.AsyncClient,
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    first = await create_principal(client, admin_key, kind="agent", name="first")
    second = await create_principal(client, admin_key, kind="agent", name="second")
    spec = identity()

    created = await upsert(
        client, admin_key, first["id"], permissions=["tasks.write", "tasks.read"], **spec
    )
    assert created.status_code == 201, created.text
    binding = created.json()
    assert binding["principalId"] == first["id"]
    assert binding["permissions"] == ["tasks.read", "tasks.write"]
    assert binding["status"] == "active"
    assert binding["revokedAt"] is None

    updated = await upsert(client, admin_key, second["id"], permissions=["tasks.read"], **spec)
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == binding["id"]
    assert updated.json()["principalId"] == second["id"]
    assert updated.json()["permissions"] == ["tasks.read"]

    # The identity moved: it is listed under the new principal only.
    for principal, expected in ((first, []), (second, [binding["id"]])):
        listed = await client.get(
            f"/api/v1/principals/{principal['id']}/iam-bindings", headers=auth(admin_key)
        )
        assert listed.status_code == 200
        assert [b["id"] for b in listed.json()["items"]] == expected


async def test_revoke_is_idempotent_and_upsert_readmits(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent = await create_principal(client, admin_key, kind="agent")
    spec = identity()
    binding = (
        await upsert(client, admin_key, agent["id"], permissions=["tasks.read"], **spec)
    ).json()

    revoked = await client.post(
        f"/api/v1/iam-bindings/{binding['id']}:revoke", headers=auth(admin_key)
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"
    assert revoked.json()["revokedAt"] is not None

    again = await client.post(
        f"/api/v1/iam-bindings/{binding['id']}:revoke", headers=auth(admin_key)
    )
    assert again.status_code == 200
    assert again.json()["revokedAt"] == revoked.json()["revokedAt"]

    # Revoked rows stay visible: the list says what was closed, not only what is open.
    listed = await client.get(
        f"/api/v1/principals/{agent['id']}/iam-bindings", headers=auth(admin_key)
    )
    assert [b["status"] for b in listed.json()["items"]] == ["revoked"]

    readmitted = await upsert(client, admin_key, agent["id"], permissions=["tasks.read"], **spec)
    assert readmitted.status_code == 200, readmitted.text
    assert readmitted.json()["id"] == binding["id"]
    assert readmitted.json()["status"] == "active"
    assert readmitted.json()["revokedAt"] is None

    missing = await client.post(
        f"/api/v1/iam-bindings/{uuid.uuid4()}:revoke", headers=auth(admin_key)
    )
    assert missing.status_code == 404


async def test_events_are_journaled_without_secrets(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent = await create_principal(client, admin_key, kind="agent")
    spec = identity()

    binding = (
        await upsert(client, admin_key, agent["id"], permissions=["tasks.read"], **spec)
    ).json()
    await upsert(client, admin_key, agent["id"], permissions=["tasks.read", "tasks.write"], **spec)
    await client.post(f"/api/v1/iam-bindings/{binding['id']}:revoke", headers=auth(admin_key))

    events = await client.get(
        "/api/v1/events",
        params={"entityType": "iam_binding", "entityId": binding["id"]},
        headers=auth(admin_key),
    )
    assert events.status_code == 200, events.text
    items = events.json()["items"]
    assert [e["type"] for e in items] == [
        "iam_binding.created",
        "iam_binding.updated",
        "iam_binding.revoked",
    ]
    for event in items:
        assert event["payload"]["principalId"] == agent["id"]
        assert event["payload"]["iamPrincipalId"] == spec["iamPrincipalId"]


# --- rules --------------------------------------------------------------------


async def test_binding_cannot_escalate_beyond_the_caller(client: httpx.AsyncClient) -> None:
    """Same rule, same errors as for API keys: grant only what you hold."""
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    principal, limited_key = await create_agent_with_key(
        client, admin_key, permissions=["principals.write", "principals.read", "tasks.read"]
    )

    escalated = await upsert(
        client,
        limited_key,
        principal["id"],
        permissions=["tasks.read", "tasks.claim"],
        **identity(),
    )
    assert escalated.status_code == 403
    assert escalated.json()["error"]["code"] == "permission_escalation"
    assert escalated.json()["error"]["details"]["missing"] == ["tasks.claim"]

    human = await create_principal(client, admin_key, kind="human")
    minted_admin = await upsert(
        client, limited_key, human["id"], permissions=["admin"], **identity()
    )
    assert minted_admin.status_code == 422
    assert minted_admin.json()["error"]["code"] == "invalid_permissions"

    within = await upsert(
        client, limited_key, principal["id"], permissions=["tasks.read"], **identity()
    )
    assert within.status_code == 201, within.text


@pytest.mark.parametrize("kind", ["agent", "service"])
@pytest.mark.parametrize("permission", ["admin", "approvals.decide"])
async def test_non_human_principal_never_holds_human_only_permissions(
    client: httpx.AsyncClient, kind: str, permission: str
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    principal = await create_principal(client, admin_key, kind=kind)

    response = await upsert(
        client, admin_key, principal["id"], permissions=["tasks.read", permission], **identity()
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "permissions_not_allowed_for_kind"
    assert error["details"] == {"kind": kind, "forbidden": [permission]}


async def test_human_principal_may_hold_the_deciding_permissions(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    human = await create_principal(client, admin_key, kind="human")

    response = await upsert(
        client, admin_key, human["id"], permissions=["admin", "approvals.decide"], **identity()
    )

    assert response.status_code == 201, response.text


async def test_unknown_permission_and_empty_list_are_rejected(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent = await create_principal(client, admin_key, kind="agent")

    unknown = await upsert(client, admin_key, agent["id"], permissions=["tasks.fly"], **identity())
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_permissions"
    assert unknown.json()["error"]["details"]["unknown"] == ["tasks.fly"]

    empty = await upsert(client, admin_key, agent["id"], permissions=[], **identity())
    assert empty.status_code == 400


async def test_identity_bound_in_another_tenant_is_not_repointed(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    from tests.helpers import make_tenant_directly

    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    agent = await create_principal(client, admin_key, kind="agent")
    spec = identity()
    assert (
        await upsert(client, admin_key, agent["id"], permissions=["tasks.read"], **spec)
    ).status_code == 201

    _, other_key = make_tenant_directly(sync_engine, "other")
    other_agent = await create_principal(client, other_key, kind="agent")
    response = await upsert(
        client, other_key, other_agent["id"], permissions=["tasks.read"], **spec
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "iam_identity_bound_elsewhere"


async def test_management_requires_principal_permissions(client: httpx.AsyncClient) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    principal, worker_key = await create_agent_with_key(
        client, admin_key, permissions=["tasks.read"]
    )

    listed = await client.get(
        f"/api/v1/principals/{principal['id']}/iam-bindings", headers=auth(worker_key)
    )
    assert listed.status_code == 403

    bound = await upsert(
        client, worker_key, principal["id"], permissions=["tasks.read"], **identity()
    )
    assert bound.status_code == 403
    assert bound.json()["error"]["code"] == "permission_denied"


# --- enforcement --------------------------------------------------------------


async def test_binding_made_through_the_api_admits_and_revocation_closes_entry(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    admin_key = (await do_bootstrap(iam_client))["apiKey"]["key"]
    agent = await create_principal(iam_client, admin_key, kind="agent")
    iam_tenant, iam_principal = uuid.uuid4(), uuid.uuid4()

    binding = (
        await upsert(
            iam_client,
            admin_key,
            agent["id"],
            permissions=["tasks.read", "tasks.write"],
            issuer=ISSUER,
            iamTenantId=str(iam_tenant),
            iamPrincipalId=str(iam_principal),
        )
    ).json()
    token = signing_key.issue(
        subject=iam_principal,
        tenant_id=iam_tenant,
        scopes=[SCOPE_READ, SCOPE_WRITE],
        ttl_seconds=3600,
    )
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 200

    revoked = await iam_client.post(
        f"/api/v1/iam-bindings/{binding['id']}:revoke", headers=auth(admin_key)
    )
    assert revoked.status_code == 200, revoked.text

    # Same still-valid token, revoked binding: entry closes now, not in an hour.
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 401


async def test_binding_changes_take_effect_despite_a_warm_cache(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """The API invalidates the enforcement cache for the identity it touched.

    Both directions matter: a negative answer cached before the binding
    existed must not keep the identity out, and a positive one must not keep
    a revoked identity in for the length of the TTL.
    """
    enable_iam(iam_app, signing_key, ttl_seconds=600.0)
    admin_key = (await do_bootstrap(iam_client))["apiKey"]["key"]
    agent = await create_principal(iam_client, admin_key, kind="agent")
    iam_tenant, iam_principal = uuid.uuid4(), uuid.uuid4()
    token = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ], ttl_seconds=3600
    )

    # Known to the cache as "no binding" ...
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 401

    binding = (
        await upsert(
            iam_client,
            admin_key,
            agent["id"],
            permissions=["tasks.read"],
            issuer=ISSUER,
            iamTenantId=str(iam_tenant),
            iamPrincipalId=str(iam_principal),
        )
    ).json()
    # ... admitted on the very next request.
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 200

    await iam_client.post(f"/api/v1/iam-bindings/{binding['id']}:revoke", headers=auth(admin_key))
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 401


async def test_an_agent_binding_admits_despite_a_cached_refusal(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """The core derives an agent's binding (CP-ADR-0073 §6) and drops the cache too.

    The executor usually knocks before the placement service has linked its
    identity; that refusal must not outlive the link, and the retirement must
    close entry just as promptly — without restarting the API.
    """
    enable_iam(iam_app, signing_key, ttl_seconds=600.0)
    admin_key = (await do_bootstrap(iam_client))["apiKey"]["key"]
    spec = {
        "displayName": "Notifier",
        "identity": {"kind": "service", "permissions": ["tasks.read"]},
        "placement": "none",
    }
    published = await iam_client.post(
        "/api/v1/agents", json={"key": "notifier", "spec": spec}, headers=auth(admin_key)
    )
    assert published.status_code == 201, published.text
    iam_tenant, iam_principal = uuid.uuid4(), uuid.uuid4()
    token = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ], ttl_seconds=3600
    )
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 401

    linked = await iam_client.put(
        "/api/v1/agents/notifier/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(iam_tenant),
            "iamPrincipalId": str(iam_principal),
        },
        headers=auth(admin_key),
    )
    assert linked.status_code == 200, linked.text
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 200
    me = await iam_client.get("/api/v1/agents/me", headers=auth(token))
    assert me.status_code == 200, me.text
    assert me.json()["principalId"] == linked.json()["principalId"]

    retired = await iam_client.post(
        "/api/v1/agents/notifier:retire", json={"reason": "gone"}, headers=auth(admin_key)
    )
    assert retired.status_code == 200, retired.text
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 401


async def test_a_replaced_agent_identity_switches_entry_despite_a_warm_cache(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, signing_key: SigningKey
) -> None:
    """``identity:replace`` (CP-ADR-0073, amendment 2026-09-30) drops both cache entries.

    The re-created service account knocks before the registry knows it, and
    the previous one was just admitted: neither answer may outlive the switch.
    """
    enable_iam(iam_app, signing_key, ttl_seconds=600.0)
    admin_key = (await do_bootstrap(iam_client))["apiKey"]["key"]
    spec = {
        "displayName": "Notifier",
        "identity": {"kind": "service", "permissions": ["tasks.read"]},
        "placement": "none",
    }
    published = await iam_client.post(
        "/api/v1/agents", json={"key": "notifier", "spec": spec}, headers=auth(admin_key)
    )
    assert published.status_code == 201, published.text
    iam_tenant, old_principal, new_principal = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old_token, new_token = (
        signing_key.issue(
            subject=subject, tenant_id=iam_tenant, scopes=[SCOPE_READ], ttl_seconds=3600
        )
        for subject in (old_principal, new_principal)
    )
    linked = await iam_client.put(
        "/api/v1/agents/notifier/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(iam_tenant),
            "iamPrincipalId": str(old_principal),
        },
        headers=auth(admin_key),
    )
    assert linked.status_code == 200, linked.text
    assert (await iam_client.get("/api/v1/tasks", headers=auth(old_token))).status_code == 200
    assert (await iam_client.get("/api/v1/tasks", headers=auth(new_token))).status_code == 401

    replaced = await iam_client.post(
        "/api/v1/agents/notifier/identity:replace",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(iam_tenant),
            "iamPrincipalId": str(new_principal),
            "reason": "service account re-created",
        },
        headers=auth(admin_key),
    )
    assert replaced.status_code == 200, replaced.text
    assert (await iam_client.get("/api/v1/tasks", headers=auth(old_token))).status_code == 401
    assert (await iam_client.get("/api/v1/tasks", headers=auth(new_token))).status_code == 200
    me = await iam_client.get("/api/v1/agents/me", headers=auth(new_token))
    assert me.status_code == 200, me.text
    assert me.json()["principalId"] == linked.json()["principalId"]
