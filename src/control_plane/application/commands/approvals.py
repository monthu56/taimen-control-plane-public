"""Approval commands: minimal human/agent-in-the-loop governance primitive.

One approval record = one decision. The decision is atomic: the row is locked
``FOR UPDATE`` and only a ``pending`` approval can transition, so two
concurrent deciders produce exactly one terminal outcome.

Deciding requires BOTH: the ``approvals.decide`` API permission AND
organizational eligibility (being the assigned principal, or holding the
required role in the approval's workspace scope).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.approval_outcomes import (
    OUTCOME_PENDING,
    decision_authority,
    declared_actions,
)
from control_plane.application.commands.approval_preconditions import require_preconditions
from control_plane.application.commands.org import get_tenant_role
from control_plane.application.commands.principals import get_tenant_principal
from control_plane.application.commands.relations import resolve_task
from control_plane.application.commands.tasks import resolve_task_for_update
from control_plane.application.commands.verification import wake_on_decision
from control_plane.application.commands.workspaces import (
    get_tenant_workspace,
    workspace_ancestor_ids,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.queries.approval_gates import pending_gate_approvals
from control_plane.application.queries.org import role_assignment_scope
from control_plane.domain.enums import ApprovalStatus, Permission
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.event_catalog import PAYLOAD_TEXT_LIMIT
from control_plane.domain.redaction import redact_secret_material
from control_plane.domain.work_item import TERMINAL_CATEGORIES
from control_plane.infrastructure.db.models import Approval, Artifact, PrincipalRole, Task


def event_comment(comment: str | None) -> str | None:
    """A comment as it may travel in an event (CP-ADR-0068): credential-shaped
    material redacted, cut to the payload text limit."""
    if comment is None:
        return None
    text = redact_secret_material(comment)
    if len(text) > PAYLOAD_TEXT_LIMIT:
        text = text[: PAYLOAD_TEXT_LIMIT - 1] + "\u2026"
    return text


async def request_approval(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str | None = None,
    artifact_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    required_role_id: uuid.UUID | None = None,
    assigned_principal_id: uuid.UUID | None = None,
    comment: str = "",
    gate: bool = False,
) -> Approval:
    await authorize(ctx, Permission.APPROVALS_MANAGE)
    if (required_role_id is None) == (assigned_principal_id is None):
        raise ValidationError(
            "invalid_approval",
            "Exactly one of requiredRoleId or assignedPrincipalId must be set",
        )
    if gate and task_ref is None:
        raise ValidationError(
            "invalid_approval",
            "A gate approval must reference the task it gates",
        )

    task_id: uuid.UUID | None = None
    task: Task | None = None
    if task_ref is not None:
        # A gate must serialize with the commands it gates: taking the task
        # row lock (first in the global order, nothing else is locked here)
        # means an in-flight :complete either finishes before the gate exists
        # or blocks until it does — no gate can attach to a task that is
        # concurrently becoming terminal.
        task = (
            await resolve_task_for_update(session, ctx, task_ref)
            if gate
            else (await resolve_task(session, ctx, task_ref))
        )
        task_id = task.id
        # A gate on a terminal task is inert (done/cancelled cannot be
        # claimed or completed) and only muddies discovery/audit — reject it.
        if gate and task.system_status_category in TERMINAL_CATEGORIES:
            raise ValidationError(
                "invalid_approval",
                f"Cannot gate a task in status '{task.status}'",
                details={"taskId": str(task_id), "status": task.status},
            )
    if artifact_id is not None:
        artifact = await session.scalar(
            select(Artifact).where(Artifact.id == artifact_id, Artifact.tenant_id == ctx.tenant_id)
        )
        if artifact is None:
            raise NotFoundError("Artifact not found", details={"artifactId": str(artifact_id)})
    if workspace_id is not None:
        await get_tenant_workspace(session, ctx, workspace_id)
    if task is not None:
        workspace_id = await _task_approval_workspace(session, ctx, task, workspace_id)
    if required_role_id is not None:
        await get_tenant_role(session, ctx, required_role_id)
    if assigned_principal_id is not None:
        await get_tenant_principal(session, ctx, assigned_principal_id)

    now = utcnow()
    approval = Approval(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        task_id=task_id,
        artifact_id=artifact_id,
        requested_by_principal_id=ctx.principal_id,
        status=ApprovalStatus.PENDING,
        gate=gate,
        required_role_id=required_role_id,
        assigned_principal_id=assigned_principal_id,
        comment=comment,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(approval)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="approval.requested",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task_id) if task_id else None,
            "artifactId": str(artifact_id) if artifact_id else None,
            "requiredRoleId": str(required_role_id) if required_role_id else None,
            "assignedPrincipalId": str(assigned_principal_id) if assigned_principal_id else None,
            "gate": gate,
            # v2 (CP-ADR-0068): enough to tell a person what to decide without a read.
            # The approval's own workspace: the one its eligibility is checked in.
            "workspaceId": str(workspace_id) if workspace_id else None,
            "taskPublicId": task.public_id if task is not None else None,
            "taskTitle": task.title if task is not None else None,
            "requestedBy": str(ctx.principal_id),
            "comment": event_comment(comment),
        },
    )
    return approval


async def _task_approval_workspace(
    session: AsyncSession, ctx: AuthContext, task: Task, workspace_id: uuid.UUID | None
) -> uuid.UUID | None:
    """Workspace of an approval about ``task`` (CP-ADR-0068).

    Without an explicit one the approval lives in the task's workspace, so a
    role granted there is enough to decide. An explicit one may only widen the
    scope: the task's workspace itself or one of its ancestors.
    """
    if workspace_id is None or workspace_id == task.workspace_id:
        return task.workspace_id
    ancestors = (
        await workspace_ancestor_ids(session, ctx.tenant_id, task.workspace_id)
        if task.workspace_id is not None
        else []
    )
    if workspace_id not in ancestors:
        raise ValidationError(
            "invalid_approval",
            "The approval's workspace must be the task's workspace or one of its ancestors",
            details={
                "taskId": str(task.id),
                "taskWorkspaceId": str(task.workspace_id) if task.workspace_id else None,
                "workspaceId": str(workspace_id),
            },
        )
    return workspace_id


async def check_approval_gate(session: AsyncSession, ctx: AuthContext, task_id: uuid.UUID) -> None:
    """Raise 409 approval_required while a pending gate approval holds the task.

    Called inside the claiming/completing transaction under the task row lock:
    an uncommitted approval decision is invisible here, so the gate opens only
    after the decision has committed.
    """
    pending = await pending_gate_approvals(session, ctx.tenant_id, task_id)
    if pending:
        raise ConflictError(
            "approval_required",
            "Task is waiting for a pending gate approval",
            details={"taskId": str(task_id), "pendingApprovals": pending},
        )


async def _get_locked_pending_approval(
    session: AsyncSession, ctx: AuthContext, approval_id: uuid.UUID
) -> Approval:
    approval = await session.scalar(
        select(Approval)
        .where(Approval.id == approval_id, Approval.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if approval is None:
        raise NotFoundError("Approval not found", details={"approvalId": str(approval_id)})
    if approval.status != ApprovalStatus.PENDING:
        raise ConflictError(
            "approval_already_decided",
            "Approval is no longer pending",
            details={"approvalId": str(approval_id), "status": approval.status},
        )
    return approval


async def _require_decision_eligibility(
    session: AsyncSession, ctx: AuthContext, approval: Approval
) -> None:
    """Organizational check: assigned principal, or holder of the required role."""
    if approval.assigned_principal_id is not None:
        if approval.assigned_principal_id != ctx.principal_id:
            raise AuthorizationError(
                "Approval is assigned to another principal",
                code="not_eligible",
                details={"approvalId": str(approval.id)},
            )
        return

    scope_filter = await role_assignment_scope(session, ctx.tenant_id, approval.workspace_id)
    held = await session.scalar(
        select(PrincipalRole.id).where(
            PrincipalRole.principal_id == ctx.principal_id,
            PrincipalRole.role_id == approval.required_role_id,
            scope_filter,
        )
    )
    if held is None:
        raise AuthorizationError(
            "Deciding requires the approval's required role",
            code="not_eligible",
            details={
                "approvalId": str(approval.id),
                "requiredRoleId": str(approval.required_role_id),
            },
        )


async def decide_approval(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    approval_id: uuid.UUID,
    approve: bool,
    comment: str | None = None,
) -> Approval:
    target = ResourceRef("approval", str(approval_id))
    if ctx.purpose_ref is not None and ctx.purpose_ref != target.key:
        # Checked before the row is read: a credential bound to one decision
        # learns nothing about any other approval (CP-ADR-0070).
        raise AuthorizationError(
            "This credential decides only the approval it was issued for",
            code="outside_purpose",
        )
    await authorize(ctx, Permission.APPROVALS_DECIDE)
    approval = await _get_locked_pending_approval(session, ctx, approval_id)
    await authorize(ctx, Permission.APPROVALS_DECIDE, resource=target)
    await _require_decision_eligibility(session, ctx, approval)
    # What the type declares must hold before an approve (TAI-ADR-0041 p.7):
    # refused, the decision is not recorded and the gate stays pending.
    if approve:
        await require_preconditions(session, ctx, approval)

    now = utcnow()
    approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    approval.decision_by_principal_id = ctx.principal_id
    approval.decision_at = now
    if comment is not None:
        approval.comment = comment
    # A gate on a task whose type declares outcomes for this decision: the
    # worker executes them after commit, with the authority of THIS credential
    # (CP-ADR-0061), or of its binding for a channel decision (CP-ADR-0070).
    # Otherwise nothing is pending — as before that ADR.
    if await declared_actions(session, approval):
        approval.outcome_status = OUTCOME_PENDING
        approval.decision_authority = await decision_authority(session, ctx)
        approval.outcome_next_attempt_at = now
    approval.version += 1
    approval.updated_at = now
    # A verification attempt waiting on this gate looks at it now (CP-ADR-0067).
    await wake_on_decision(session, approval)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="approval.approved" if approve else "approval.rejected",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(approval.task_id) if approval.task_id else None,
            "artifactId": str(approval.artifact_id) if approval.artifact_id else None,
            "outcomeStatus": approval.outcome_status,
            "decisionBy": str(ctx.principal_id),
            "comment": event_comment(comment),
            # The credential's channel of entry (CP-ADR-0070); a direct API
            # call has none.
            "channel": ctx.channel,
        },
    )
    return approval


async def cancel_approval(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    approval_id: uuid.UUID,
    comment: str | None = None,
) -> Approval:
    await authorize(ctx, Permission.APPROVALS_MANAGE)
    approval = await session.scalar(
        select(Approval)
        .where(Approval.id == approval_id, Approval.tenant_id == ctx.tenant_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if approval is None:
        raise NotFoundError("Approval not found", details={"approvalId": str(approval_id)})
    if approval.status == ApprovalStatus.CANCELLED:
        return approval  # idempotent
    if approval.status != ApprovalStatus.PENDING:
        raise ConflictError(
            "approval_already_decided",
            "Approval is no longer pending",
            details={"approvalId": str(approval_id), "status": approval.status},
        )
    # A GATE approval is an enforcement primitive (blocks claim/complete):
    # cancelling it opens the gate exactly like a decision. So cancelling a
    # foreign gate requires the FULL authority to decide it — both the
    # approvals.decide permission and organizational eligibility — otherwise
    # the very principal the gate is meant to hold could void it with only
    # approvals.manage. The requester may always cancel its own request.
    if approval.gate and approval.requested_by_principal_id != ctx.principal_id:
        await authorize(ctx, Permission.APPROVALS_DECIDE)
        await _require_decision_eligibility(session, ctx, approval)

    now = utcnow()
    approval.status = ApprovalStatus.CANCELLED
    if comment is not None:
        approval.comment = comment
    approval.version += 1
    approval.updated_at = now
    await wake_on_decision(session, approval)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="approval.cancelled",
        entity_type="approval",
        entity_id=approval.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(approval.task_id) if approval.task_id else None,
            "cancelledBy": str(ctx.principal_id),
        },
    )
    return approval
