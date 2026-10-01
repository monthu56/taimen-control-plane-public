"""Delegations: allow an agent to act on behalf of a human."""

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.principals import get_tenant_principal
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import ALL_PERMISSIONS, Permission, PrincipalKind
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.db.models import Delegation


async def create_delegation(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    human_principal_id: uuid.UUID,
    agent_principal_id: uuid.UUID,
    permissions: list[str],
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> Delegation:
    await authorize(ctx, Permission.DELEGATIONS_MANAGE)
    human = await get_tenant_principal(session, ctx, human_principal_id)
    agent = await get_tenant_principal(session, ctx, agent_principal_id)
    if human.kind != PrincipalKind.HUMAN:
        raise ValidationError(
            "invalid_delegation", "humanPrincipalId must reference a human principal"
        )
    if agent.kind != PrincipalKind.AGENT:
        raise ValidationError(
            "invalid_delegation", "agentPrincipalId must reference an agent principal"
        )
    unknown = sorted(set(permissions) - ALL_PERMISSIONS)
    if unknown:
        raise ValidationError(
            "invalid_permissions", "Unknown permissions requested", details={"unknown": unknown}
        )

    now = utcnow()
    effective_start = starts_at or now
    if expires_at is not None and expires_at <= effective_start:
        raise ValidationError("invalid_delegation", "expiresAt must be after startsAt")

    delegation = Delegation(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        human_principal_id=human.id,
        agent_principal_id=agent.id,
        permissions=sorted(set(permissions)),
        starts_at=effective_start,
        expires_at=expires_at,
        created_at=now,
    )
    session.add(delegation)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="delegation.created",
        entity_type="delegation",
        entity_id=delegation.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "humanPrincipalId": str(human.id),
            "agentPrincipalId": str(agent.id),
            "permissions": delegation.permissions,
        },
    )
    return delegation


async def revoke_delegation(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    delegation_id: uuid.UUID,
) -> Delegation:
    await authorize(ctx, Permission.DELEGATIONS_MANAGE)
    delegation = await session.scalar(
        select(Delegation)
        .where(Delegation.id == delegation_id, Delegation.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if delegation is None:
        raise NotFoundError("Delegation not found", details={"delegationId": str(delegation_id)})
    if delegation.revoked_at is not None:
        return delegation  # idempotent

    delegation.revoked_at = utcnow()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="delegation.revoked",
        entity_type="delegation",
        entity_id=delegation.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={},
    )
    return delegation


async def find_active_delegation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    human_principal_id: uuid.UUID,
    agent_principal_id: uuid.UUID,
) -> Delegation | None:
    now = utcnow()
    result = await session.scalars(
        select(Delegation).where(
            Delegation.tenant_id == tenant_id,
            Delegation.human_principal_id == human_principal_id,
            Delegation.agent_principal_id == agent_principal_id,
            Delegation.revoked_at.is_(None),
            Delegation.starts_at <= now,
        )
    )
    for delegation in result:
        if delegation.expires_at is None or delegation.expires_at > now:
            return delegation
    return None


async def find_delegation_from_any(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    human_principal_ids: Sequence[uuid.UUID],
    agent_principal_id: uuid.UUID,
) -> Delegation | None:
    """An active delegation to the agent from any of the principals, if there is one."""
    if not human_principal_ids:
        return None
    now = utcnow()
    result = await session.scalars(
        select(Delegation)
        .where(
            Delegation.tenant_id == tenant_id,
            Delegation.human_principal_id.in_(human_principal_ids),
            Delegation.agent_principal_id == agent_principal_id,
            Delegation.revoked_at.is_(None),
            Delegation.starts_at <= now,
        )
        .order_by(Delegation.created_at, Delegation.id)
    )
    for delegation in result:
        if delegation.expires_at is None or delegation.expires_at > now:
            return delegation
    return None
