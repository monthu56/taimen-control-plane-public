"""Durable Active Turn Control commands (TASK-000003)."""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands._claim_release import release_claim_on_locked_task
from control_plane.application.commands.child_runs import (
    cascade_cooperative_cancel,
    record_child_result,
)
from control_plane.application.commands.runs import (
    _get_tenant_run,
    _lock_task_then_run,
    _require_run_running,
)
from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.commands.tasks import enforce_claim_gate
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import (
    Permission,
    RunControlOperation,
    RunControlStatus,
    RunStatus,
)
from control_plane.domain.errors import AuthorizationError, ConflictError, ValidationError
from control_plane.infrastructure.db.models import (
    Run,
    RunControlMessage,
    Task,
    TaskClaim,
    TaskRelation,
)


async def _cascade_force_cancel_children(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    root_task_id: uuid.UUID,
    root_message_id: uuid.UUID,
    causal_position: str,
    reason: str,
) -> None:
    """Cancel active spawned descendants and attach an immutable derived message."""
    relations = (
        await session.execute(
            select(TaskRelation.from_task_id, TaskRelation.to_task_id).where(
                TaskRelation.tenant_id == ctx.tenant_id,
                TaskRelation.relation_type == "spawned_by",
            )
        )
    ).all()
    children_by_parent: dict[uuid.UUID, set[uuid.UUID]] = {}
    for child_id, parent_id in relations:
        children_by_parent.setdefault(parent_id, set()).add(child_id)
    descendants: set[uuid.UUID] = set()
    frontier = [root_task_id]
    while frontier:
        parent_id = frontier.pop()
        for child_id in children_by_parent.get(parent_id, set()):
            if child_id not in descendants:
                descendants.add(child_id)
                frontier.append(child_id)

    for child_task_id in sorted(descendants, key=str):
        child_task = await session.scalar(
            select(Task).where(Task.id == child_task_id).with_for_update()
        )
        if child_task is None:  # pragma: no cover - relation FK guarantees existence
            continue
        child_run_probe = await session.scalar(
            select(Run).where(
                Run.task_id == child_task.id,
                Run.tenant_id == ctx.tenant_id,
                Run.status == RunStatus.RUNNING,
            )
        )
        if child_run_probe is None:
            continue
        child_claim = await session.scalar(
            select(TaskClaim).where(TaskClaim.id == child_run_probe.claim_id).with_for_update()
        )
        child_run = await session.scalar(
            select(Run)
            .where(Run.id == child_run_probe.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if child_claim is None or child_run is None or child_run.status != RunStatus.RUNNING:
            continue

        last_seq = (
            await session.scalar(
                select(func.max(RunControlMessage.seq)).where(
                    RunControlMessage.run_id == child_run.id
                )
            )
        ) or 0
        now = utcnow()
        pending = (
            await session.scalars(
                select(RunControlMessage)
                .where(
                    RunControlMessage.run_id == child_run.id,
                    RunControlMessage.status == RunControlStatus.ACCEPTED,
                )
                .order_by(RunControlMessage.seq)
                .with_for_update()
            )
        ).all()
        for older in pending:
            older.status = RunControlStatus.SUPERSEDED
            older.safe_boundary = "server:parent_force_cancel"
            older.acknowledged_by_principal_id = ctx.principal_id
            older.version += 1
            older.resolved_at = now
        derived = RunControlMessage(
            id=new_uuid(),
            tenant_id=ctx.tenant_id,
            run_id=child_run.id,
            task_id=child_task.id,
            seq=last_seq + 1,
            operation=RunControlOperation.FORCE_CANCEL,
            status=RunControlStatus.APPLIED,
            causal_position=causal_position,
            directive=None,
            reason=reason,
            safe_boundary="server:parent_force_cancel",
            idempotency_key=f"parent-force:{root_message_id}:{child_run.id}",
            requested_by_principal_id=ctx.principal_id,
            acknowledged_by_principal_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            causation_id=str(root_message_id),
            version=2,
            accepted_at=now,
            resolved_at=now,
        )
        session.add(derived)
        child_run.status = RunStatus.CANCELLED
        child_run.failure_reason = "parent_force_cancel"
        child_run.finished_at = now
        child_run.updated_at = now
        child_run.version += 1
        release_claim_on_locked_task(
            child_task,
            child_claim,
            reason="parent_force_cancel",
            lifecycle=await lifecycle_of(session, child_task),
        )
        child_task.version += 1
        await session.flush()

        # A cancelled child still owes its parent a result: "terminal run with
        # nothing to read" is exactly the hole the handle exists to close.
        await record_child_result(
            session,
            ctx,
            run=child_run,
            outcome="cancelled",
            output=None,
            failure_reason="parent_force_cancel",
        )

        for older in pending:
            await record_event(
                session,
                tenant_id=ctx.tenant_id,
                event_type="run.control_message.superseded",
                entity_type="run",
                entity_id=child_run.id,
                actor_id=ctx.principal_id,
                session_id=child_run.session_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
                causation_id=str(root_message_id),
                trace_run_id=ctx.trace_run_id,
                payload={
                    "taskId": str(child_task.id),
                    "controlMessageId": str(older.id),
                    "seq": older.seq,
                    "operation": older.operation,
                    "status": older.status,
                    "causalPosition": older.causal_position,
                    "safeBoundary": older.safe_boundary,
                },
            )

        for event_type, entity_type, entity_id, payload in (
            (
                "run.control_message.accepted",
                "run",
                child_run.id,
                {
                    "taskId": str(child_task.id),
                    "controlMessageId": str(derived.id),
                    "seq": derived.seq,
                    "operation": derived.operation,
                    "status": "accepted",
                    "causalPosition": derived.causal_position,
                },
            ),
            (
                "run.control_message.applied",
                "run",
                child_run.id,
                {
                    "taskId": str(child_task.id),
                    "controlMessageId": str(derived.id),
                    "seq": derived.seq,
                    "operation": derived.operation,
                    "status": derived.status,
                    "causalPosition": derived.causal_position,
                    "safeBoundary": derived.safe_boundary,
                },
            ),
            (
                "run.cancelled",
                "run",
                child_run.id,
                {
                    "taskId": str(child_task.id),
                    "reason": "parent_force_cancel",
                    "attempt": child_run.attempt,
                    "controlMessageId": str(derived.id),
                },
            ),
            (
                "claim.released",
                "claim",
                child_claim.id,
                {
                    "taskId": str(child_task.id),
                    "reason": "parent_force_cancel",
                    "taskStatus": child_task.status,
                },
            ),
        ):
            await record_event(
                session,
                tenant_id=ctx.tenant_id,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_id=ctx.principal_id,
                session_id=child_run.session_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
                causation_id=str(root_message_id),
                trace_run_id=ctx.trace_run_id,
                payload=payload,
            )


@dataclass(frozen=True)
class ControlMessageResult:
    control_message: RunControlMessage
    run_version: int


_SENSITIVE_CONTROL_CONTENT = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\bBearer\s+\S{16,}|"
    r"\bcp_[A-Za-z0-9]+_[A-Za-z0-9]{16,}|"
    r"\b(?:api[_-]?key|password|secret)\s*[:=]\s*\S{8,})",
    re.IGNORECASE,
)
_ABSOLUTE_LOCAL_PATH = re.compile(
    r"(?:^|\s)(?:/(?:Users|home|private|tmp|var|opt|Volumes)/|[A-Za-z]:[\\/])"
)


def _validate_control_text(value: str, field: str) -> None:
    if _SENSITIVE_CONTROL_CONTENT.search(value):
        raise ValidationError(
            "unsafe_control_payload",
            f"{field} must not contain credential-like material",
        )
    if _ABSOLUTE_LOCAL_PATH.search(value):
        raise ValidationError(
            "unsafe_control_payload",
            f"{field} must not contain absolute local filesystem paths",
        )


def _validate_create(
    *, operation: str, causal_position: str, directive: str | None, reason: str
) -> None:
    if operation not in set(RunControlOperation):
        raise ValidationError(
            "invalid_control_operation",
            f"Unknown control operation: {operation}",
        )
    if not causal_position.strip():
        raise ValidationError("invalid_control_message", "causalPosition must not be empty")
    if operation in {
        RunControlOperation.QUEUE,
        RunControlOperation.STEER,
        RunControlOperation.REDIRECT,
    } and (directive is None or not directive.strip()):
        raise ValidationError(
            "invalid_control_message",
            f"directive is required for operation '{operation}'",
        )
    if (
        operation
        in {
            RunControlOperation.REQUEST_CANCEL,
            RunControlOperation.FORCE_CANCEL,
        }
        and directive is not None
    ):
        raise ValidationError(
            "invalid_control_message",
            f"directive is not allowed for operation '{operation}'; use reason",
        )
    if not reason.strip() and operation == RunControlOperation.FORCE_CANCEL:
        raise ValidationError("invalid_control_message", "reason is required for force_cancel")
    if directive is not None:
        _validate_control_text(directive, "directive")
    if reason:
        _validate_control_text(reason, "reason")


async def create_control_message(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    operation: str,
    causal_position: str,
    directive: str | None,
    reason: str,
    idempotency_key: str,
    expected_run_version: int,
) -> ControlMessageResult:
    """Append one accepted message under the Run lock."""
    if operation == RunControlOperation.FORCE_CANCEL:
        await authorize(ctx, Permission.CLAIMS_MANAGE)
    else:
        await authorize(ctx, Permission.TASKS_WRITE)
    _validate_create(
        operation=operation,
        causal_position=causal_position,
        directive=directive,
        reason=reason,
    )
    if operation == RunControlOperation.FORCE_CANCEL:
        # spawned_by is a general relation graph. Serialize force-cascade
        # mutations per tenant before taking any task/run row lock so two
        # overlapping or cyclic descendant closures cannot deadlock.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(f"cp:run-control-tree:{ctx.tenant_id}", 0)
                )
            )
        )

    run_probe = await _get_tenant_run(session, ctx, run_id)
    task, run = await _lock_task_then_run(session, ctx, run_probe)
    _require_run_running(run)

    existing = await session.scalar(
        select(RunControlMessage).where(
            RunControlMessage.run_id == run.id,
            RunControlMessage.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        same_request = (
            existing.operation == operation
            and existing.causal_position == causal_position.strip()
            and existing.directive == (directive.strip() if directive is not None else None)
            and existing.reason == reason.strip()
        )
        if not same_request:
            raise ConflictError(
                "idempotency_key_reused",
                "Idempotency key was already used with a different control message",
                details={"key": idempotency_key, "runId": str(run.id)},
            )
        return ControlMessageResult(existing, run.version)

    if run.version != expected_run_version:
        raise ConflictError(
            "run_version_conflict",
            "Run version does not match expectedRunVersion",
            details={
                "runId": str(run.id),
                "expectedVersion": expected_run_version,
                "currentVersion": run.version,
            },
        )

    last_seq = (
        await session.scalar(
            select(func.max(RunControlMessage.seq)).where(RunControlMessage.run_id == run.id)
        )
    ) or 0
    now = utcnow()
    message = RunControlMessage(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        run_id=run.id,
        task_id=task.id,
        seq=last_seq + 1,
        operation=operation,
        status=RunControlStatus.ACCEPTED,
        causal_position=causal_position.strip(),
        directive=directive.strip() if directive is not None else None,
        reason=reason.strip(),
        safe_boundary=None,
        idempotency_key=idempotency_key,
        requested_by_principal_id=ctx.principal_id,
        acknowledged_by_principal_id=None,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        version=1,
        accepted_at=now,
        resolved_at=None,
    )
    session.add(message)
    run.version += 1
    run.updated_at = now
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.control_message.accepted",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task.id),
            "controlMessageId": str(message.id),
            "seq": message.seq,
            "operation": message.operation,
            "status": message.status,
            "causalPosition": message.causal_position,
        },
    )
    if operation == RunControlOperation.FORCE_CANCEL:
        claim = await session.scalar(
            select(TaskClaim)
            .where(
                TaskClaim.id == run.claim_id,
                TaskClaim.tenant_id == ctx.tenant_id,
            )
            .with_for_update()
        )
        if claim is None:  # pragma: no cover - FK guarantees existence
            raise ConflictError(
                "stale_claim",
                "Run claim is no longer available",
                details={"runId": str(run.id), "claimId": str(run.claim_id)},
            )

        # Every older pending instruction is rendered inapplicable by the
        # authoritative terminal transition.
        pending = (
            await session.scalars(
                select(RunControlMessage)
                .where(
                    RunControlMessage.run_id == run.id,
                    RunControlMessage.status == RunControlStatus.ACCEPTED,
                    RunControlMessage.id != message.id,
                )
                .order_by(RunControlMessage.seq)
                .with_for_update()
            )
        ).all()
        for older in pending:
            older.status = RunControlStatus.SUPERSEDED
            older.safe_boundary = "server:force_cancel"
            older.acknowledged_by_principal_id = ctx.principal_id
            older.version += 1
            older.resolved_at = now

        message.status = RunControlStatus.APPLIED
        message.safe_boundary = "server:force_cancel"
        message.acknowledged_by_principal_id = ctx.principal_id
        message.version += 1
        message.resolved_at = now
        run.status = RunStatus.CANCELLED
        run.failure_reason = reason.strip()
        run.finished_at = now
        run.updated_at = now
        run.version += 1
        release_claim_on_locked_task(
            task, claim, reason="force_cancel", lifecycle=await lifecycle_of(session, task)
        )
        task.version += 1
        await session.flush()

        await record_child_result(
            session,
            ctx,
            run=run,
            outcome="cancelled",
            output=None,
            failure_reason=reason.strip() or "force_cancel",
        )

        for older in pending:
            await record_event(
                session,
                tenant_id=ctx.tenant_id,
                event_type="run.control_message.superseded",
                entity_type="run",
                entity_id=run.id,
                actor_id=ctx.principal_id,
                session_id=run.session_id,
                request_id=ctx.request_id,
                correlation_id=ctx.correlation_id,
                trace_run_id=ctx.trace_run_id,
                payload={
                    "taskId": str(task.id),
                    "controlMessageId": str(older.id),
                    "seq": older.seq,
                    "operation": older.operation,
                    "status": older.status,
                    "causalPosition": older.causal_position,
                    "safeBoundary": older.safe_boundary,
                },
            )
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="run.control_message.applied",
            entity_type="run",
            entity_id=run.id,
            actor_id=ctx.principal_id,
            session_id=run.session_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "taskId": str(task.id),
                "controlMessageId": str(message.id),
                "seq": message.seq,
                "operation": message.operation,
                "status": message.status,
                "causalPosition": message.causal_position,
                "safeBoundary": message.safe_boundary,
            },
        )
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="run.cancelled",
            entity_type="run",
            entity_id=run.id,
            actor_id=ctx.principal_id,
            session_id=run.session_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "taskId": str(task.id),
                "reason": "force_cancel",
                "attempt": run.attempt,
                "controlMessageId": str(message.id),
            },
        )
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="claim.released",
            entity_type="claim",
            entity_id=claim.id,
            actor_id=ctx.principal_id,
            session_id=claim.session_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "taskId": str(task.id),
                "reason": "force_cancel",
                "taskStatus": task.status,
            },
        )
        await _cascade_force_cancel_children(
            session,
            ctx,
            root_task_id=task.id,
            root_message_id=message.id,
            causal_position=message.causal_position,
            reason=reason.strip(),
        )
    if operation == RunControlOperation.REQUEST_CANCEL and run.cancel_requested_at is None:
        run.cancel_requested_at = now
        run.cancel_requested_by = ctx.principal_id
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="run.cancel_requested",
            entity_type="run",
            entity_id=run.id,
            actor_id=ctx.principal_id,
            session_id=run.session_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "taskId": str(task.id),
                "controlMessageId": str(message.id),
                "attempt": run.attempt,
            },
        )
    return ControlMessageResult(message, run.version)


