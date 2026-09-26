"""Break-glass keys (ADR-0065): the way in while IAM is down."""

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import Engine

from control_plane.application.commands.break_glass import (
    issue_break_glass_key,
    revoke_break_glass_keys,
)
from control_plane.config import Settings
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.db.engine import (
    build_engine,
    build_session_factory,
    transaction,
)
from control_plane.infrastructure.db.models import ApiKey
from control_plane.main import create_app
from tests.helpers import auth, create_agent_with_key, do_bootstrap


@pytest.fixture
def closed_settings(settings: Settings) -> Settings:
    """IAM-only installation: the legacy compatibility window is closed."""
    return settings.model_copy(update={"legacy_api_keys_enabled": False})


@asynccontextmanager
async def _client(settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


async def _issue(
    settings: Settings, principal_id: str, *, ttl_seconds: int = 3600, reason: str = "IAM is down"
) -> str:
    engine = build_engine(settings)
    try:
        async with transaction(build_session_factory(engine)) as session:
            issued = await issue_break_glass_key(
                session,
                settings,
                principal_id=uuid.UUID(principal_id),
                ttl_seconds=ttl_seconds,
                reason=reason,
                issued_by="operator@host",
            )
    finally:
        await engine.dispose()
    return issued.generated.full_key


async def _revoke(settings: Settings) -> int:
    engine = build_engine(settings)
    try:
        async with transaction(build_session_factory(engine)) as session:
            return len(await revoke_break_glass_keys(session, issued_by="operator@host"))
    finally:
        await engine.dispose()


async def _bootstrap(settings: Settings) -> dict[str, object]:
    # Bootstrap mints a legacy admin key; it is minted with the window open so
    # the test can show that closing the window refuses it while break-glass works.
    open_settings = settings.model_copy(update={"legacy_api_keys_enabled": True})
    async with _client(open_settings) as client:
        return await do_bootstrap(client)


async def test_break_glass_key_works_when_the_legacy_window_is_closed(
    closed_settings: Settings, sync_engine: Engine
) -> None:
    body = await _bootstrap(closed_settings)
    admin = body["adminPrincipal"]["id"]  # type: ignore[index]
    legacy_key = body["apiKey"]["key"]  # type: ignore[index]
    key = await _issue(closed_settings, admin)

    async with _client(closed_settings) as client:
        legacy = await client.get("/api/v1/tasks", headers=auth(legacy_key))
        emergency = await client.get("/api/v1/principals", headers=auth(key))

    assert legacy.status_code == 401
    assert emergency.status_code == 200

    with sync_engine.begin() as conn:
        event = conn.execute(
            text("SELECT payload FROM events WHERE event_type = 'api_key.break_glass_issued'")
        ).scalar_one()
    assert event["reason"] == "IAM is down"
    assert event["issuedBy"] == "operator@host"
    assert event["permissions"] == ["admin"]
    assert key not in str(event)


async def test_revoke_closes_every_break_glass_key(closed_settings: Settings) -> None:
    body = await _bootstrap(closed_settings)
    admin = body["adminPrincipal"]["id"]  # type: ignore[index]
    first = await _issue(closed_settings, admin)
    second = await _issue(closed_settings, admin)

    assert await _revoke(closed_settings) == 2
    assert await _revoke(closed_settings) == 0  # idempotent

    async with _client(closed_settings) as client:
        for key in (first, second):
            assert (await client.get("/api/v1/tasks", headers=auth(key))).status_code == 401


async def test_switch_off_refuses_issue_and_existing_keys(closed_settings: Settings) -> None:
    body = await _bootstrap(closed_settings)
    admin = body["adminPrincipal"]["id"]  # type: ignore[index]
    key = await _issue(closed_settings, admin)
    off = closed_settings.model_copy(update={"break_glass_enabled": False})

    with pytest.raises(ValidationError) as refused:
        await _issue(off, admin)
    assert refused.value.code == "break_glass_disabled"

    async with _client(off) as client:
        assert (await client.get("/api/v1/tasks", headers=auth(key))).status_code == 401


async def test_only_an_active_human_gets_a_key(closed_settings: Settings) -> None:
    open_settings = closed_settings.model_copy(update={"legacy_api_keys_enabled": True})
    async with _client(open_settings) as client:
        body = await do_bootstrap(client)
        agent, _ = await create_agent_with_key(client, body["apiKey"]["key"])

    with pytest.raises(ValidationError) as refused:
        await _issue(closed_settings, agent["id"])
    assert refused.value.code == "principal_not_human"

    with pytest.raises(NotFoundError):
        await _issue(closed_settings, str(uuid.uuid4()))


@pytest.mark.parametrize(
    ("ttl_seconds", "reason", "code"),
    [
        (30, "why", "invalid_ttl"),
        (4 * 3600 + 1, "why", "invalid_ttl"),
        (3600, "  ", "invalid_reason"),
    ],
)
async def test_bounds_are_checked_at_issue(
    closed_settings: Settings, ttl_seconds: int, reason: str, code: str
) -> None:
    body = await _bootstrap(closed_settings)
    admin = body["adminPrincipal"]["id"]  # type: ignore[index]
    with pytest.raises(ValidationError) as refused:
        await _issue(closed_settings, admin, ttl_seconds=ttl_seconds, reason=reason)
    assert refused.value.code == code


async def test_a_stretched_break_glass_key_is_not_honoured(closed_settings: Settings) -> None:
    """A break-glass key edited in the database to live longer stops working."""
    body = await _bootstrap(closed_settings)
    admin = body["adminPrincipal"]["id"]  # type: ignore[index]
    key = await _issue(closed_settings, admin)

    engine = build_engine(closed_settings)
    try:
        async with transaction(build_session_factory(engine)) as session:
            row = await session.scalar(select(ApiKey).where(ApiKey.key_prefix.startswith("bg")))
            assert row is not None and row.expires_at is not None
            await session.execute(
                update(ApiKey)
                .where(ApiKey.id == row.id)
                .values(expires_at=row.created_at + timedelta(days=30))
            )
    finally:
        await engine.dispose()

    async with _client(closed_settings) as client:
        assert (await client.get("/api/v1/tasks", headers=auth(key))).status_code == 401
