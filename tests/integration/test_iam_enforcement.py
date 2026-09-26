"""Conformance matrix for the IAM Policy Enforcement Point (IAM-7).

Every case here answers one question about the enforcement order
``identity -> revocation -> entitlement -> domain policy``: what is rejected,
at which step, and what the client is told. The point of the file is that the
answers stay the same for HTTP, realtime and the compatibility window — a path
that quietly skips a step is exactly the defect these tests exist to catch.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from platform_auth import (
    Decision,
    EntitlementUnavailable,
    JwksCache,
    NullEntitlementClient,
    PolicyEnforcementPoint,
    TokenVerifier,
    TrustedAuthContext,
    VerifierConfig,
)
from platform_auth.testing import SigningKey
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from control_plane.config import Settings
from control_plane.domain.errors import AuthorizationError
from control_plane.infrastructure.auth.iam import (
    SCOPE_ADMIN,
    SCOPE_DECIDE,
    SCOPE_READ,
    SCOPE_WRITE,
    BindingDirectory,
    IamEnforcement,
    LoggingAuditSink,
    channel_of,
    decision_purpose,
    feature_for_path,
    narrow_permissions,
)
from control_plane.main import create_app
from control_plane.worker.main import Worker
from tests.helpers import BOOTSTRAP_TOKEN, auth, do_bootstrap

ISSUER = "https://iam.test"
AUDIENCE = "control-plane"

DEFAULT_PERMISSIONS = [
    "tasks.read",
    "tasks.write",
    "workspaces.read",
    "projects.read",
    "events.read",
    "sessions.open",
]


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


class StubEntitlement:
    """Entitlement service replaced by a stub: the PEP is what is under test."""

    def __init__(self, outcome: Decision | Exception) -> None:
        self.outcome = outcome
        self.calls = 0

    async def check(
        self, ctx: TrustedAuthContext, *, feature: str, required_amount: int = 0
    ) -> Decision:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def enable_iam(
    app: FastAPI,
    signing_key: SigningKey,
    *,
    entitlement: Any | None = None,
    ttl_seconds: float = 0.0,
) -> IamEnforcement:
    """Attach a PEP to a running app with the JWKS seeded instead of fetched."""
    keys = JwksCache(f"{ISSUER}/.well-known/jwks.json")
    keys.seed(signing_key.jwks())
    verifier = TokenVerifier(keys, VerifierConfig(issuer=ISSUER, audience=AUDIENCE))
    bindings = BindingDirectory(
        app.state.session_factory,
        # No caching by default: every case must see the state it just wrote.
        # A case about the cache itself asks for a TTL explicitly.
        ttl_seconds=ttl_seconds,
        stale_after_seconds=600.0,
    )
    enforcement = IamEnforcement(
        pep=PolicyEnforcementPoint(
            verifier,
            entitlement=entitlement or NullEntitlementClient("control-plane"),
            revocation=bindings,
            audit=LoggingAuditSink(),
        ),
        bindings=bindings,
        default_feature="api",
        entitlement_enabled=entitlement is not None,
        closables=[keys],
    )
    app.state.iam = enforcement
    return enforcement


def add_binding(
    engine: Engine,
    *,
    tenant_id: str,
    principal_id: str,
    iam_principal_id: uuid.UUID,
    iam_tenant_id: uuid.UUID,
    permissions: list[str] | None = None,
    status: str = "active",
    issuer: str = ISSUER,
) -> uuid.UUID:
    binding_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO iam_principal_bindings "
                "(id, tenant_id, principal_id, issuer, iam_tenant_id, iam_principal_id, "
                " permissions, status, created_at, updated_at) "
                "VALUES (:id, :tenant, :principal, :issuer, :iam_tenant, :iam_principal, "
                " CAST(:permissions AS jsonb), :status, now(), now())"
            ),
            {
                "id": binding_id,
                "tenant": tenant_id,
                "principal": principal_id,
                "issuer": issuer,
                "iam_tenant": iam_tenant_id,
                "iam_principal": iam_principal_id,
                "permissions": _json(permissions or DEFAULT_PERMISSIONS),
                "status": status,
            },
        )
    return binding_id


def _json(values: list[str]) -> str:
    import json

    return json.dumps(values)


async def bootstrap_with_binding(
    client: httpx.AsyncClient,
    engine: Engine,
    *,
    permissions: list[str] | None = None,
    status: str = "active",
    iam_tenant_id: uuid.UUID | None = None,
) -> tuple[dict[str, Any], uuid.UUID, uuid.UUID]:
    """Bootstrap a tenant and bind a federated identity to its admin principal."""
    result = await do_bootstrap(client)
    iam_principal_id = uuid.uuid4()
    iam_tenant = iam_tenant_id or uuid.uuid4()
    add_binding(
        engine,
        tenant_id=result["tenant"]["id"],
        principal_id=result["adminPrincipal"]["id"],
        iam_principal_id=iam_principal_id,
        iam_tenant_id=iam_tenant,
        permissions=permissions,
        status=status,
    )
    return result, iam_principal_id, iam_tenant


# --- identity ----------------------------------------------------------------


async def test_iam_token_is_accepted_and_maps_to_the_local_principal(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)

    token = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ, SCOPE_WRITE]
    )
    response = await iam_client.get("/api/v1/tasks", headers=auth(token))

    assert response.status_code == 200, response.text


async def test_token_of_another_audience_is_rejected(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)

    token = signing_key.issue(
        subject=iam_principal,
        tenant_id=iam_tenant,
        audience="memory-service",
        scopes=[SCOPE_READ],
    )
    response = await iam_client.get("/api/v1/tasks", headers=auth(token))

    assert response.status_code == 401


async def test_foreign_issuer_is_rejected(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)

    token = signing_key.issue(
        subject=iam_principal,
        tenant_id=iam_tenant,
        issuer="https://evil.test",
        scopes=[SCOPE_READ],
    )
    response = await iam_client.get("/api/v1/tasks", headers=auth(token))

    assert response.status_code == 401


# --- revocation --------------------------------------------------------------


async def test_identity_without_a_binding_is_indistinguishable_from_revoked(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    await do_bootstrap(iam_client)

    unknown = signing_key.issue(scopes=[SCOPE_READ])
    unknown_response = await iam_client.get("/api/v1/tasks", headers=auth(unknown))

    assert unknown_response.status_code == 401
    assert unknown_response.json()["error"]["code"] == "invalid_credentials"


async def test_disabled_binding_closes_entry(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(
        iam_client, sync_engine, status="disabled"
    )

    token = signing_key.issue(subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ])
    response = await iam_client.get("/api/v1/tasks", headers=auth(token))

    assert response.status_code == 401


async def test_token_of_another_tenant_does_not_match_by_subject(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    """The binding pins the IAM tenant: a same subject from elsewhere is not us."""
    enable_iam(iam_app, signing_key)
    _, iam_principal, _ = await bootstrap_with_binding(iam_client, sync_engine)

    token = signing_key.issue(subject=iam_principal, tenant_id=uuid.uuid4(), scopes=[SCOPE_READ])
    response = await iam_client.get("/api/v1/tasks", headers=auth(token))

    assert response.status_code == 401


async def test_revocation_takes_effect_without_waiting_for_expiry(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)
    token = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ], ttl_seconds=3600
    )

    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 200

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE iam_principal_bindings SET revoked_at = now(), status = 'disabled'")
        )

    # Same still-valid token, revoked binding: entry closes now, not in an hour.
    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 401


# --- scope ceiling and domain policy ----------------------------------------


async def test_token_without_a_control_plane_scope_is_denied(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)

    token = signing_key.issue(subject=iam_principal, tenant_id=iam_tenant, scopes=["memory:read"])
    response = await iam_client.get("/api/v1/tasks", headers=auth(token))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


async def test_read_only_token_cannot_write_even_with_a_writing_binding(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    """The token scope is a ceiling: the binding may allow more, the token wins."""
    enable_iam(iam_app, signing_key)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)

    token = signing_key.issue(subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ])

    assert (await iam_client.get("/api/v1/tasks", headers=auth(token))).status_code == 200

    response = await iam_client.post(
        "/api/v1/tasks", json={"title": "denied by scope"}, headers=auth(token)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


async def test_license_without_domain_permission_is_denied(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    """Identity and entitlement are not enough: the product still decides."""
    enable_iam(iam_app, signing_key)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(
        iam_client, sync_engine, permissions=["tasks.read"]
    )

    token = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ, SCOPE_WRITE]
    )
    response = await iam_client.post(
        "/api/v1/tasks", json={"title": "no permission"}, headers=auth(token)
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


# --- entitlement -------------------------------------------------------------


async def test_valid_identity_without_license_is_denied(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    entitlement = StubEntitlement(
        Decision(
            allowed=False,
            reason="no_active_grant",
            product="control-plane",
            feature="tasks",
        )
    )
    enable_iam(iam_app, signing_key, entitlement=entitlement)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)

    token = signing_key.issue(subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ])
    response = await iam_client.get("/api/v1/tasks", headers=auth(token))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_entitled"
    assert entitlement.calls == 1


async def test_entitlement_outage_fails_closed_with_503(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    entitlement = StubEntitlement(EntitlementUnavailable("entitlement_service_unavailable"))
    enable_iam(iam_app, signing_key, entitlement=entitlement)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)

    token = signing_key.issue(subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ])
    response = await iam_client.get("/api/v1/tasks", headers=auth(token))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "entitlement_unavailable"


async def test_feature_is_derived_per_api_area(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    seen: list[str] = []

    class Recording(StubEntitlement):
        async def check(
            self, ctx: TrustedAuthContext, *, feature: str, required_amount: int = 0
        ) -> Decision:
            seen.append(feature)
            return await super().check(ctx, feature=feature, required_amount=required_amount)

    entitlement = Recording(
        Decision(allowed=True, reason="allowed", product="control-plane", feature="tasks")
    )
    enable_iam(iam_app, signing_key, entitlement=entitlement)
    _, iam_principal, iam_tenant = await bootstrap_with_binding(iam_client, sync_engine)
    token = signing_key.issue(subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ])

    await iam_client.get("/api/v1/tasks", headers=auth(token))
    await iam_client.get("/api/v1/workspaces", headers=auth(token))

    assert seen == ["tasks", "workspaces"]


# --- realtime ----------------------------------------------------------------


def test_websocket_goes_through_the_same_enforcement(
    iam_settings: Settings, signing_key: SigningKey, sync_engine: Engine
) -> None:
    """A subscription is subject to the same ceiling as an ordinary request.

    The scoped denial (4403) is what makes this test meaningful: only the PEP
    can produce it. A missing credential would close with 4401 through the
    legacy path and would prove nothing about enforcement.
    """
    application = create_app(iam_settings)
    with TestClient(application) as test_client:
        enable_iam(application, signing_key)
        bootstrapped = test_client.post(
            "/api/v1/bootstrap",
            json={"tenantSlug": "acme", "tenantName": "Acme", "adminDisplayName": "Admin"},
            headers=auth(BOOTSTRAP_TOKEN),
        ).json()
        iam_principal, iam_tenant = uuid.uuid4(), uuid.uuid4()
        add_binding(
            sync_engine,
            tenant_id=bootstrapped["tenant"]["id"],
            principal_id=bootstrapped["adminPrincipal"]["id"],
            iam_principal_id=iam_principal,
            iam_tenant_id=iam_tenant,
            permissions=["events.read"],
        )

        out_of_scope = signing_key.issue(
            subject=iam_principal, tenant_id=iam_tenant, scopes=["memory:read"]
        )
        with (
            test_client.websocket_connect("/api/v1/events/ws", headers=auth(out_of_scope)) as ws,
            pytest.raises(WebSocketDisconnect) as denied,
        ):
            ws.receive_json()

        allowed = signing_key.issue(
            subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ]
        )
        with test_client.websocket_connect("/api/v1/events/ws", headers=auth(allowed)) as ws:
            ready = ws.receive_json()

    assert denied.value.code == 4403
    # The scoped token gets a live stream rather than a close frame.
    assert "cursor" in ready


# --- decision from a channel (CP-ADR-0070) ------------------------------------

DECIDER_BINDING = [*DEFAULT_PERMISSIONS, "approvals.read", "approvals.decide", "approvals.manage"]


async def _channel_setup(
    client: httpx.AsyncClient, engine: Engine
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any], uuid.UUID, uuid.UUID]:
    """A person bound in IAM, a task and two approvals assigned to that person."""
    result, iam_principal, iam_tenant = await bootstrap_with_binding(
        client, engine, permissions=DECIDER_BINDING
    )
    admin_key = result["apiKey"]["key"]
    person = result["adminPrincipal"]["id"]
    task = (
        await client.post("/api/v1/tasks", json={"title": "Pay"}, headers=auth(admin_key))
    ).json()
    own, other = [
        (
            await client.post(
                "/api/v1/approvals",
                json={"task": task["id"], "assignedPrincipalId": person},
                headers=auth(admin_key),
            )
        ).json()
        for _ in range(2)
    ]
    return admin_key, task, own, other, iam_principal, iam_tenant


def _decision_token(
    key: SigningKey,
    iam_principal: uuid.UUID,
    iam_tenant: uuid.UUID,
    approval_id: str | None,
    **overrides: Any,
) -> str:
    """Shaped like the IAM channel-assertion exchange token (N004)."""
    claims: dict[str, Any] = {"amr": ["channel:telegram"]}
    if approval_id is not None:
        claims["purpose_ref"] = f"approval:{approval_id}"
    options: dict[str, Any] = {
        "subject": iam_principal,
        "tenant_id": iam_tenant,
        "scopes": [SCOPE_DECIDE],
        "scope_ceiling": [SCOPE_DECIDE],
        "acr": "channel:telegram",
        "ttl_seconds": 60,
        "extra_claims": claims,
    }
    options.update(overrides)
    return key.issue(**options)


async def test_channel_token_decides_only_its_own_approval(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    admin_key, task, own, other, iam_principal, iam_tenant = await _channel_setup(
        iam_client, sync_engine
    )
    token = _decision_token(signing_key, iam_principal, iam_tenant, own["id"])
    keyed = {**auth(token), "Idempotency-Key": "callback-1"}

    # Nothing but the decision of its own approval: no reads — not even of
    # that approval — no other writes, no other approval.
    refused = [
        await iam_client.get(f"/api/v1/approvals/{own['id']}", headers=keyed),
        await iam_client.get("/api/v1/approvals", headers=keyed),
        await iam_client.get("/api/v1/tasks", headers=keyed),
        await iam_client.get(f"/api/v1/tasks/{task['id']}", headers=keyed),
        await iam_client.get("/api/v1/events", headers=keyed),
        await iam_client.post("/api/v1/tasks", json={"title": "x"}, headers=keyed),
        await iam_client.post(
            f"/api/v1/tasks/{task['id']}/comments", json={"body": "x"}, headers=keyed
        ),
        await iam_client.post(f"/api/v1/approvals/{own['id']}:cancel", headers=keyed),
        await iam_client.post(f"/api/v1/approvals/{other['id']}:approve", headers=keyed),
        await iam_client.post(f"/api/v1/approvals/{other['id']}:reject", headers=keyed),
    ]
    assert [r.status_code for r in refused] == [403] * len(refused)
    assert {r.json()["error"]["code"] for r in refused} == {"outside_purpose"}

    # The decision itself needs an Idempotency-Key: a retrying adapter replays.
    unkeyed = await iam_client.post(
        f"/api/v1/approvals/{own['id']}:approve", json={"comment": "ok"}, headers=auth(token)
    )
    assert unkeyed.status_code == 422
    assert unkeyed.json()["error"]["code"] == "idempotency_key_required"

    decided = await iam_client.post(
        f"/api/v1/approvals/{own['id']}:approve", json={"comment": "ok"}, headers=keyed
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"
    replayed = await iam_client.post(
        f"/api/v1/approvals/{own['id']}:approve", json={"comment": "ok"}, headers=keyed
    )
    assert replayed.status_code == 200
    assert replayed.headers["Idempotency-Replayed"] == "true"

    untouched = await iam_client.get(f"/api/v1/approvals/{other['id']}", headers=auth(admin_key))
    assert untouched.json()["status"] == "pending"

    events = (
        await iam_client.get("/api/v1/events?types=approval.", headers=auth(admin_key))
    ).json()["items"]
    decisions = [e for e in events if e["type"] in ("approval.approved", "approval.rejected")]
    assert len(decisions) == 1
    assert decisions[0]["entityId"] == own["id"]
    assert decisions[0]["payload"]["channel"] == "telegram"
    assert decisions[0]["payload"]["comment"] == "ok"


async def test_channel_token_rejects_and_a_direct_call_has_no_channel(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    _, _, own, other, iam_principal, iam_tenant = await _channel_setup(iam_client, sync_engine)

    token = _decision_token(signing_key, iam_principal, iam_tenant, own["id"])
    rejected = await iam_client.post(
        f"/api/v1/approvals/{own['id']}:reject",
        headers={**auth(token), "Idempotency-Key": "callback-2"},
    )
    assert rejected.status_code == 200, rejected.text

    # The same person through the ordinary API: a decision without a channel.
    direct = signing_key.issue(
        subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ, SCOPE_WRITE]
    )
    approved = await iam_client.post(
        f"/api/v1/approvals/{other['id']}:approve", headers=auth(direct)
    )
    assert approved.status_code == 200, approved.text

    events = (await iam_client.get("/api/v1/events?types=approval.", headers=auth(direct))).json()[
        "items"
    ]
    channels = {
        e["type"]: e["payload"]["channel"]
        for e in events
        if e["type"] in ("approval.approved", "approval.rejected")
    }
    assert channels == {"approval.rejected": "telegram", "approval.approved": None}


async def test_decision_token_without_a_purpose_or_the_right_is_refused(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    admin_key, _, own, _, iam_principal, iam_tenant = await _channel_setup(iam_client, sync_engine)
    keyed = {"Idempotency-Key": "callback-3"}

    # A decision token that does not name its approval decides nothing.
    unnamed = _decision_token(signing_key, iam_principal, iam_tenant, None)
    response = await iam_client.post(
        f"/api/v1/approvals/{own['id']}:approve", headers={**auth(unnamed), **keyed}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "purpose_ref_required"

    # Other scopes on the same token do not widen it past the one decision.
    widened = _decision_token(
        signing_key,
        iam_principal,
        iam_tenant,
        own["id"],
        scopes=[SCOPE_DECIDE, SCOPE_READ, SCOPE_WRITE],
        scope_ceiling=None,
    )
    response = await iam_client.get("/api/v1/tasks", headers=auth(widened))
    assert response.status_code == 403

    # The scope carries no right of its own: the binding must grant the decision.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE iam_principal_bindings SET permissions = CAST(:p AS jsonb) "
                "WHERE iam_principal_id = :iam"
            ),
            {"p": _json(DEFAULT_PERMISSIONS), "iam": iam_principal},
        )
    token = _decision_token(signing_key, iam_principal, iam_tenant, own["id"])
    response = await iam_client.post(
        f"/api/v1/approvals/{own['id']}:approve", headers={**auth(token), **keyed}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"

    pending = await iam_client.get(f"/api/v1/approvals/{own['id']}", headers=auth(admin_key))
    assert pending.json()["status"] == "pending"


# --- compatibility window ----------------------------------------------------


async def test_legacy_key_keeps_working_while_the_window_is_open(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    enable_iam(iam_app, signing_key)
    result = await do_bootstrap(iam_client)

    response = await iam_client.get("/api/v1/tasks", headers=auth(result["apiKey"]["key"]))

    assert response.status_code == 200


async def test_closing_the_window_rejects_the_legacy_key(
    iam_settings: Settings, sync_engine: Engine, signing_key: SigningKey
) -> None:
    settings = iam_settings.model_copy(update={"legacy_api_keys_enabled": False})
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            enable_iam(application, signing_key)
            result, iam_principal, iam_tenant = await bootstrap_with_binding(client, sync_engine)

            legacy = await client.get("/api/v1/tasks", headers=auth(result["apiKey"]["key"]))
            token = signing_key.issue(
                subject=iam_principal, tenant_id=iam_tenant, scopes=[SCOPE_READ]
            )
            federated = await client.get("/api/v1/tasks", headers=auth(token))

    assert legacy.status_code == 401
    assert federated.status_code == 200


# --- unit-level rules --------------------------------------------------------


def test_scope_narrows_permissions_but_never_widens() -> None:
    granted = ["tasks.read", "tasks.write", "admin"]

    read_only = narrow_permissions(granted, frozenset({SCOPE_READ}))
    writing = narrow_permissions(granted, frozenset({SCOPE_READ, SCOPE_WRITE}))
    admin = narrow_permissions(granted, frozenset({SCOPE_ADMIN}))
    nothing = narrow_permissions(["tasks.read"], frozenset({SCOPE_ADMIN}))

    assert read_only == frozenset({"tasks.read"})
    assert writing == frozenset({"tasks.read", "tasks.write"})
    assert admin == frozenset(granted)
    # An admin scope does not invent permissions the binding never granted.
    assert nothing == frozenset({"tasks.read"})


def test_decision_scope_leaves_only_the_decision() -> None:
    granted = ["tasks.read", "tasks.write", "approvals.decide", "approvals.read"]

    assert narrow_permissions(granted, frozenset({SCOPE_DECIDE})) == {"approvals.decide"}
    # Whatever else the token carries, the decision scope keeps it to one decision.
    assert narrow_permissions(granted, frozenset({SCOPE_DECIDE, SCOPE_ADMIN, SCOPE_WRITE})) == {
        "approvals.decide"
    }
    assert narrow_permissions(["admin"], frozenset({SCOPE_DECIDE})) == {"approvals.decide"}
    assert narrow_permissions(["tasks.write"], frozenset({SCOPE_DECIDE})) == frozenset()


def test_channel_comes_only_from_a_channel_acr() -> None:
    assert channel_of("channel:telegram") == "telegram"
    assert channel_of("") is None
    assert channel_of("urn:mfa") is None
    assert channel_of("channel:") is None
    assert channel_of("channel:Bad Name") is None


def test_decision_purpose_admits_only_its_own_decision() -> None:
    approval = uuid.uuid4()
    claims = {"purpose_ref": f"approval:{approval}"}

    for verb in ("approve", "reject"):
        action = f"POST /api/v1/approvals/{approval}:{verb}"
        assert decision_purpose(claims, action) == f"approval:{approval}"

    for action in (
        f"POST /api/v1/approvals/{approval}:cancel",
        f"GET /api/v1/approvals/{approval}",
        f"POST /api/v1/approvals/{uuid.uuid4()}:approve",
        "POST /api/v1/approvals/not-a-uuid:approve",
        "WS /api/v1/events/ws",
    ):
        with pytest.raises(AuthorizationError) as denied:
            decision_purpose(claims, action)
        assert denied.value.code == "outside_purpose"

    for bad in ({}, {"purpose_ref": "task:1"}, {"purpose_ref": 42}):
        with pytest.raises(AuthorizationError) as denied:
            decision_purpose(bad, f"POST /api/v1/approvals/{approval}:approve")
        assert denied.value.code == "purpose_ref_required"


def test_unparsed_path_falls_back_to_the_default_feature() -> None:
    assert feature_for_path("/api/v1/tasks/123", "api") == "tasks"
    assert feature_for_path("/api/v1/workspaces", "api") == "workspaces"
    assert feature_for_path("/healthz", "api") == "api"
    assert feature_for_path("/", "api") == "api"


# --- outcome of a channel decision (CP-ADR-0070, owner's decision N009) --------

# The accountant is told through a skill and the decision is noted on the task:
# both need rights a decision token does not carry.
INVOICE_PAYMENT_SCHEMA: dict[str, Any] = {
    "gates": {
        "default": {
            "outcomes": {
                "approved": [
                    {"invokeSkill": {"skill": "notify.send@1", "inputs": {"to": "accounting"}}},
                    {"comment": {"body": "Payment approved: $.approval.comment"}},
                ]
            }
        }
    }
}
PAYER_BINDING = [*DECIDER_BINDING, "skills.invoke"]


@pytest.fixture
async def worker(iam_settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(iam_settings)
    yield instance
    await instance.engine.dispose()


async def _invoice_setup(
    client: httpx.AsyncClient, engine: Engine, *, permissions: list[str]
) -> dict[str, Any]:
    """An invoice-payment task whose gate is assigned to a person bound in IAM."""
    result, iam_principal, iam_tenant = await bootstrap_with_binding(
        client, engine, permissions=permissions
    )
    admin_key = result["apiKey"]["key"]
    person = result["adminPrincipal"]["id"]
    skill = await client.post(
        "/api/v1/skills",
        json={
            "name": "notify.send",
            "version": "1",
            "sideEffects": "none",
            "riskLevel": "low",
            "contract": {
                "inputs": {"type": "object"},
                "outputs": {"type": "object"},
                "timeoutSeconds": 10,
                "idempotency": "natural",
                "implementation": {"protocol": "local", "entrypoint": "notify:send"},
            },
        },
        headers=auth(admin_key),
    )
    assert skill.status_code == 201, skill.text
    task_type = await client.post(
        "/api/v1/task-types",
        json={
            "key": "invoice-payment",
            "displayName": "Invoice payment",
            "approvalSchema": INVOICE_PAYMENT_SCHEMA,
        },
        headers=auth(admin_key),
    )
    assert task_type.status_code == 201, task_type.text
    task = (
        await client.post(
            "/api/v1/tasks",
            json={"title": "Pay invoice 42", "typeKey": "invoice-payment"},
            headers=auth(admin_key),
        )
    ).json()
    approval = await client.post(
        "/api/v1/approvals",
        json={"task": task["id"], "assignedPrincipalId": person, "gate": True},
        headers=auth(admin_key),
    )
    assert approval.status_code == 201, approval.text
    return {
        "admin_key": admin_key,
        "person": person,
        "task": task,
        "approval": approval.json(),
        "iam_principal": iam_principal,
        "iam_tenant": iam_tenant,
    }


async def _decide_from_channel(
    client: httpx.AsyncClient, key: SigningKey, s: dict[str, Any]
) -> None:
    approval_id = s["approval"]["id"]
    token = _decision_token(key, s["iam_principal"], s["iam_tenant"], approval_id)
    decided = await client.post(
        f"/api/v1/approvals/{approval_id}:approve",
        json={"comment": "ok"},
        headers={**auth(token), "Idempotency-Key": f"callback-{approval_id}"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["outcomeStatus"] == "pending"


def _decision_authority(engine: Engine, approval_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        authority: dict[str, Any] = conn.execute(
            text("SELECT decision_authority FROM approvals WHERE id = :id"), {"id": approval_id}
        ).scalar_one()
    return authority


async def _outcome_of(client: httpx.AsyncClient, key: str, approval_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/approvals/{approval_id}/outcome", headers=auth(key))
    assert response.status_code == 200, response.text
    outcome: dict[str, Any] = response.json()
    return outcome


async def test_channel_decision_outcome_runs_with_the_binding_rights(
    iam_app: FastAPI,
    iam_client: httpx.AsyncClient,
    sync_engine: Engine,
    signing_key: SigningKey,
    worker: Worker,
) -> None:
    enable_iam(iam_app, signing_key)
    s = await _invoice_setup(iam_client, sync_engine, permissions=PAYER_BINDING)
    approval_id = s["approval"]["id"]

    await _decide_from_channel(iam_client, signing_key, s)
    authority = _decision_authority(sync_engine, approval_id)
    # Same credential and identity as the token; the rights are the binding's
    # under a web session's ceiling, and the snapshot says where they came from.
    with sync_engine.connect() as conn:
        binding_id = conn.execute(
            text("SELECT id FROM iam_principal_bindings WHERE iam_principal_id = :iam"),
            {"iam": s["iam_principal"]},
        ).scalar_one()
    assert authority["credentialId"] == str(binding_id)
    assert authority["iamPrincipalId"] == str(s["iam_principal"])
    assert authority["permissions"] == sorted(PAYER_BINDING)
    assert (authority["authoritySource"], authority["channel"]) == ("binding", "telegram")

    await worker.run_once()

    outcome = await _outcome_of(iam_client, s["admin_key"], approval_id)
    assert outcome["outcomeStatus"] == "executed", outcome
    queued = outcome["actions"][0]["result"]
    assert queued["skill"] == "notify.send@1"
    invocation = (
        await iam_client.get(
            f"/api/v1/skill-invocations/{queued['invocationId']}", headers=auth(s["admin_key"])
        )
    ).json()
    assert invocation["inputs"] == {"to": "accounting"}
    comments = (
        await iam_client.get(
            f"/api/v1/tasks/{s['task']['id']}/comments", headers=auth(s["admin_key"])
        )
    ).json()["items"]
    assert [(c["body"], c["authorPrincipalId"]) for c in comments] == [
        ("Payment approved: ok", s["person"])
    ]


async def test_channel_decision_outcome_needs_the_right_on_the_binding(
    iam_app: FastAPI,
    iam_client: httpx.AsyncClient,
    sync_engine: Engine,
    signing_key: SigningKey,
    worker: Worker,
) -> None:
    """The binding bounds the outcome: without skills.invoke it is still refused."""
    enable_iam(iam_app, signing_key)
    s = await _invoice_setup(iam_client, sync_engine, permissions=DECIDER_BINDING)

    await _decide_from_channel(iam_client, signing_key, s)
    await worker.run_once()

    outcome = await _outcome_of(iam_client, s["admin_key"], s["approval"]["id"])
    assert outcome["outcomeStatus"] == "failed"
    failed, rest = outcome["actions"]
    assert (failed["action"], failed["status"]) == ("invokeSkill", "failed")
    assert failed["error"]["code"] == "forbidden"
    assert rest["status"] == "not_executed"


async def test_channel_decision_outcome_stops_when_the_binding_is_revoked(
    iam_app: FastAPI,
    iam_client: httpx.AsyncClient,
    sync_engine: Engine,
    signing_key: SigningKey,
    worker: Worker,
) -> None:
    enable_iam(iam_app, signing_key)
    s = await _invoice_setup(iam_client, sync_engine, permissions=PAYER_BINDING)

    await _decide_from_channel(iam_client, signing_key, s)
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE iam_principal_bindings SET status = 'revoked', revoked_at = now() "
                "WHERE iam_principal_id = :iam"
            ),
            {"iam": s["iam_principal"]},
        )
    await worker.run_once()

    outcome = await _outcome_of(iam_client, s["admin_key"], s["approval"]["id"])
    assert outcome["outcomeStatus"] == "failed"
    error = outcome["actions"][0]["error"]
    assert (error["code"], error["cause"]) == ("forbidden", "credential_inactive")
    assert [a["status"] for a in outcome["actions"]] == ["failed", "not_executed"]
    with sync_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar() == 0


async def test_web_decision_authority_is_the_token_as_before(
    iam_app: FastAPI, iam_client: httpx.AsyncClient, sync_engine: Engine, signing_key: SigningKey
) -> None:
    """Only a decision token is widened; a web session keeps its own snapshot."""
    enable_iam(iam_app, signing_key)
    s = await _invoice_setup(iam_client, sync_engine, permissions=PAYER_BINDING)
    approval_id = s["approval"]["id"]
    write_only = signing_key.issue(
        subject=s["iam_principal"], tenant_id=s["iam_tenant"], scopes=[SCOPE_WRITE]
    )

    decided = await iam_client.post(
        f"/api/v1/approvals/{approval_id}:approve", headers=auth(write_only)
    )
    assert decided.status_code == 200, decided.text

    authority = _decision_authority(sync_engine, approval_id)
    assert "authoritySource" not in authority
    assert "channel" not in authority
    # The write scope alone: the binding's read rights are not carried over.
    assert authority["permissions"] == sorted(p for p in PAYER_BINDING if not p.endswith(".read"))
