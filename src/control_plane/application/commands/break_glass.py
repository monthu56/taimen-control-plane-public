"""Break-glass keys (ADR-0065): the way into the Control Plane while IAM is down.

There is no API to mint one: the only path is a shell on the host running the
Control Plane, and that shell is the trust boundary. The key is a short-lived
admin credential of a named, active human; every issue and revocation lands in
the event journal with the stated reason and the host account that issued it.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.config import Settings
from control_plane.domain.enums import Permission, PrincipalKind, PrincipalStatus
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.auth.api_keys import (
    GeneratedKey,
    generate_break_glass_key,
    is_break_glass_prefix,
)
from control_plane.infrastructure.db.models import ApiKey, Principal

MIN_TTL_SECONDS = 60
MAX_REASON_LENGTH = 500


@dataclass(frozen=True)
class IssuedBreakGlassKey:
    api_key: ApiKey
    generated: GeneratedKey


async def issue_break_glass_key(
    session: AsyncSession,
    settings: Settings,
    *,
    principal_id: uuid.UUID,
    ttl_seconds: int,
    reason: str,
    issued_by: str,
) -> IssuedBreakGlassKey:
    if not settings.break_glass_enabled:
        raise ValidationError(
            "break_glass_disabled", "Break-glass keys are disabled (CP_BREAK_GLASS_ENABLED=false)"
        )
    if not MIN_TTL_SECONDS <= ttl_seconds <= settings.break_glass_max_ttl_seconds:
        raise ValidationError(
            "invalid_ttl",
            "ttl is out of range",
            details={"min": MIN_TTL_SECONDS, "max": settings.break_glass_max_ttl_seconds},
        )
    reason = reason.strip()
    if not reason or len(reason) > MAX_REASON_LENGTH:
        raise ValidationError(
            "invalid_reason", f"reason is required, at most {MAX_REASON_LENGTH} characters"
        )

    principal = await session.get(Principal, principal_id)
    if principal is None:
        raise NotFoundError("Principal not found", details={"principalId": str(principal_id)})
    # An agent's admin key without a human behind it is precisely what the
    # emergency path must not produce.
    if principal.kind != PrincipalKind.HUMAN:
        raise ValidationError(
            "principal_not_human",
            "Break-glass keys are issued to humans only",
            details={"kind": principal.kind},
        )
    if principal.status != PrincipalStatus.ACTIVE:
        raise ValidationError(
            "principal_not_active",
            "Cannot issue a key for a non-active principal",
            details={"status": principal.status},
        )

    now = utcnow()
    expires_at = now + timedelta(seconds=ttl_seconds)
    generated = generate_break_glass_key()
    api_key = ApiKey(
        id=new_uuid(),
        tenant_id=principal.tenant_id,
        principal_id=principal.id,
        key_prefix=generated.prefix,
        key_hash=generated.key_hash,
        permissions=[Permission.ADMIN.value],
        expires_at=expires_at,
        created_at=now,
    )
    session.add(api_key)
    await session.flush()

    await record_event(
        session,
        tenant_id=principal.tenant_id,
        event_type="api_key.break_glass_issued",
        entity_type="api_key",
        entity_id=api_key.id,
        actor_id=None,
        request_id=f"break-glass:{api_key.id}",
        payload={
            "principalId": str(principal.id),
            "keyPrefix": generated.prefix,
            "permissions": api_key.permissions,
            "expiresAt": expires_at.isoformat(),
            "ttlSeconds": ttl_seconds,
            "reason": reason,
            "issuedBy": issued_by,
        },
    )
    return IssuedBreakGlassKey(api_key=api_key, generated=generated)


async def revoke_break_glass_keys(session: AsyncSession, *, issued_by: str) -> list[ApiKey]:
    """Revoke every live break-glass key: the way out of the emergency."""
    now = utcnow()
    rows = await session.scalars(
        select(ApiKey)
        .where(ApiKey.revoked_at.is_(None), ApiKey.key_prefix.startswith("bg"))
        .with_for_update()
    )
    revoked: list[ApiKey] = []
    for api_key in rows:
        if not is_break_glass_prefix(api_key.key_prefix):
            continue
        api_key.revoked_at = now
        await record_event(
            session,
            tenant_id=api_key.tenant_id,
            event_type="api_key.revoked",
            entity_type="api_key",
            entity_id=api_key.id,
            actor_id=None,
            request_id=f"break-glass:{api_key.id}",
            payload={"keyPrefix": api_key.key_prefix, "breakGlass": True, "issuedBy": issued_by},
        )
        revoked.append(api_key)
    return revoked
