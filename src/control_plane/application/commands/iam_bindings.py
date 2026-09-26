"""IAM identity bindings: the management surface over ``iam_principal_bindings``.

A binding is the IAM-era counterpart of an API key: it is where a federated
identity gets its Control Plane permissions (ADR-0053). So the rules that
guard key issuance apply here unchanged — the caller cannot hand out more than
it holds, and only an admin can make another admin. What is new is a rule
about the target: a non-human principal is never given the two rights that
exist to keep a human in the loop.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.principals import get_tenant_principal
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import ALL_PERMISSIONS, Permission, PrincipalKind, PrincipalStatus
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.db.models import IamPrincipalBinding

# Rights that presuppose a human decision. An agent or a service that held
# ``admin`` could rewrite its own binding; one that held ``approvals.decide``
# would approve the very gate meant to stop it.
HUMAN_ONLY_PERMISSIONS = frozenset({Permission.ADMIN.value, Permission.APPROVALS_DECIDE.value})

BINDING_STATUS_ACTIVE = "active"
BINDING_STATUS_REVOKED = "revoked"


@dataclass(frozen=True)
class UpsertedBinding:
    binding: IamPrincipalBinding
    created: bool


def validate_binding_permissions(
    ctx: AuthContext, *, permissions: list[str], principal_kind: str
) -> list[str]:
    """The same three checks an API key goes through, plus the kind rule."""
    unknown = sorted(set(permissions) - ALL_PERMISSIONS)
    if unknown:
        raise ValidationError(
            "invalid_permissions",
            "Unknown permissions requested",
            details={"unknown": unknown},
        )
    if not permissions:
        raise ValidationError("invalid_permissions", "permissions must not be empty")
    # No privilege escalation: a binding can only grant permissions its creator
    # holds (admin holds everything).
    if not ctx.has(Permission.ADMIN):
        if Permission.ADMIN.value in permissions:
            raise ValidationError(
                "invalid_permissions", "Only an admin can bind an identity as admin"
            )
        missing = sorted(set(permissions) - set(ctx.permissions))
        if missing:
            raise AuthorizationError(
                "Cannot grant permissions the calling credential does not hold",
                code="permission_escalation",
                details={"missing": missing},
            )
    if principal_kind != PrincipalKind.HUMAN:
        forbidden = sorted(HUMAN_ONLY_PERMISSIONS & set(permissions))
        if forbidden:
            raise ValidationError(
                "permissions_not_allowed_for_kind",
                f"A principal of kind {principal_kind!r} cannot hold human-only permissions",
                details={"kind": principal_kind, "forbidden": forbidden},
            )
    return sorted(set(permissions))


async def upsert_iam_binding(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    issuer: str,
    iam_tenant_id: uuid.UUID,
    iam_principal_id: uuid.UUID,
    permissions: list[str],
) -> UpsertedBinding:
    """Create or replace the binding of one federated identity.

    The identity ``(issuer, iam_principal_id)`` is the key: if it is already
    bound, the row is repointed and reopened rather than duplicated, so a
    revoked identity can be readmitted with one call.
    """
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    principal = await get_tenant_principal(session, ctx, principal_id)
    if principal.status != PrincipalStatus.ACTIVE:
        raise ValidationError(
            "principal_not_active",
            "Cannot bind an identity to a non-active principal",
            details={"status": principal.status},
        )
    granted = validate_binding_permissions(
        ctx, permissions=permissions, principal_kind=principal.kind
    )

    now = utcnow()
    existing = await session.scalar(
        select(IamPrincipalBinding)
        .where(
            IamPrincipalBinding.issuer == issuer,
            IamPrincipalBinding.iam_principal_id == iam_principal_id,
        )
        .with_for_update()
    )
    if existing is not None and existing.tenant_id != ctx.tenant_id:
        # The pair is unique across tenants by design; a foreign row is not
        # ours to repoint, and saying so is not a leak — the caller already
        # knows the identity it asked about.
        raise ConflictError(
            "iam_identity_bound_elsewhere",
            "This IAM identity is already bound outside the current tenant",
        )

    if existing is None:
        binding = IamPrincipalBinding(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            principal_id=principal.id,
            issuer=issuer,
            iam_tenant_id=iam_tenant_id,
            iam_principal_id=iam_principal_id,
            permissions=granted,
            status=BINDING_STATUS_ACTIVE,
            revoked_at=None,
            last_used_at=None,
            created_at=now,
            updated_at=now,
        )
        session.add(binding)
        await session.flush()
        event_type = "iam_binding.created"
    else:
        binding = existing
        binding.principal_id = principal.id
        binding.iam_tenant_id = iam_tenant_id
        binding.permissions = granted
        binding.status = BINDING_STATUS_ACTIVE
        binding.revoked_at = None
        binding.updated_at = now
        await session.flush()
        event_type = "iam_binding.updated"

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        entity_type="iam_binding",
        entity_id=binding.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "principalId": str(binding.principal_id),
            "issuer": binding.issuer,
            "iamTenantId": str(binding.iam_tenant_id),
            "iamPrincipalId": str(binding.iam_principal_id),
            "permissions": binding.permissions,
        },
    )
    return UpsertedBinding(binding=binding, created=existing is None)


async def revoke_iam_binding(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    binding_id: uuid.UUID,
) -> IamPrincipalBinding:
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    binding = await session.scalar(
        select(IamPrincipalBinding)
        .where(IamPrincipalBinding.id == binding_id, IamPrincipalBinding.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if binding is None:
        raise NotFoundError("IAM binding not found", details={"bindingId": str(binding_id)})
    if binding.status == BINDING_STATUS_REVOKED:
        return binding  # idempotent

    now = utcnow()
    binding.status = BINDING_STATUS_REVOKED
    binding.revoked_at = now
    binding.updated_at = now
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="iam_binding.revoked",
        entity_type="iam_binding",
        entity_id=binding.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "principalId": str(binding.principal_id),
            "issuer": binding.issuer,
            "iamPrincipalId": str(binding.iam_principal_id),
        },
    )
    return binding
