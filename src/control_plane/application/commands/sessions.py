"""Work session lifecycle: open, heartbeat, close.

A session is a lease: it must be heartbeaten to stay alive. Heartbeats extend
``expires_at``; an expired session is marked ``stale`` lazily (on first touch)
or by the background worker — correctness never depends on the worker running.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize, require
from control_plane.application.commands.delegations import find_active_delegation
from control_plane.application.commands.principals import get_tenant_principal
from control_plane.application.common import clamp_ttl, new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.locking import lock_principal_key_share
from control_plane.config import Settings
from control_plane.domain.enums import (
    KNOWN_HARNESS_CAPABILITIES,
    SUPPORTED_HARNESS_PROTOCOL_VERSIONS,
    ControlLevel,
    Permission,
    PrincipalKind,
    PrincipalStatus,
    SessionStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.db.models import Session

from ._claim_release import release_active_claims_for_session


@dataclass(frozen=True)
class HarnessSpec:
    """Harness registration block supplied at session open (all optional)."""

    harness_type: str
    harness_version: str = ""
    protocol_version: str = "1"
    capabilities: list[str] = field(default_factory=list)
    hostname: str | None = None
    environment: dict[str, Any] = field(default_factory=dict)


def validate_harness_spec(spec: HarnessSpec) -> None:
    """Protocol negotiation: reject unsupported versions loudly, ignore
    unknown capabilities silently (forward compatibility for newer clients)."""
    if spec.protocol_version not in SUPPORTED_HARNESS_PROTOCOL_VERSIONS:
        raise ValidationError(
            "unsupported_protocol_version",
            f"Harness protocol version '{spec.protocol_version}' is not supported",
            details={
                "requested": spec.protocol_version,
                "supported": sorted(SUPPORTED_HARNESS_PROTOCOL_VERSIONS),
            },
        )
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,99}", spec.harness_type) is None:
        raise ValidationError(
            "invalid_harness",
            "harness.type must be a lowercase client identifier",
        )


def known_capabilities(capabilities: list[str]) -> list[str]:
    """Keep declared capabilities the server understands (order-preserving)."""
    return [c for c in dict.fromkeys(capabilities) if c in KNOWN_HARNESS_CAPABILITIES]


async def open_session(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    client_name: str,
    client_version: str = "",
    metadata: dict[str, Any] | None = None,
    on_behalf_of_id: uuid.UUID | None = None,
    ttl_seconds: int | None = None,
    harness: HarnessSpec | None = None,
) -> Session:
    await authorize(ctx, Permission.SESSIONS_OPEN)
    if harness is not None:
        validate_harness_spec(harness)
    ttl = clamp_ttl(
        ttl_seconds,
        default=settings.session_ttl_seconds,
        minimum=settings.session_ttl_min_seconds,
        maximum=settings.session_ttl_max_seconds,
    )

    delegation_id: uuid.UUID | None = None
    if on_behalf_of_id is not None:
        await get_tenant_principal(session, ctx, on_behalf_of_id)
        # The session references the human: its principal is locked now, and
        # its status and the delegation are read under that lock. A
        # ``principals/{id}:disable`` of the human in flight holds it ``FOR
        # UPDATE``; this waits, then sees ``disabled`` and a revoked delegation
        # instead of opening an active session on behalf of a disabled
        # principal (CP-ADR-0077 §3, ``application/locking.py``).
        human_status = await lock_principal_key_share(session, ctx.tenant_id, on_behalf_of_id)
        if human_status != PrincipalStatus.ACTIVE:
            raise AuthorizationError(
                "Delegating principal is not active", code="delegation_required"
            )
        delegation = await find_active_delegation(
            session,
            tenant_id=ctx.tenant_id,
            human_principal_id=on_behalf_of_id,
            agent_principal_id=ctx.principal_id,
        )
        if delegation is None:
            raise AuthorizationError(
                "No active delegation allows acting on behalf of this principal",
                code="delegation_required",
                details={"onBehalfOf": str(on_behalf_of_id)},
            )
        delegation_id = delegation.id

    now = utcnow()
    work_session = Session(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
        on_behalf_of_id=on_behalf_of_id,
        delegation_id=delegation_id,
        status=SessionStatus.ACTIVE,
        client_name=client_name,
        client_version=client_version,
        control_level=(
            ControlLevel.HUMAN_OPERATED
            if ctx.principal_kind == PrincipalKind.HUMAN
            else ControlLevel.CONNECTED
        ),
        harness_type=harness.harness_type if harness else None,
        harness_version=harness.harness_version if harness else None,
        protocol_version=harness.protocol_version if harness else None,
        harness_capabilities=known_capabilities(harness.capabilities) if harness else None,
        hostname=harness.hostname if harness else None,
        environment=harness.environment if harness else None,
        metadata_json=metadata or {},
        started_at=now,
        heartbeat_at=now,
        expires_at=now + timedelta(seconds=ttl),
        ended_at=None,
    )
    session.add(work_session)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="session.opened",
        entity_type="session",
        entity_id=work_session.id,
        actor_id=ctx.principal_id,
        session_id=work_session.id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "clientName": client_name,
            "harnessType": work_session.harness_type,
            "controlLevel": work_session.control_level,
            "protocolVersion": work_session.protocol_version,
            "onBehalfOf": str(on_behalf_of_id) if on_behalf_of_id else None,
            "expiresAt": work_session.expires_at.isoformat(),
        },
    )
    return work_session


async def _get_session_for_update(
    session: AsyncSession, ctx: AuthContext, session_id: uuid.UUID
) -> Session:
    work_session = await session.scalar(
        select(Session)
        .where(Session.id == session_id, Session.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if work_session is None:
        raise NotFoundError("Session not found", details={"sessionId": str(session_id)})
    return work_session


def _require_session_access(ctx: AuthContext, work_session: Session) -> None:
    if work_session.principal_id != ctx.principal_id:
        require(ctx, Permission.SESSIONS_MANAGE)


async def _mark_session_stale(
    session: AsyncSession, ctx: AuthContext, work_session: Session
) -> None:
    work_session.status = SessionStatus.STALE
    work_session.ended_at = utcnow()
    await record_event(
        session,
        tenant_id=work_session.tenant_id,
        event_type="session.expired",
        entity_type="session",
        entity_id=work_session.id,
        actor_id=ctx.principal_id,
        session_id=work_session.id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"expiresAt": work_session.expires_at.isoformat()},
    )


async def heartbeat_session(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    session_id: uuid.UUID,
    ttl_seconds: int | None = None,
) -> Session:
    await authorize(ctx, Permission.SESSIONS_OPEN, Permission.SESSIONS_MANAGE)
    work_session = await _get_session_for_update(session, ctx, session_id)
    _require_session_access(ctx, work_session)

    if work_session.status != SessionStatus.ACTIVE:
        raise ConflictError(
            "session_not_active",
            "Session is not active",
            details={"sessionId": str(session_id), "status": work_session.status},
        )
    now = utcnow()
    if work_session.expires_at <= now:
        # Lazily converge the state: mark stale and COMMIT (no exception here —
        # a raise would roll the transition back). The API layer translates a
        # non-active result into 409 session_expired.
        await _mark_session_stale(session, ctx, work_session)
        return work_session

    ttl = clamp_ttl(
        ttl_seconds,
        default=settings.session_ttl_seconds,
        minimum=settings.session_ttl_min_seconds,
        maximum=settings.session_ttl_max_seconds,
    )
    work_session.heartbeat_at = now
    work_session.expires_at = now + timedelta(seconds=ttl)
    return work_session


async def close_session(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    session_id: uuid.UUID,
) -> Session:
    await authorize(ctx, Permission.SESSIONS_OPEN, Permission.SESSIONS_MANAGE)
    work_session = await _get_session_for_update(session, ctx, session_id)
    _require_session_access(ctx, work_session)

    if work_session.status == SessionStatus.CLOSED:
        return work_session  # idempotent

    now = utcnow()
    work_session.status = SessionStatus.CLOSED
    work_session.ended_at = now

    released = await release_active_claims_for_session(
        session,
        tenant_id=ctx.tenant_id,
        session_id=work_session.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        reason="session_closed",
    )

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="session.closed",
        entity_type="session",
        entity_id=work_session.id,
        actor_id=ctx.principal_id,
        session_id=work_session.id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"releasedClaims": [str(c) for c in released]},
    )
    return work_session