async def acknowledge_control_message(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    message_id: uuid.UUID,
    status: str,
    claim_id: uuid.UUID,
    fencing_token: int,
    expected_run_version: int,
    expected_message_version: int,
    safe_boundary: str | None,
    reason: str,
) -> ControlMessageResult:
    """Resolve the oldest accepted message at a declared safe boundary."""
    await authorize(ctx, Permission.TASKS_CLAIM)
    allowed_statuses = {
        RunControlStatus.APPLIED,
        RunControlStatus.REJECTED,
        RunControlStatus.SUPERSEDED,
    }
    if status not in allowed_statuses:
        raise ValidationError("invalid_control_status", f"Unknown acknowledgement status: {status}")
    if status == RunControlStatus.APPLIED and (safe_boundary is None or not safe_boundary.strip()):
        raise ValidationError(
            "invalid_control_message", "safeBoundary is required when status=applied"
        )

    run_probe = await _get_tenant_run(session, ctx, run_id)
    task, run = await _lock_task_then_run(session, ctx, run_probe)
    _require_run_running(run)
    if run.principal_id != ctx.principal_id:
        raise AuthorizationError(
            "Run belongs to another principal",
            code="run_holder_mismatch",
            details={"runId": str(run.id)},
        )
    live_claim = await enforce_claim_gate(
        session, ctx, task, claim_id=claim_id, fencing_token=fencing_token
    )
    if live_claim is None or live_claim.id != run.claim_id:
        raise ConflictError(
            "stale_claim",
            "Acknowledging control input requires the Run's live claim",
            details={"runId": str(run.id), "claimId": str(claim_id)},
        )
    if run.version != expected_run_version:
        raise ConflictError(
            "run_version_conflict",
            "Run version does not match expectedRunVersion",
            details={
                "runId": str(run.id),
                "expectedVersion": expected_run_version,
                "currentVersion": run.version,
            },
        )

    message = await session.scalar(
        select(RunControlMessage)
        .where(
            RunControlMessage.id == message_id,
            RunControlMessage.run_id == run.id,
            RunControlMessage.tenant_id == ctx.tenant_id,
        )
        .with_for_update()
    )
    if message is None:
        from control_plane.domain.errors import NotFoundError

        raise NotFoundError(
            "Control message not found", details={"controlMessageId": str(message_id)}
        )
    if message.status != RunControlStatus.ACCEPTED:
        raise ConflictError(
            "control_message_terminal",
            "Control message is already resolved",
            details={"controlMessageId": str(message.id), "status": message.status},
        )
    if message.version != expected_message_version:
        raise ConflictError(
            "control_message_version_conflict",
            "Control message version does not match expectedMessageVersion",
            details={
                "controlMessageId": str(message.id),
                "expectedVersion": expected_message_version,
                "currentVersion": message.version,
            },
        )
    oldest = await session.scalar(
        select(RunControlMessage)
        .where(
            RunControlMessage.run_id == run.id,
            RunControlMessage.status == RunControlStatus.ACCEPTED,
        )
        .order_by(RunControlMessage.seq)
        .limit(1)
    )
    if oldest is None or oldest.id != message.id:
        raise ConflictError(
            "control_message_out_of_order",
            "An earlier accepted control message must be resolved first",
            details={
                "controlMessageId": str(message.id),
                "earliestAcceptedId": str(oldest.id) if oldest else None,
            },
        )

    now = utcnow()
    message.status = status
    message.safe_boundary = safe_boundary.strip() if safe_boundary else None
    message.reason = reason.strip() or message.reason
    message.acknowledged_by_principal_id = ctx.principal_id
    message.version += 1
    message.resolved_at = now
    run.version += 1
    run.updated_at = now
    await session.flush()

    if (
        message.operation == RunControlOperation.REQUEST_CANCEL
        and status == RunControlStatus.APPLIED
    ):
        # The parent accepted the stop at its own safe boundary; children that
        # opted into cascade_cooperative are asked to do the same (HRS-7).
        await cascade_cooperative_cancel(
            session,
            ctx,
            parent_run_id=run.id,
            reason=message.reason or "parent_request_cancel",
            causation_id=str(message.id),
        )

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=f"run.control_message.{status}",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(task.id),
            "controlMessageId": str(message.id),
            "seq": message.seq,
            "operation": message.operation,
            "status": message.status,
            "causalPosition": message.causal_position,
            "safeBoundary": message.safe_boundary,
        },
    )
    return ControlMessageResult(message, run.version)
