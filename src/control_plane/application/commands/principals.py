"""Principal and API key management commands."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import (
    ALL_PERMISSIONS,
    Permission,
    PrincipalKind,
    PrincipalStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.auth.api_keys import GeneratedKey, generate_api_key
from control_plane.infrastructure.db.models import ApiKey, Principal


async def get_tenant_principal(
    session: AsyncSession,
    ctx: AuthContext,
    principal_id: uuid.UUID,
) -> Principal:
    principal = await session.scalar(
        select(Principal).where(Principal.id == principal_id, Principal.tenant_id == ctx.tenant_id)
    )
    if principal is None:
        raise NotFoundError("Principal not found", details={"principalId": str(principal_id)})
    return principal


async def create_principal(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    kind: str,
    display_name: str,
    metadata: dict[str, Any] | None = None,
    status: str = PrincipalStatus.ACTIVE,
) -> Principal:
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    if kind not in set(PrincipalKind):
        raise ValidationError("invalid_kind", f"Unknown principal kind: {kind}")
    if status not in set(PrincipalStatus):
        raise ValidationError("invalid_status", f"Unknown principal status: {status}")
    if not display_name.strip():
        raise ValidationError("invalid_display_name", "displayName must not be empty")

    now = utcnow()
    principal = Principal(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        kind=kind,
        display_name=display_name.strip(),
        status=status,
        metadata_json=metadata or {},
        created_at=now,
        updated_at=now,
    )
    session.add(principal)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="principal.created",
        entity_type="principal",
        entity_id=principal.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"kind": kind, "displayName": principal.display_name},
    )
    return principal


@dataclass(frozen=True)
class CreatedApiKey:
    api_key: ApiKey
    generated: GeneratedKey


async def create_api_key(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    permissions: list[str],
    expires_at: datetime | None = None,
) -> CreatedApiKey:
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    unknown = sorted(set(permissions) - ALL_PERMISSIONS)
    if unknown:
        raise ValidationError(
            "invalid_permissions",
            "Unknown permissions requested",
            details={"unknown": unknown},
        )
    if not permissions:
        raise ValidationError("invalid_permissions", "permissions must not be empty")
    # No privilege escalation: a key can only grant permissions its creator
    # holds (admin holds everything).
    if not ctx.has(Permission.ADMIN):
        if Permission.ADMIN.value in permissions:
            raise ValidationError(
                "invalid_permissions", "Only an admin key can create another admin key"
            )
        missing = sorted(set(permissions) - set(ctx.permissions))
        if missing:
            raise AuthorizationError(
                "Cannot grant permissions the creating key does not hold",
                code="permission_escalation",
                details={"missing": missing},
            )
    principal = await get_tenant_principal(session, ctx, principal_id)
    if principal.status != PrincipalStatus.ACTIVE:
        raise ValidationError(
            "principal_not_active",
            "Cannot issue a key for a non-active principal",
            details={"status": principal.status},
        )

    now = utcnow()
    generated = generate_api_key()
    api_key = ApiKey(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        principal_id=principal.id,
        key_prefix=generated.prefix,
        key_hash=generated.key_hash,
        permissions=sorted(set(permissions)),
        expires_at=expires_at,
        created_at=now,
    )
    session.add(api_key)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="api_key.created",
        entity_type="api_key",
        entity_id=api_key.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "principalId": str(principal.id),
            "keyPrefix": generated.prefix,
            "permissions": api_key.permissions,
        },
    )
    return CreatedApiKey(api_key=api_key, generated=generated)


async def revoke_api_key(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    api_key_id: uuid.UUID,
) -> ApiKey:
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    api_key = await session.scalar(
        select(ApiKey)
        .where(ApiKey.id == api_key_id, ApiKey.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if api_key is None:
        raise NotFoundError("API key not found", details={"apiKeyId": str(api_key_id)})
    if api_key.revoked_at is not None:
        return api_key  # idempotent

    api_key.revoked_at = utcnow()
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="api_key.revoked",
        entity_type="api_key",
        entity_id=api_key.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"keyPrefix": api_key.key_prefix},
    )
    return api_key


# Marks the tenant's core service principal in ``principals.metadata``.
CORE_PRINCIPAL_MARK = "control-plane-core"


async def ensure_core_principal(session: AsyncSession, tenant_id: uuid.UUID) -> Principal:
    """The tenant's own service principal of core, created on first use.

    Core sometimes has to write on its own behalf rather than on a caller's —
    a follow-up work item for a failed approval outcome is filed by core, not
    by the decider whose authority just proved insufficient. It holds no API
    key and no IAM binding: nobody can sign in as it, it only attributes
    core's own writes. Concurrent first uses are serialized per tenant.
    """
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"{CORE_PRINCIPAL_MARK}:{tenant_id}")))
    )
    principal = await session.scalar(
        select(Principal)
        .where(
            Principal.tenant_id == tenant_id,
            Principal.kind == PrincipalKind.SERVICE,
            Principal.metadata_json["system"].astext == CORE_PRINCIPAL_MARK,
        )
        .order_by(Principal.created_at)
        .limit(1)
    )
    if principal is not None:
        return principal
    now = utcnow()
    principal = Principal(
        id=new_uuid(),
        tenant_id=tenant_id,
        kind=PrincipalKind.SERVICE,
        display_name="Control Plane",
        status=PrincipalStatus.ACTIVE,
        metadata_json={"system": CORE_PRINCIPAL_MARK},
        created_at=now,
        updated_at=now,
    )
    session.add(principal)
    await session.flush()
    return principal
