"""Shared claim-release helper.

Lock ordering discipline (deadlock avoidance), everywhere in the codebase:
task row first, then claim row. Sessions are never locked after tasks.
"""

import uuid

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.common import utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import ClaimStatus
from control_plane.domain.work_item import WorkItemLifecycle
from control_plane.infrastructure.db.models import Task, TaskClaim


def release_claim_on_locked_task(
    task: Task,
    claim: TaskClaim,
    *,
    reason: str,
    new_status: ClaimStatus = ClaimStatus.RELEASED,
    lifecycle: WorkItemLifecycle | None = None,
) -> None:
    """Mutate a (locked) claim + task pair: release the claim, detach the task.

    The caller holds FOR UPDATE locks on both rows and is responsible for
    version bump and event recording.

    ``lifecycle`` is what pre-v0.8's hardcoded ``in_progress -> todo`` became
    (ADR-0048). Passing None means "do not touch the status" and is the right
    answer wherever the caller overwrites it anyway (completion) or re-claims
    immediately (claim takeover) — the type lookup would be dead work on the
    hot path.
    """
    now = utcnow()
    claim.status = new_status
    claim.released_at = now
    claim.release_reason = reason
    if task.active_claim_id == claim.id:
        task.active_claim_id = None
        apply_release_status(task, lifecycle)
        task.updated_at = now


def apply_claim_status(task: Task, lifecycle: WorkItemLifecycle) -> None:
    """Move a freshly claimed task to its type's ``claimStatus``, if declared.

    The transition is applied only along a DECLARED edge. Refusing a claim
    because a tenant did not declare ``blocked -> in_progress`` would turn a
    cosmetic gap in configuration into an outage: the claim, not the label on
    it, is the authoritative coordination gate (SPEC §4.4).
    """
    if lifecycle.claim_status is None:
        return
    if not lifecycle.allows(task.status, lifecycle.claim_status):
        return
    task.status = lifecycle.claim_status
    task.system_status_category = lifecycle.category_of(lifecycle.claim_status)


def apply_release_status(task: Task, lifecycle: WorkItemLifecycle | None) -> None:
    """Move a released task back to its type's ``releaseStatus``, if declared.

    Two guards, both inherited from the pre-v0.8 behaviour they replace:
    the task must currently sit in the status the CLAIM put it in (releasing a
    claim must not drag a task out of a state a human set by hand), and the
    edge must be declared — an undeclared edge leaves the status alone rather
    than failing the release (SPEC §4.4).
    """
    if lifecycle is None or lifecycle.claim_status is None or lifecycle.release_status is None:
        return
    if task.status != lifecycle.claim_status:
        return
    if not lifecycle.allows(lifecycle.claim_status, lifecycle.release_status):
        return
    task.status = lifecycle.release_status
    task.system_status_category = lifecycle.category_of(lifecycle.release_status)


async def release_active_claims_for_session(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    request_id: str,
    correlation_id: str,
    trace_run_id: str = "",
    reason: str,
) -> list[uuid.UUID]:
    """Release every active claim held by a session (used on session close).

    Returns the released claim ids.
    """
    return await _release_active_claims(
        session,
        tenant_id=tenant_id,
        condition=TaskClaim.session_id == session_id,
        actor_id=actor_id,
        request_id=request_id,
        correlation_id=correlation_id,
        trace_run_id=trace_run_id,
        reason=reason,
    )


async def release_active_claims_of_holder(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    holder_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    request_id: str,
    correlation_id: str,
    trace_run_id: str = "",
    reason: str,
) -> list[uuid.UUID]:
    """Release every active claim held by a principal, whatever its session.

    Used when the principal itself leaves (a retired agent, CP-ADR-0073 §9):
    its work goes back to the queue under the ordinary release rules.
    """
    return await _release_active_claims(
        session,
        tenant_id=tenant_id,
        condition=TaskClaim.holder_id == holder_id,
        actor_id=actor_id,
        request_id=request_id,
        correlation_id=correlation_id,
        trace_run_id=trace_run_id,
        reason=reason,
    )


async def _release_active_claims(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    condition: ColumnElement[bool],
    actor_id: uuid.UUID | None,
    request_id: str,
    correlation_id: str,
    trace_run_id: str,
    reason: str,
) -> list[uuid.UUID]:
    rows = (
        await session.execute(
            select(TaskClaim.id, TaskClaim.task_id)
            .where(
                condition,
                TaskClaim.tenant_id == tenant_id,
                TaskClaim.status == ClaimStatus.ACTIVE,
            )
            .order_by(TaskClaim.task_id)
        )
    ).all()

    released: list[uuid.UUID] = []
    for claim_id, task_id in rows:
        task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
        claim = await session.scalar(
            select(TaskClaim).where(TaskClaim.id == claim_id).with_for_update()
        )
        if task is None or claim is None or claim.status != ClaimStatus.ACTIVE:
            continue  # re-check after acquiring locks: someone else got here first
        release_claim_on_locked_task(
            task, claim, reason=reason, lifecycle=await lifecycle_of(session, task)
        )
        task.version += 1
        await record_event(
            session,
            tenant_id=tenant_id,
            event_type="claim.released",
            entity_type="claim",
            entity_id=claim.id,
            actor_id=actor_id,
            session_id=claim.session_id,
            request_id=request_id,
            correlation_id=correlation_id,
            trace_run_id=trace_run_id,
            payload={
                "taskId": str(task.id),
                "reason": reason,
                "taskStatus": task.status,
                "taskSystemStatusCategory": task.system_status_category,
            },
        )
        released.append(claim.id)
    return released
