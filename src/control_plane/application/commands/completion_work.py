"""Work a task type declares for after completion (CP-ADR-0061, amendment 2026-09-25).

When a task moves into ``terminal_success`` (:func:`finish_locked_task`: a
``:complete``, a run's ``:succeed``, an approval outcome's ``completeTask``),
core files what the task's type version declares in ``completion_schema`` —
in the same transaction, with the authority of whoever completed it:

* **condition** — every ``when`` expression must resolve to something (the
  published branch of the task, say); otherwise nothing is filed and nothing
  is recorded, so a later completion may still file it;
* **authority** — the completer's own ``AuthContext``: the actions go through
  the ordinary commands and the same authorizer, and what the expressions read
  the completer must be able to read (as with an approval's decider);
* **once** — one ``task_completion_work`` row per task; an index recorded as
  executed is never run again, so a repeated completion files nothing twice
  and a failed one resumes at its first open action;
* **failure** — a refused action's writes roll back (savepoint), the rest is
  not attempted, and the completion itself stands: completing a task must
  never be undone by what its type files afterwards. The failure is recorded
  (row ``failed``, ``task.completion_work_failed``) and said in a comment on
  the task by core's own principal — not in a work item for the completer,
  who is typically a runner.

Core knows the action vocabulary, never what a tenant uses it for.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands.approval_outcomes import (
    DecisionContext,
    failure_of,
    read_context,
    run_action,
    task_view,
)
from control_plane.application.commands.principals import ensure_core_principal
from control_plane.application.commands.task_comments import add_comment
from control_plane.application.commands.task_types import task_type_of
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.completion_work import CompletionSchema, is_met, schema_of
from control_plane.domain.enums import Permission
from control_plane.domain.errors import DomainError
from control_plane.infrastructure.db.models import Task, TaskCompletionWork

EXECUTED = "executed"
FAILED = "failed"
# ``outcome`` of the context a completion's actions run in (no decision).
COMPLETED = "completed"


def _context(task: Task, ctx: AuthContext) -> DecisionContext:
    return DecisionContext(
        approval_id=None,
        decided_by=ctx.principal_id,
        outcome=COMPLETED,
        approval={},
        task=task_view(task),
        spawned_by={},
        process_ref=f"completion:{task.id}",
        origin_metadata={"origin": "completion", "taskId": str(task.id)},
    )


async def file_completion_work(
    session: AsyncSession, ctx: AuthContext, task: Task
) -> TaskCompletionWork | None:
    """File what the type of the just-completed (locked) ``task`` declares.

    ``None`` when the type declares nothing or ``when`` does not hold; the
    row otherwise. Never raises a domain error: a failed action is recorded,
    the completion stands.
    """
    task_type = await task_type_of(session, task)
    schema = schema_of(task_type.completion_schema)
    if schema.empty:
        return None
    # The task row is locked by the completion, which serializes this read.
    row: TaskCompletionWork | None = await session.scalar(
        select(TaskCompletionWork).where(TaskCompletionWork.task_id == task.id)
    )
    if row is not None and row.status == EXECUTED:
        return row

    context = _context(task, ctx)
    evidence: dict[int, dict[str, Any]] = {
        int(item["index"]): item for item in (row.actions if row is not None else [])
    }
    first_open = next(
        (i for i in range(len(schema.actions)) if evidence.get(i, {}).get("status") != EXECUTED),
        0,
    )
    try:
        async with session.begin_nested():
            await read_context(session, ctx, context, schema.actions, extra=schema.when)
    except DomainError as exc:
        # Whether the work is due cannot even be told: the completer may not
        # read what the declaration reads. Said, not swallowed.
        return await _fail(
            session, ctx, task, task_type.id, row, evidence, first_open, schema, failure_of(exc)
        )
    if not all(is_met(context.resolve(condition)) for condition in schema.when):
        return row

    for index, action in enumerate(schema.actions):
        if evidence.get(index, {}).get("status") == EXECUTED:
            continue
        try:
            async with session.begin_nested():
                result = await run_action(session, ctx, context, action, index)
        except DomainError as exc:
            return await _fail(
                session, ctx, task, task_type.id, row, evidence, index, schema, failure_of(exc)
            )
        evidence[index] = {
            "index": index,
            "action": action.name,
            "status": EXECUTED,
            "result": result,
        }

    row = _save(session, ctx, task, task_type.id, row, evidence, status=EXECUTED, error=None)
    await session.flush()
    await _record(
        session,
        ctx,
        task,
        "task.completion_work_executed",
        {"taskTypeId": str(task_type.id), "actions": _ordered(evidence)},
    )
    return row


def _ordered(evidence: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    return [evidence[i] for i in sorted(evidence)]


def _save(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    task_type_id: uuid.UUID,
    row: TaskCompletionWork | None,
    evidence: dict[int, dict[str, Any]],
    *,
    status: str,
    error: dict[str, Any] | None,
) -> TaskCompletionWork:
    now = utcnow()
    if row is None:
        row = TaskCompletionWork(
            id=new_uuid(),
            tenant_id=task.tenant_id,
            task_id=task.id,
            task_type_id=task_type_id,
            completed_by=ctx.principal_id,
            status=status,
            attempts=1,
            actions=_ordered(evidence),
            error=error,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        return row
    row.completed_by = ctx.principal_id
    row.status = status
    row.attempts += 1
    row.actions = _ordered(evidence)
    row.error = error
    row.updated_at = now
    return row


async def _fail(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    task_type_id: uuid.UUID,
    row: TaskCompletionWork | None,
    evidence: dict[int, dict[str, Any]],
    index: int,
    schema: CompletionSchema,
    error: dict[str, Any],
) -> TaskCompletionWork:
    action = schema.actions[index]
    evidence[index] = {"index": index, "action": action.name, "status": FAILED, "error": error}
    row = _save(session, ctx, task, task_type_id, row, evidence, status=FAILED, error=error)
    await session.flush()
    await _record(
        session,
        ctx,
        task,
        "task.completion_work_failed",
        {
            "taskTypeId": str(task_type_id),
            "failedAction": {"index": index, "action": action.name, **error},
            "actions": _ordered(evidence),
        },
    )
    await _tell(session, ctx, task, index, action.name, error)
    return row


async def _record(
    session: AsyncSession, ctx: AuthContext, task: Task, event_type: str, payload: dict[str, Any]
) -> None:
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type=event_type,
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"publicId": task.public_id, **payload},
    )


async def _tell(
    session: AsyncSession,
    completer: AuthContext,
    task: Task,
    index: int,
    action: str,
    error: dict[str, Any],
) -> None:
    """A comment on the completed task, by core: the failure must be seen.

    By core's own principal, narrowed to ``tasks.write`` with no IAM subject,
    for the reason the failure work of an approval outcome is (the typical
    failure is that the completer may not write).
    """
    core = await ensure_core_principal(session, completer.tenant_id)
    ctx = AuthContext(
        tenant_id=completer.tenant_id,
        principal_id=core.id,
        principal_kind=core.kind,
        # No credential exists for core; its principal id stands in.
        api_key_id=core.id,
        permissions=frozenset({Permission.TASKS_WRITE.value}),
        request_id=completer.request_id,
        correlation_id=completer.correlation_id,
        causation_id=completer.causation_id,
        trace_run_id=completer.trace_run_id,
    )
    body = (
        f"Work declared by the task type for after completion was not filed: action #{index} "
        f"({action}) failed: {error['code']}: {error['message']}. The completion stands; the "
        "remaining actions were not executed. Fix the cause and file the work by hand, or "
        "reopen and complete the task again to resume at this action."
    )
    try:
        async with session.begin_nested():
            await add_comment(session, ctx, task_ref=str(task.id), body=body)
    except DomainError:
        # Still recorded on the row and in the journal.
        return
