"""Disabling a principal: one call that takes a person or an agent out of the system.

Everything that lets the principal act goes in the same transaction
(CP-ADR-0077): the status, its IAM bindings, the delegations it is a side of,
its open sessions and those opened on its behalf, the claims they hold, the
runs on those claims and its live skill calls. A half-disabled principal —
locked out but still holding tasks, or with a live binding and a ``disabled``
label — is exactly what an operator reaching for this button cannot afford.

Lock order: agent record (``FOR SHARE``; ``agents/{key}:retire`` and
``:link-identity`` take it ``FOR UPDATE``), then the principal ``FOR UPDATE``
together with the caller's ``FOR KEY SHARE`` in id order, bindings,
delegations, sessions, then task → claim → run as in ``_claim_release``, and
skill call rows last. Every other writer keeps to the same order by the rules
of ``application/locking.py`` (CP-ADR-0077 §3): its caller's principal first
(the write flow), the session of a run or claim before its task, any other
principal it references before the task too. A writer therefore either waits
on the first lock ``:disable`` takes from it, holding nothing ``:disable``
needs, or makes ``:disable`` wait there — and then finds the principal
disabled (``principal_not_active``) once the lock is released.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands._claim_release import (
    release_active_claims_for_session,
    release_active_claims_of_holder,
)
from control_plane.application.commands.child_runs import record_child_result
from control_plane.application.commands.iam_bindings import BINDING_STATUS_REVOKED
from control_plane.application.commands.skill_invocations import withdraw_principal_invocations
from control_plane.application.commands.tasks import supersede_run
from control_plane.application.common import utcnow
from control_plane.application.events import event_reason, record_event
from control_plane.application.locking import lock_caller_and_principal_for_update
from control_plane.domain.enums import (
    AgentStatus,
    Permission,
    PrincipalKind,
    PrincipalStatus,
    RunStatus,
    SessionStatus,
)
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.db.models import (
    Agent,
    ApiKey,
    Delegation,
    IamPrincipalBinding,
    Principal,
    Run,
    Session,
    Task,
)

DISABLE_REASON = "principal_disabled"

# Services are not people or agents: the core's own principal lives among
# them, and switching it off would stop core writes, not a participant.
DISABLEABLE_KINDS = frozenset({PrincipalKind.HUMAN.value, PrincipalKind.AGENT.value})


@dataclass(frozen=True)
class DisabledPrincipal:
    principal: Principal
    changed: bool
    # Identities whose enforcement cache the API drops after commit.
    touched_identities: list[tuple[str, uuid.UUID]] = field(default_factory=list)


async def load_target(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID, *, lock: bool
) -> tuple[Sequence[Agent], Principal]:
    """The target principal and its agent records, in the lock order of CP-ADR-0077 §3.

    With ``lock`` — the agent records ``FOR SHARE`` (``:retire`` and
    ``:link-identity`` take them ``FOR UPDATE``), then the principal ``FOR
    UPDATE`` together with the caller's ``FOR KEY SHARE`` in id order (the
    write flow leaves the caller to this command). Without — plain reads, for
    ``POST /authz:check``.
    """
    query = (
        select(Agent)
        .where(Agent.tenant_id == ctx.tenant_id, Agent.principal_id == principal_id)
        .order_by(Agent.id)
    )
    agents = (await session.scalars(query.with_for_update(read=True) if lock else query)).all()
    if lock:
        principal = await lock_caller_and_principal_for_update(session, ctx, principal_id)
    else:
        principal = await session.scalar(
            select(Principal).where(
                Principal.id == principal_id, Principal.tenant_id == ctx.tenant_id
            )
        )
    if principal is None:
        raise NotFoundError("Principal not found", details={"principalId": str(principal_id)})
    return agents, principal


@dataclass(frozen=True)
class DisableGate:
    principal: Principal
    # False: already disabled, the call is a repeat and changes nothing.
    changed: bool
    # Bindings not revoked (``FOR UPDATE`` under ``lock``); empty on a repeat.
    bindings: Sequence[IamPrincipalBinding] = ()


async def disable_gate(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID, *, lock: bool = False
) -> DisableGate:
    """Everything that decides whether ``ctx`` may disable the principal.

    The command goes through here with ``lock``; ``POST /authz:check`` asks
    the same question without row locks (CP-ADR-0055, amendment of
    2026-09-29). A repeat on a disabled principal passes: the endpoint
    answers it with ``200``.
    """
    await authorize(ctx, Permission.PRINCIPALS_WRITE)
    agents, principal = await load_target(session, ctx, principal_id, lock=lock)
    if principal.status == PrincipalStatus.DISABLED:
        return DisableGate(principal=principal, changed=False)  # idempotent
    if principal.kind not in DISABLEABLE_KINDS:
        raise ValidationError(
            "principal_kind_not_disableable",
            "Only a human or an agent principal can be disabled",
            details={"kind": principal.kind},
        )
    if principal.id == ctx.principal_id:
        raise ConflictError(
            "cannot_disable_self",
            "A principal cannot disable itself",
            details={"principalId": str(principal.id)},
        )
    query = (
        select(IamPrincipalBinding)
        .where(
            IamPrincipalBinding.tenant_id == ctx.tenant_id,
            IamPrincipalBinding.principal_id == principal.id,
            IamPrincipalBinding.status != BINDING_STATUS_REVOKED,
        )
        .order_by(IamPrincipalBinding.id)
    )
    bindings = (await session.scalars(query.with_for_update() if lock else query)).all()
    if not ctx.has(Permission.ADMIN) and await holds_admin(session, principal, bindings):
        # The mirror of "only an admin makes an admin": taking one away is
        # no less a change of who administers the tenant.
        raise AuthorizationError(
            "Only an admin can disable a principal that holds admin",
            code="permission_escalation",
            details={"missing": [Permission.ADMIN.value]},
        )
    # After the admin check: the refusal names the agent, and a caller who may
    # not disable an admin learns nothing about the admin's agent record.
    agent = next((a for a in agents if a.status != AgentStatus.RETIRED), None)
    if agent is not None:
        # The registry would bring the binding back on the next publish; the
        # agent leaves through its own record.
        raise ConflictError(
            "use_agent_retire",
            "The principal belongs to a registered agent: retire the agent instead",
            details={"principalId": str(principal.id), "agent": agent.key},
        )
    return DisableGate(principal=principal, changed=True, bindings=bindings)


async def disable_principal(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    reason: str | None = None,
) -> DisabledPrincipal:
    gate = await disable_gate(session, ctx, principal_id, lock=True)
    principal, bindings = gate.principal, gate.bindings
    if not gate.changed:
        return DisabledPrincipal(principal=principal, changed=False)

    now = utcnow()
    previous_status = principal.status
    principal.status = PrincipalStatus.DISABLED
    principal.updated_at = now

    touched: list[tuple[str, uuid.UUID]] = []
    for binding in bindings:
        binding.status = BINDING_STATUS_REVOKED
        binding.revoked_at = now
        binding.updated_at = now
        touched.append((binding.issuer, binding.iam_principal_id))
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

    delegations = (
        await session.scalars(
            select(Delegation)
            .where(
                Delegation.tenant_id == ctx.tenant_id,
                or_(
                    Delegation.human_principal_id == principal.id,
                    Delegation.agent_principal_id == principal.id,
                ),
                Delegation.revoked_at.is_(None),
            )
            .order_by(Delegation.id)
            .with_for_update()
        )
    ).all()
    for delegation in delegations:
        delegation.revoked_at = now
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
            payload={"reason": DISABLE_REASON},
        )

    # Its own sessions and those an agent opened on its behalf. Sessions are
    # locked before any task row and before any skill call row (lock order;
    # an executor's ``_locked_own_lease`` takes its session first too), then
    # each one gives up its own claims so ``session.closed`` reports them.
    work_sessions = (
        await session.scalars(
            select(Session)
            .where(
                Session.tenant_id == ctx.tenant_id,
                or_(Session.principal_id == principal.id, Session.on_behalf_of_id == principal.id),
                Session.status != SessionStatus.CLOSED,
            )
            .order_by(Session.id)
            .with_for_update()
        )
    ).all()
    for work_session in work_sessions:
        work_session.status = SessionStatus.CLOSED
        work_session.ended_at = work_session.ended_at or now
    released: list[uuid.UUID] = []
    for work_session in work_sessions:
        of_session = await release_active_claims_for_session(
            session,
            tenant_id=ctx.tenant_id,
            session_id=work_session.id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            reason=DISABLE_REASON,
        )
        released.extend(of_session)
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
            payload={"releasedClaims": [str(c) for c in of_session], "reason": DISABLE_REASON},
        )
    # Claims held by the principal through a session that is not its own
    # (none today, but the claim row does not forbid it) go back too.
    released.extend(
        await release_active_claims_of_holder(
            session,
            tenant_id=ctx.tenant_id,
            holder_id=principal.id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            reason=DISABLE_REASON,
        )
    )

    failed_runs = await _fail_runs_of_claims(session, ctx, released)
    withdrawn = await withdraw_principal_invocations(
        session, ctx, principal_id=principal.id, reason=DISABLE_REASON
    )

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="principal.disabled",
        entity_type="principal",
        entity_id=principal.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "kind": principal.kind,
            "previousStatus": previous_status,
            "reason": event_reason(reason) if reason else None,
            "revokedBindings": len(bindings),
            "revokedDelegations": len(delegations),
            "closedSessions": len(work_sessions),
            "releasedClaims": len(released),
            "failedRuns": failed_runs,
            "withdrawnInvocations": withdrawn,
        },
    )
    return DisabledPrincipal(principal=principal, changed=True, touched_identities=touched)


async def holds_admin(
    session: AsyncSession, principal: Principal, bindings: Sequence[IamPrincipalBinding]
) -> bool:
    """Admin in a binding not revoked or in a live (not revoked, not expired) API key.

    An expired key does not authenticate, so it administers nothing: the same
    rule as the escalation check of ``:enable`` (CP-ADR-0077).
    """
    if any(Permission.ADMIN.value in binding.permissions for binding in bindings):
        return True
    now = utcnow()
    keys = await session.scalars(
        select(ApiKey.permissions).where(
            ApiKey.tenant_id == principal.tenant_id,
            ApiKey.principal_id == principal.id,
            ApiKey.revoked_at.is_(None),
            or_(ApiKey.expires_at.is_(None), ApiKey.expires_at > now),
        )
    )
    return any(Permission.ADMIN.value in permissions for permissions in keys)


async def _fail_runs_of_claims(
    session: AsyncSession, ctx: AuthContext, claim_ids: list[uuid.UUID]
) -> int:
    """Fail the running runs of the claims just released.

    Nobody is left to finish them: their holder cannot enter, and a run whose
    claim is gone would otherwise stay ``running`` until the next claim of its
    task superseded it. Task and claim rows are already locked by the release,
    so the run rows come next in the usual order.
    """
    if not claim_ids:
        return 0
    runs = (
        await session.scalars(
            select(Run)
            .where(
                Run.tenant_id == ctx.tenant_id,
                Run.claim_id.in_(claim_ids),
                Run.status == RunStatus.RUNNING,
            )
            .order_by(Run.task_id)
            .with_for_update()
        )
    ).all()
    for run in runs:
        task = await session.get(Task, run.task_id)
        assert task is not None  # FK
        await supersede_run(session, ctx, task, run, reason=DISABLE_REASON)
        # A child run still owes its parent a result.
        await record_child_result(
            session, ctx, run=run, outcome="failed", output=None, failure_reason=DISABLE_REASON
        )
    return len(runs)
