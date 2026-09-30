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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.principals import (
    get_tenant_principal,
    ungranted_permissions,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import (
    ALL_PERMISSIONS,
    AgentStatus,
    Permission,
    PrincipalKind,
    PrincipalStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.db.models import Agent, IamPrincipalBinding, Principal

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
        missing = ungranted_permissions(ctx, permissions)
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


def check_trusted_issuer(issuer: str, trusted_issuer: str) -> None:
    """Only the issuer whose tokens the core verifies can be bound.

    Enforcement accepts tokens of ``CP_IAM_ISSUER`` alone, so a binding of any
    other issuer admits nobody. On a registry agent such a call would revoke
    the working binding and leave a useless one: a denial of service, not a
    change of identity. Without a configured issuer (IAM off) there is nothing
    to compare with, and the caller passes the issuer it already trusts.
    """
    if trusted_issuer and issuer != trusted_issuer:
        raise ValidationError(
            "iam_issuer_untrusted",
            "The Control Plane does not accept tokens of this issuer",
            details={"issuer": issuer, "expected": trusted_issuer},
        )


def identity_taken(
    existing: IamPrincipalBinding | None, ctx: AuthContext, key: str
) -> ConflictError:
    """The refusal for an identity another principal holds, same for a racer.

    A concurrent writer that won the unique index gets the answer it would
    have got a moment later, read from the row that beat it.
    """
    if existing is None or existing.tenant_id != ctx.tenant_id:
        return ConflictError(
            "iam_identity_bound_elsewhere",
            "This IAM identity is already bound outside the current tenant",
        )
    return ConflictError(
        "agent_identity_conflict",
        "This IAM identity is already bound to another principal",
        details={"agent": key},
    )


async def _registry_agent_of(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> Agent | None:
    agent: Agent | None = await session.scalar(
        select(Agent).where(
            Agent.tenant_id == ctx.tenant_id,
            Agent.principal_id == principal_id,
            Agent.status != AgentStatus.RETIRED,
        )
    )
    return agent


def _registry_principal_conflict(agent: Agent) -> ConflictError:
    """The bindings of a registry agent's principal are the registry's own.

    Any identity bound here would enter with rights the revision does not set,
    and would survive ``identity:replace`` — the way back to a previous
    service identity with rights wider than the revision. Its own identity is
    no exception, for an admin neither: re-binding it would set rights beside
    the revision, and racing ``identity:replace`` would reopen the binding the
    replacement revokes. So every identity goes through the registry
    (CP-ADR-0073, amendment 2026-09-30, E4).
    """
    return ConflictError(
        "agent_identity_conflict",
        "The identity of a registry agent changes through the registry: "
        f"/agents/{agent.key}/identity (identity:replace for a service)",
        details={"agent": agent.key, "route": f"/agents/{agent.key}/identity"},
    )


async def _check_previous_owner(
    session: AsyncSession, ctx: AuthContext, *, existing_binding: IamPrincipalBinding
) -> None:
    """Moving an identity away from another principal takes it from its owner.

    Two owners are not given up by a plain upsert. An agent of the registry
    (CP-ADR-0073 §6) keeps its identity in ``agents``: moving the binding
    under it would leave the registry granting rights to a binding that is no
    longer its own, so the identity changes through the registry. Any other
    owner loses its entry: only an admin reassigns it — a human's login, and
    no less the identity of a service outside the registry.
    """
    agent_key = await session.scalar(
        select(Agent.key).where(
            Agent.tenant_id == ctx.tenant_id,
            Agent.principal_id == existing_binding.principal_id,
            Agent.iam_issuer == existing_binding.issuer,
            Agent.iam_principal_id == existing_binding.iam_principal_id,
            Agent.status != AgentStatus.RETIRED,
        )
    )
    if agent_key is not None:
        raise ConflictError(
            "agent_identity_conflict",
            "This IAM identity belongs to a registry agent; change it through the registry",
            details={"agent": agent_key},
        )
    if not ctx.has(Permission.ADMIN):
        owner = await session.get(Principal, existing_binding.principal_id)
        raise AuthorizationError(
            "Only an admin can move an identity to another principal",
            code="permission_escalation",
            details={"previousOwnerKind": owner.kind if owner is not None else None},
        )


async def _insert_new(
    session: AsyncSession, binding: IamPrincipalBinding
) -> IamPrincipalBinding | None:
    """Insert a binding of a new identity; the row that beat it, if one did.

    Two upserts of one new identity both find no row to lock, and the second
    insert hits ``uq_iam_bindings_identity`` once the first commits. Under a
    SAVEPOINT the loser keeps its transaction and reads the winner's row
    (locked, as the lookup would have locked it) instead of failing with a 500.
    """
    try:
        async with session.begin_nested():
            session.add(binding)
            await session.flush()
    except IntegrityError:
        winner: IamPrincipalBinding | None = await session.scalar(
            select(IamPrincipalBinding)
            .where(
                IamPrincipalBinding.issuer == binding.issuer,
                IamPrincipalBinding.iam_principal_id == binding.iam_principal_id,
            )
            .with_for_update()
        )
        if winner is None:
            raise  # not the identity index; nothing to answer with
        return winner
    return None


async def upsert_iam_binding(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    issuer: str,
    iam_tenant_id: uuid.UUID,
    iam_principal_id: uuid.UUID,
    permissions: list[str],
    trusted_issuer: str,
) -> UpsertedBinding:
    """Create or replace the binding of one federated identity.

    The identity ``(issuer, iam_principal_id)`` is the key: if it is already
    bound, the row is repointed and reopened rather than duplicated, so a
    revoked identity can be readmitted with one call. Taking it from another
    principal is checked against that owner first (``_check_previous_owner``);
    the principal of a registry agent takes no identity through here at all
    (``_registry_principal_conflict``). Losing a race to insert the same new
    identity is a 409 too (``_insert_new``).
    """
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    check_trusted_issuer(issuer, trusted_issuer)
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
    # Unlocked, and safely so: a principal becomes an agent's only in the
    # transaction that creates it (``PUT /agents/{key}/identity``), and every
    # call on an agent's principal is refused, so nothing here races
    # ``identity:replace`` for the agent's bindings.
    agent = await _registry_agent_of(session, ctx, principal.id)
    if agent is not None:
        raise _registry_principal_conflict(agent)

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
    previous_owner = existing.principal_id if existing is not None else None
    if existing is not None and existing.principal_id != principal.id:
        await _check_previous_owner(session, ctx, existing_binding=existing)

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
        winner = await _insert_new(session, binding)
        if winner is not None and (
            winner.tenant_id != ctx.tenant_id or winner.principal_id != principal.id
        ):
            # Bound at the same moment elsewhere: the caller is told so rather
            # than taking the identity from a principal it never saw holding it.
            raise ConflictError(
                "iam_identity_bound_elsewhere",
                "This IAM identity was bound to another principal at the same time",
            )
        # The same identity on the same principal: the winner's row is the one
        # this call would have found a moment later, and it is updated below.
        existing = winner
    if existing is None:
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

    payload: dict[str, object] = {
        "principalId": str(binding.principal_id),
        "issuer": binding.issuer,
        "iamTenantId": str(binding.iam_tenant_id),
        "iamPrincipalId": str(binding.iam_principal_id),
        "permissions": binding.permissions,
    }
    if previous_owner is not None and previous_owner != binding.principal_id:
        payload["previousPrincipalId"] = str(previous_owner)
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
        payload=payload,
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
