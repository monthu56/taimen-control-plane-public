"""The verification stage: acceptance checks run before a task is done (CP-ADR-0067).

A task with acceptance checks is not set done by any of the three completion
paths (``:complete``, a run's ``:succeed``, an approval outcome's
``completeTask``): :func:`open_verification` releases its claim and opens an
**attempt** instead, and the task stays where it was, not claimable
(``verification_pending``). The worker picks up due attempts
(:func:`due_verifications`) and calls :func:`execute_verification`, which runs
the checks in the order they are declared:

* **authority** — the completer's: every call goes through the ordinary
  command with an ``AuthContext`` rebuilt from the credential snapshot taken
  at completion, which must still be active (as an approval outcome runs with
  its decider's);
* **deterministic** — a check with a ``skill`` queues a ``skill_invocation``
  through the path of ``POST /skills/{ref}:invoke``, requested by the attempt
  (``requestedBy.kind = verification``) and bound to the task; it passes when
  the call succeeds and its output equals ``expect``. A call without a result
  after ``skill_timeout`` is cancelled and the check fails ``no_result``;
* **evidence** — an ``external_state`` check, and a ``deterministic`` one
  without a skill, passes by evidence of the task tied to it (with ``event``,
  an observation of that kind); none after ``external_timeout`` is
  ``no_result``;
* **human, llm_judge** — a person's decision on a gate approval of the task:
  the approval whose ``completeTask`` outcome handed the task in counts at
  once, as does a gate approved after the attempt opened; otherwise the
  attempt waits (``waiting_human``) on the open gate, or requests one of the
  check's ``approver`` / ``approverRole`` (else the task's owner, then its
  assignee). Approved passes, rejected fails with the decision's comment;
  the decision wakes the attempt (:func:`wake_on_decision`). An
  ``llm_judge`` is decided by a person the same way — its rubric is shown;
* **outcome** — every check passed: the task moves into its completion
  status with ``task.completed`` and ``task.verified`` in one transaction, a
  ``verification`` artifact, and the work its type declares for after
  completion. The first failed check fails the attempt: the rest is not run,
  the task goes back to its ``releaseStatus`` with a comment on why, and the
  third failure in a row moves it to its first reachable ``blocked`` status
  instead. A cancelled task closes its open attempt ``cancelled``.

Core knows tasks, checks, skills, evidence and approvals here — never what
is checked or what a tenant does with it.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands.approval_outcomes import (
    DecisionContext,
    authority_snapshot,
    failure_of,
    read_context,
    require_active_credential,
    task_view,
)
from control_plane.application.commands.principals import ensure_core_principal
from control_plane.application.commands.skill_invocations import (
    CANCELLED_BY_SYSTEM,
    LIVE_STATUSES,
    cancel_skill_invocation,
    invoke_skill,
    resolve_skill_ref,
)
from control_plane.application.commands.task_comments import add_comment
from control_plane.application.commands.task_types import lifecycle_of
from control_plane.application.commands.tasks import mark_task_completed
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.approval_outcomes import Path, expressions_in, render
from control_plane.domain.enums import (
    ApprovalStatus,
    Permission,
    PrincipalKind,
    SkillInvocationRequester,
    SkillInvocationStatus,
    SkillSideEffects,
)
from control_plane.domain.errors import ConflictError, DomainError, NotFoundError, ValidationError
from control_plane.domain.work_graph import INVALID_ACCEPTANCE_SPEC, CheckKind, EvidenceKind
from control_plane.domain.work_item import TERMINAL_CATEGORIES, WorkItemStatusCategory
from control_plane.infrastructure.db.models import (
    Approval,
    Artifact,
    Event,
    EventArchive,
    Principal,
    Skill,
    SkillInvocation,
    Task,
    TaskVerification,
)

RUNNING = "running"
WAITING_HUMAN = "waiting_human"
WAITING_EXTERNAL = "waiting_external"
PASSED = "passed"
FAILED = "failed"
CANCELLED = "cancelled"
OPEN_STATUSES = (RUNNING, WAITING_HUMAN, WAITING_EXTERNAL)

TRIGGERS = frozenset({"run", "complete", "approval", "rule"})

# The third failed attempt in a row hands the task to a person.
MAX_CONSECUTIVE_FAILURES = 3
# Type of the artifact a passed attempt leaves on the task.
VERIFICATION_ARTIFACT = "verification"
# Idempotency key of a check's skill call: one per (attempt, check).
INVOCATION_KEY_PREFIX = "verification:"

DEFAULT_CHECK_SECONDS = 15.0
DEFAULT_SKILL_TIMEOUT_SECONDS = 900.0
DEFAULT_EXTERNAL_TIMEOUT_SECONDS = 86400.0

# Why a check failed (``results[].reason``, ``task.verification_failed``).
NO_RESULT = "no_result"
EXPECTATION_NOT_MET = "expectation_not_met"
SKILL_FAILED = "skill_failed"
SKILL_CANCELLED = "skill_cancelled"
APPROVAL_REJECTED = "approval_rejected"
APPROVAL_CANCELLED = "approval_cancelled"
NO_APPROVER = "no_approver"
# Why an attempt was cancelled.
TASK_CANCELLED = "task_cancelled"
# The reason given to a skill call the stage stops waiting for.
SKILL_TIMEOUT = "verification_skill_timeout"
ATTEMPT_CLOSED = "verification_closed"
# What an approval a check cites is, as evidence.
APPROVAL_EVIDENCE = "approval"
MAX_APPROVAL_COMMENT = 4000


# --- reads -----------------------------------------------------------------------


async def open_attempt(session: AsyncSession, task_id: uuid.UUID) -> TaskVerification | None:
    """The task's open attempt, if one is running or waiting."""
    row: TaskVerification | None = await session.scalar(
        select(TaskVerification)
        .where(TaskVerification.task_id == task_id, TaskVerification.status.in_(OPEN_STATUSES))
        .execution_options(populate_existing=True)
    )
    return row


async def latest_attempts(
    session: AsyncSession, tenant_id: uuid.UUID, task_ids: list[uuid.UUID]
) -> dict[uuid.UUID, TaskVerification]:
    """The newest attempt of each task, for a whole page in one query."""
    if not task_ids:
        return {}
    rows = (
        await session.scalars(
            select(TaskVerification)
            .where(TaskVerification.tenant_id == tenant_id, TaskVerification.task_id.in_(task_ids))
            .order_by(TaskVerification.task_id, TaskVerification.attempt.desc())
            .distinct(TaskVerification.task_id)
        )
    ).all()
    return {row.task_id: row for row in rows}


async def check_verification_gate(session: AsyncSession, task: Task) -> None:
    """Raise 409 ``verification_pending`` while the task's checks are running.

    The work was handed in; until the attempt closes, nobody takes it again.
    """
    row = await open_attempt(session, task.id)
    if row is not None:
        raise ConflictError(
            "verification_pending",
            "Task is waiting for its acceptance checks",
            details={"taskId": str(task.id), "verificationId": str(row.id), "status": row.status},
        )


def summary(row: TaskVerification | None) -> dict[str, Any] | None:
    """``TaskOut.verification``: the newest attempt, in brief."""
    if row is None:
        return None
    return {
        "id": str(row.id),
        "status": row.status,
        "attempt": row.attempt,
        "updatedAt": row.updated_at.isoformat(),
    }


# --- opening ---------------------------------------------------------------------


async def check_acceptance_skills(
    session: AsyncSession,
    ctx: AuthContext,
    checks: list[dict[str, Any]],
    *,
    field: str = "acceptance",
) -> None:
    """The skills ``deterministic`` checks name exist and write nothing outside.

    The grammar (``check_spec``) knows the form only; the registry is asked
    here, when the acceptance is written. A check whose skill is
    ``external_write`` is refused: a verification has no approval that could
    be the basis of an external write (ADR-0056 §4).
    """
    for index, check in enumerate(checks):
        ref = (check.get("spec") or {}).get("skill")
        if check["kind"] != CheckKind.DETERMINISTIC or not ref:
            continue
        path = f"{field}[{index}].spec.skill"
        try:
            skill = await resolve_skill_ref(session, ctx, ref)
        except NotFoundError as exc:
            raise ValidationError(
                INVALID_ACCEPTANCE_SPEC,
                f"{path}: skill {ref!r} is not registered",
                details={"field": path, "kind": check["kind"]},
            ) from exc
        if skill.side_effects == SkillSideEffects.EXTERNAL_WRITE:
            raise ValidationError(
                INVALID_ACCEPTANCE_SPEC,
                f"{path}: {ref!r} writes to an external system; a check may not",
                details={"field": path, "kind": check["kind"], "sideEffects": skill.side_effects},
            )


async def open_verification(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    *,
    session_id: uuid.UUID | None,
    trigger: str,
    trigger_ref: str | None,
    checks: list[dict[str, Any]] | None = None,
) -> Task:
    """Open an attempt on a locked task whose completion was just requested.

    The caller has released the claim and holds the task row lock, which
    serializes this with every other completion of the task; the partial
    unique index is the net under it. The task keeps its status: only a
    passed attempt moves it into its completion status. ``checks`` default to
    the task's acceptance; a rule closing work without acceptance passes its
    implicit check instead.
    """
    assert trigger in TRIGGERS
    now = utcnow()
    last = await session.scalar(
        select(func.max(TaskVerification.attempt)).where(TaskVerification.task_id == task.id)
    )
    row = TaskVerification(
        id=new_uuid(),
        tenant_id=task.tenant_id,
        task_id=task.id,
        attempt=(last or 0) + 1,
        status=RUNNING,
        trigger=trigger,
        trigger_ref=trigger_ref,
        authority_principal_id=ctx.principal_id,
        authority=authority_snapshot(ctx),
        correlation_id=ctx.correlation_id,
        checks=list(checks if checks is not None else task.acceptance),
        results=[],
        cursor=0,
        skill_invocation_id=None,
        approval_id=None,
        next_check_at=now,
        started_at=now,
        finished_at=None,
        updated_at=now,
    )
    session.add(row)
    task.updated_at = now
    task.version += 1
    await session.flush()
    await _record(
        session,
        ctx,
        task,
        row,
        event_type="task.verification_started",
        payload={"trigger": trigger, "triggerRef": trigger_ref},
        session_id=session_id,
    )
    return task


# --- cancelling ------------------------------------------------------------------


async def cancel_open_attempt(session: AsyncSession, ctx: AuthContext, task: Task) -> None:
    """Close the open attempt of a (locked) task that was just cancelled.

    Its live skill call is cancelled too: nothing it could report would be
    used. A cancelled attempt is never counted as a success or a failure.
    """
    row = await open_attempt(session, task.id)
    if row is None:
        return
    locked = await _lock_row(session, row.id)
    if locked is None or locked.status not in OPEN_STATUSES:
        return
    # The call is the completer's: stopped with their authority, whoever
    # cancelled the task.
    await _stop_call(session, _authority_context(locked, ctx.trace_run_id), locked, ATTEMPT_CLOSED)
    now = utcnow()
    locked.status = CANCELLED
    locked.next_check_at = None
    locked.finished_at = now
    locked.updated_at = now
    locked.results = [
        *locked.results,
        *(
            [_result(locked.checks[locked.cursor], CANCELLED, reason=TASK_CANCELLED)]
            if locked.cursor < len(locked.checks)
            else []
        ),
    ]
    await session.flush()
    await _withdraw_request(session, ctx, task, locked)


# --- the worker's pass -----------------------------------------------------------


async def due_verifications(session: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Open attempts whose next look is due, oldest first."""
    rows = await session.scalars(
        select(TaskVerification.id)
        .where(
            TaskVerification.status.in_(OPEN_STATUSES),
            TaskVerification.next_check_at <= utcnow(),
        )
        .order_by(TaskVerification.next_check_at)
        .limit(limit)
    )
    return list(rows.all())


async def postpone_verification(
    session: AsyncSession, *, verification_id: uuid.UUID, seconds: float
) -> None:
    """Look at an attempt whose pass broke unexpectedly again only later."""
    row = await _lock_row(session, verification_id)
    if row is not None and row.status in OPEN_STATUSES:
        row.next_check_at = utcnow() + timedelta(seconds=seconds)
        row.updated_at = utcnow()


@dataclass
class Timing:
    check: timedelta = timedelta(seconds=DEFAULT_CHECK_SECONDS)
    skill_timeout: timedelta = timedelta(seconds=DEFAULT_SKILL_TIMEOUT_SECONDS)
    external_timeout: timedelta = timedelta(seconds=DEFAULT_EXTERNAL_TIMEOUT_SECONDS)


@dataclass
class _Outcome:
    """What one look at a check found: a result, or a reason to wait."""

    status: str  # passed | failed | running | waiting_human | waiting_external
    evidence: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    message: str | None = None
    next_check_at: datetime | None = None


async def execute_verification(
    session: AsyncSession,
    *,
    verification_id: uuid.UUID,
    trace_run_id: str = "",
    timing: Timing | None = None,
) -> TaskVerification | None:
    """Run an open attempt as far as it goes now.

    A no-op for an attempt that is no longer open, or whose task another
    transaction holds (``SKIP LOCKED``: several workers may scan side by
    side). Lock order is the codebase's: the task row, then the attempt.
    """
    timing = timing or Timing()
    probe = await session.get(TaskVerification, verification_id, populate_existing=True)
    if probe is None or probe.status not in OPEN_STATUSES:
        return probe
    task: Task | None = await session.scalar(
        select(Task)
        .where(Task.id == probe.task_id)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if task is None:
        return None
    row = await _lock_row(session, verification_id)
    if row is None or row.status not in OPEN_STATUSES:
        return row
    ctx = _authority_context(row, trace_run_id)
    if task.system_status_category in TERMINAL_CATEGORIES:
        # Closed under the attempt by a path that did not cancel it.
        await cancel_open_attempt(session, ctx, task)
        return row

    try:
        await require_active_credential(
            session,
            authority=row.authority,
            principal_id=row.authority_principal_id,
            subject="the task was completed with",
        )
    except DomainError as exc:
        error = failure_of(exc)
        return await _fail(
            session,
            ctx,
            task,
            row,
            _Outcome(FAILED, reason=error["code"], message=error["message"]),
        )

    while row.cursor < len(row.checks):
        check = row.checks[row.cursor]
        outcome = await _look(session, ctx, task, row, check, timing)
        if outcome.status == FAILED:
            return await _fail(session, ctx, task, row, outcome)
        if outcome.status != PASSED:
            row.status = outcome.status
            row.next_check_at = outcome.next_check_at
            row.updated_at = utcnow()
            await session.flush()
            return row
        row.results = [*row.results, _result(check, PASSED, evidence=outcome.evidence)]
        row.cursor += 1
        row.skill_invocation_id = None
        row.approval_id = None
        row.status = RUNNING
        row.updated_at = utcnow()
        await session.flush()
    return await _pass(session, ctx, task, row)


async def _look(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
    timing: Timing,
) -> _Outcome:
    kind = check["kind"]
    spec = check.get("spec") or {}
    if kind == CheckKind.DETERMINISTIC and spec.get("skill"):
        return await _skill_check(session, ctx, task, row, spec, timing)
    if kind in (CheckKind.DETERMINISTIC, CheckKind.EXTERNAL_STATE):
        return await _evidence_check(session, task, row, check, spec, timing)
    return await _decision_check(session, ctx, task, row, check)


# --- deterministic: a skill call -------------------------------------------------


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    return []


async def _skill_check(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    spec: dict[str, Any],
    timing: Timing,
) -> _Outcome:
    now = utcnow()
    if row.skill_invocation_id is None:
        try:
            async with session.begin_nested():
                inputs = await _render_inputs(session, ctx, task, spec.get("inputs") or {})
                queued = await invoke_skill(
                    session,
                    ctx,
                    skill_ref=spec["skill"],
                    inputs=inputs,
                    idempotency_key=f"{INVOCATION_KEY_PREFIX}{row.id}:{row.cursor}",
                    task_ref=str(task.id),
                    requested_by=(SkillInvocationRequester.VERIFICATION, str(row.id)),
                )
        except DomainError as exc:
            # The completer may not make the call, the skill is gone or its
            # inputs do not fit: the check cannot pass, and says why.
            error = failure_of(exc)
            return _Outcome(FAILED, reason=error["code"], message=error["message"])
        row.skill_invocation_id = queued.invocation.id
        return _Outcome(RUNNING, next_check_at=now + min(timing.check, timing.skill_timeout))

    invocation = await session.get(SkillInvocation, row.skill_invocation_id, populate_existing=True)
    assert invocation is not None  # pragma: no cover - rows are never deleted
    evidence = [{"kind": "skill_invocation", "ref": str(invocation.id)}]
    if invocation.status in LIVE_STATUSES:
        deadline = invocation.created_at + timing.skill_timeout
        if deadline > now:
            return _Outcome(RUNNING, next_check_at=min(now + timing.check, deadline))
        await _stop_call(session, ctx, row, SKILL_TIMEOUT)
        return _Outcome(
            FAILED,
            evidence=evidence,
            reason=NO_RESULT,
            message=f"no result within {int(timing.skill_timeout.total_seconds())} s",
        )
    skill = await session.get(Skill, invocation.skill_id)
    assert skill is not None
    name = f"{skill.name}@{skill.version}"
    if invocation.status == SkillInvocationStatus.SUCCEEDED:
        artifact = await _result_artifact(session, task, invocation)
        if artifact is not None:
            evidence.append({"kind": "artifact", "ref": str(artifact)})
        mismatch = _mismatch(invocation.output or {}, spec.get("expect") or {})
        if not mismatch:
            return _Outcome(PASSED, evidence=evidence)
        return _Outcome(
            FAILED,
            evidence=evidence,
            reason=EXPECTATION_NOT_MET,
            message=f"{name}: output does not match expect in {mismatch}",
        )
    error = invocation.error or {}
    return _Outcome(
        FAILED,
        evidence=evidence,
        reason=SKILL_CANCELLED
        if invocation.status == SkillInvocationStatus.CANCELLED
        else SKILL_FAILED,
        message=f"{name} ended {invocation.status}: {error.get('code')}: {error.get('message')}",
    )


async def _render_inputs(
    session: AsyncSession, ctx: AuthContext, task: Task, inputs: dict[str, Any]
) -> dict[str, Any]:
    """The check's inputs, their ``$.task…`` expressions read as the completer."""
    paths: list[Path] = [path for text in _strings(inputs) for path in expressions_in(text)]
    context = DecisionContext(
        approval_id=None,
        decided_by=ctx.principal_id,
        outcome="verification",
        approval={},
        task=task_view(task),
        spawned_by={},
    )
    await read_context(session, ctx, context, (), extra=tuple(paths))
    rendered = render(inputs, context.resolve)
    assert isinstance(rendered, dict)
    return rendered


def _mismatch(output: dict[str, Any], expect: dict[str, Any]) -> list[str]:
    """Output fields that differ from ``expect`` (strict JSON equality)."""
    return sorted(
        key
        for key, value in expect.items()
        if key not in output or type(output[key]) is not type(value) or output[key] != value
    )


async def _result_artifact(
    session: AsyncSession, task: Task, invocation: SkillInvocation
) -> uuid.UUID | None:
    artifact_id: uuid.UUID | None = await session.scalar(
        select(Artifact.id)
        .where(
            Artifact.tenant_id == task.tenant_id,
            Artifact.task_id == task.id,
            Artifact.type == "skill_result",
            Artifact.metadata_json["invocationId"].astext == str(invocation.id),
        )
        .limit(1)
    )
    return artifact_id


async def _stop_call(
    session: AsyncSession, ctx: AuthContext, row: TaskVerification, reason: str
) -> None:
    """Cancel the attempt's live skill call; a finished one is left alone."""
    if row.skill_invocation_id is None:
        return
    invocation = await session.get(SkillInvocation, row.skill_invocation_id)
    if invocation is None or invocation.status not in LIVE_STATUSES:
        return
    try:
        async with session.begin_nested():
            await cancel_skill_invocation(
                session,
                ctx,
                invocation_id=invocation.id,
                reason=reason,
                initiator=CANCELLED_BY_SYSTEM,
            )
    except DomainError:
        # The completer's authority can no longer cancel it (its lease bounds
        # it anyway); the attempt does not wait for its result either way.
        return


# --- deterministic without a skill, external_state: evidence ---------------------


async def _evidence_check(
    session: AsyncSession,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
    spec: dict[str, Any],
    timing: Timing,
) -> _Outcome:
    """Evidence of the task tied to the check — read fresh, it may arrive late."""
    tied = [item for item in task.evidence or [] if item.get("check") == check["key"]]
    wanted = spec.get("event")
    if wanted:
        tied = [item for item in tied if await _observation_of_kind(session, task, item, wanted)]
    if tied:
        return _Outcome(PASSED, evidence=[_pointer(item) for item in tied])
    now = utcnow()
    deadline = row.started_at + timing.external_timeout
    if deadline > now:
        return _Outcome(WAITING_EXTERNAL, next_check_at=min(now + timing.check, deadline))
    return _Outcome(
        FAILED,
        reason=NO_RESULT,
        message=(
            f"no evidence for {check['key']!r}"
            + (f" of kind {wanted!r}" if wanted else "")
            + f" within {int(timing.external_timeout.total_seconds())} s"
        ),
    )


async def _observation_of_kind(
    session: AsyncSession, task: Task, item: dict[str, Any], kind: str
) -> bool:
    if item.get("kind") != EvidenceKind.OBSERVATION:
        return False
    for table in (Event, EventArchive):
        payload = await session.scalar(
            select(table.payload).where(
                table.tenant_id == task.tenant_id,
                table.entity_type == "observation",
                table.event_type == "observation.recorded",
                table.entity_id == uuid.UUID(item["observationId"]),
            )
        )
        if payload is not None:
            return bool(payload.get("kind") == kind)
    return False


def _pointer(item: dict[str, Any]) -> dict[str, Any]:
    """An evidence item as a result cites it: the pointer, not the note."""
    return {k: v for k, v in item.items() if k not in ("note", "check")}


# --- human, llm_judge: a decision on a gate approval -----------------------------


async def _decision_check(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
) -> _Outcome:
    """A person's decision on the task's gate approval (CP-ADR-0067 §6).

    Nothing to look at on a timer: the attempt waits without one, and the
    decision wakes it (:func:`wake_on_decision`).
    """
    if row.approval_id is None:
        counted = await _counted_approval(session, task, row)
        if counted is not None:
            return _Outcome(PASSED, evidence=[_approval_pointer(counted)])
        waited: Approval | None = await session.scalar(
            select(Approval)
            .where(
                Approval.tenant_id == task.tenant_id,
                Approval.task_id == task.id,
                Approval.gate.is_(True),
                Approval.status == ApprovalStatus.PENDING,
            )
            .order_by(Approval.created_at, Approval.id)
            .limit(1)
        )
        if waited is None:
            try:
                async with session.begin_nested():
                    waited = await _request_decision(session, ctx, task, row, check)
            except DomainError as exc:
                error = failure_of(exc)
                return _Outcome(FAILED, reason=error["code"], message=error["message"])
            if waited is None:
                return _Outcome(
                    FAILED,
                    reason=NO_APPROVER,
                    message="the check names no approver and the task's owner and assignee "
                    "are not people",
                )
        row.approval_id = waited.id
        return _Outcome(WAITING_HUMAN)

    approval = await session.get(Approval, row.approval_id, populate_existing=True)
    assert approval is not None  # pragma: no cover - rows are never deleted
    if approval.status == ApprovalStatus.PENDING:
        return _Outcome(WAITING_HUMAN)
    evidence = [_approval_pointer(approval)]
    if approval.status == ApprovalStatus.APPROVED:
        return _Outcome(PASSED, evidence=evidence)
    return _Outcome(
        FAILED,
        evidence=evidence,
        reason=APPROVAL_REJECTED
        if approval.status == ApprovalStatus.REJECTED
        else APPROVAL_CANCELLED,
        message=f"approval {approval.status}: {approval.comment}"
        if approval.comment
        else f"approval {approval.status}",
    )


async def _counted_approval(
    session: AsyncSession, task: Task, row: TaskVerification
) -> Approval | None:
    """An approved gate that already decides the check, if there is one.

    The decision whose ``completeTask`` outcome handed the task in counts for
    every ``human`` check of its attempt. Otherwise a gate of the task
    approved since the attempt opened counts once: each check of the attempt
    needs a decision of its own.
    """
    if row.trigger == "approval" and row.trigger_ref:
        trigger = await session.get(Approval, uuid.UUID(row.trigger_ref))
        if (
            trigger is not None
            and trigger.task_id == task.id
            and trigger.status == ApprovalStatus.APPROVED
        ):
            return trigger
    cited = {
        item["ref"]
        for result in row.results
        for item in result["evidence"]
        if item.get("kind") == APPROVAL_EVIDENCE
    }
    candidates = (
        await session.scalars(
            select(Approval)
            .where(
                Approval.tenant_id == task.tenant_id,
                Approval.task_id == task.id,
                Approval.gate.is_(True),
                Approval.status == ApprovalStatus.APPROVED,
                Approval.decision_at >= row.started_at,
            )
            .order_by(Approval.decision_at, Approval.id)
        )
    ).all()
    return next((a for a in candidates if str(a.id) not in cited), None)


async def _request_decision(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
) -> Approval | None:
    """Ask for the decision: a gate approval of the task, filed by core.

    Filed by core's own principal, narrowed to ``approvals.manage``, like the
    comment on a failed attempt: the typical completer is a runner, which asks
    no one for anything, and core can withdraw its own request when the
    attempt closes. Without ``approver`` / ``approverRole`` it goes to a
    person only (:func:`_person_to_ask`). ``None`` if nobody is there to ask.
    """
    from control_plane.application.commands.approvals import request_approval

    spec = check.get("spec") or {}
    role = spec.get("approverRole")
    principal = spec.get("approver") or (None if role else await _person_to_ask(session, task))
    if role is None and principal is None:
        return None
    lines = [
        f"Acceptance check {check['key']!r} ({check['kind']}) of {task.public_id}, "
        f"verification attempt #{row.attempt}: {check.get('description') or ''}".rstrip(": "),
    ]
    if spec.get("rubric"):
        lines.append(f"Rubric: {spec['rubric']}")
    return await request_approval(
        session,
        await _core_context(session, task, ctx, Permission.APPROVALS_MANAGE),
        task_ref=str(task.id),
        workspace_id=task.workspace_id,
        required_role_id=uuid.UUID(str(role)) if role else None,
        assigned_principal_id=uuid.UUID(str(principal)) if principal else None,
        comment="\n".join(lines)[:MAX_APPROVAL_COMMENT],
        gate=True,
    )


async def _person_to_ask(session: AsyncSession, task: Task) -> uuid.UUID | None:
    """The task's owner, else its assignee — whichever is a person.

    An agent is never asked to accept work, least of all its own: a runner
    that executed the task would otherwise approve its own result. Nobody
    human — ``None``, and the check fails ``no_approver``.
    """
    for principal_id in (task.owner_id, task.assignee_id):
        if principal_id is None:
            continue
        kind = await session.scalar(select(Principal.kind).where(Principal.id == principal_id))
        if kind == PrincipalKind.HUMAN:
            return principal_id
    return None


async def _withdraw_request(
    session: AsyncSession, ctx: AuthContext, task: Task, row: TaskVerification
) -> None:
    """Cancel the gate a closed attempt filed and nobody decided: it would hold the task.

    A gate the attempt only waited on is somebody else's and is left alone.
    """
    from control_plane.application.commands.approvals import cancel_approval

    if row.approval_id is None:
        return
    approval = await session.get(Approval, row.approval_id, populate_existing=True)
    if approval is None or approval.status != ApprovalStatus.PENDING:
        return
    core = await _core_context(session, task, ctx, Permission.APPROVALS_MANAGE)
    if approval.requested_by_principal_id != core.principal_id:
        return
    try:
        async with session.begin_nested():
            await cancel_approval(session, core, approval_id=approval.id)
    except DomainError:
        # Left pending; whoever it is assigned to can still close it.
        return


def _approval_pointer(approval: Approval) -> dict[str, Any]:
    return {"kind": APPROVAL_EVIDENCE, "ref": str(approval.id)}


async def wake_on_decision(session: AsyncSession, approval: Approval) -> None:
    """A gate of a task was decided or withdrawn: look at its waiting attempt now.

    A plain ``UPDATE``: if the worker holds the attempt, this waits for it and
    then sees the status it left, so a decision is never slept through.
    """
    if not approval.gate or approval.task_id is None:
        return
    await session.execute(
        update(TaskVerification)
        .where(
            TaskVerification.task_id == approval.task_id,
            TaskVerification.status == WAITING_HUMAN,
        )
        .values(next_check_at=utcnow(), updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )


async def wake_on_evidence(session: AsyncSession, task_id: uuid.UUID) -> None:
    """The task's evidence changed: an attempt waiting for a fact looks now.

    A rule closing the work, or anybody writing evidence tied to a check,
    does not leave the attempt asleep until its next timed look.
    """
    await session.execute(
        update(TaskVerification)
        .where(TaskVerification.task_id == task_id, TaskVerification.status == WAITING_EXTERNAL)
        .values(next_check_at=utcnow(), updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )


# --- closing ---------------------------------------------------------------------


def _result(
    check: dict[str, Any],
    status: str,
    *,
    evidence: list[dict[str, Any]] | None = None,
    reason: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    return {
        "key": check["key"],
        "kind": check["kind"],
        "status": status,
        "evidence": evidence or [],
        "reason": reason,
        **({"message": message[:2000]} if message else {}),
    }


def _brief(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Results as the journal carries them: no reason text (ADR-0015)."""
    return [
        {"key": r["key"], "kind": r["kind"], "status": r["status"], "evidence": r["evidence"]}
        for r in results
    ]


async def _pass(
    session: AsyncSession, ctx: AuthContext, task: Task, row: TaskVerification
) -> TaskVerification:
    """Every check passed: the task is done — in this transaction, with its events."""
    now = utcnow()
    row.status = PASSED
    row.next_check_at = None
    row.finished_at = now
    row.updated_at = now
    await session.flush()
    artifact = await _record_artifact(session, ctx, task, row)
    await mark_task_completed(
        session,
        ctx,
        task,
        payload={"verificationId": str(row.id), "attempt": row.attempt},
    )
    await _record(
        session,
        ctx,
        task,
        row,
        event_type="task.verified",
        payload={"results": _brief(row.results), "artifactId": str(artifact.id)},
    )
    return row


async def _fail(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    outcome: _Outcome,
) -> TaskVerification:
    """The first failed check fails the attempt; the task goes back to work."""
    now = utcnow()
    check = row.checks[min(row.cursor, len(row.checks) - 1)]
    row.results = [
        *row.results,
        _result(
            check, FAILED, evidence=outcome.evidence, reason=outcome.reason, message=outcome.message
        ),
    ]
    row.status = FAILED
    row.next_check_at = None
    row.finished_at = now
    row.updated_at = now
    await session.flush()
    await _withdraw_request(session, ctx, task, row)

    failures = await _consecutive_failures(session, task.id)
    exhausted = failures >= MAX_CONSECUTIVE_FAILURES
    previous = task.status
    await _return_task(session, task, blocked=exhausted)
    task.updated_at = now
    task.version += 1
    await _record(
        session,
        ctx,
        task,
        row,
        event_type="task.verification_failed",
        payload={
            "results": _brief(row.results),
            "failedCheck": check["key"],
            "reason": outcome.reason,
            "consecutiveFailures": failures,
            "blocked": exhausted,
            "fromStatus": previous,
            "status": task.status,
            "systemStatusCategory": task.system_status_category,
        },
    )
    await _tell(session, ctx, task, row, check, outcome, failures, exhausted)
    return row


async def _consecutive_failures(session: AsyncSession, task_id: uuid.UUID) -> int:
    """Failed attempts in a row, newest first, up to the first that did not fail.

    A cancelled attempt says nothing about the work and is passed over.
    """
    statuses = (
        await session.scalars(
            select(TaskVerification.status)
            .where(
                TaskVerification.task_id == task_id,
                TaskVerification.status.in_((PASSED, FAILED)),
            )
            .order_by(TaskVerification.attempt.desc())
        )
    ).all()
    count = 0
    for status in statuses:
        if status != FAILED:
            break
        count += 1
    return count


async def _return_task(session: AsyncSession, task: Task, *, blocked: bool) -> None:
    """Back to the executor (``releaseStatus``), or to a person (``blocked``).

    Only along a declared edge: a lifecycle without one leaves the status
    alone, as a claim release does (SPEC §4.4). The open attempt is closed
    by now, so the task is claimable again either way.
    """
    lifecycle = await lifecycle_of(session, task)
    target: str | None
    if blocked:
        target = next(
            (
                status
                for status in lifecycle.targets_from(task.status)
                if lifecycle.category_of(status) == WorkItemStatusCategory.BLOCKED
            ),
            None,
        )
    else:
        target = lifecycle.release_status
    if target is None or target == task.status or not lifecycle.allows(task.status, target):
        return
    task.status = target
    task.system_status_category = lifecycle.category_of(target)


async def _record_artifact(
    session: AsyncSession, ctx: AuthContext, task: Task, row: TaskVerification
) -> Artifact:
    """What the passed attempt established, on the task, by the completer."""
    artifact = Artifact(
        id=new_uuid(),
        tenant_id=task.tenant_id,
        workspace_id=task.workspace_id,
        task_id=task.id,
        run_id=None,
        created_by_principal_id=row.authority_principal_id,
        type=VERIFICATION_ARTIFACT,
        name=f"{task.public_id} verification #{row.attempt}",
        uri=None,
        content={
            "verificationId": str(row.id),
            "attempt": row.attempt,
            "trigger": row.trigger,
            "triggerRef": row.trigger_ref,
            "results": row.results,
        },
        supersedes_artifact_id=None,
        metadata_json={"verificationId": str(row.id), "attempt": row.attempt},
        created_at=utcnow(),
    )
    session.add(artifact)
    await session.flush()
    await record_event(
        session,
        tenant_id=task.tenant_id,
        event_type="artifact.created",
        entity_type="artifact",
        entity_id=artifact.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "type": artifact.type,
            "name": artifact.name,
            "taskId": str(task.id),
            "runId": None,
            "uri": None,
            "supersedesArtifactId": None,
            "verificationId": str(row.id),
        },
    )
    return artifact


async def _tell(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    check: dict[str, Any],
    outcome: _Outcome,
    failures: int,
    exhausted: bool,
) -> None:
    """A comment on the task with why it failed, by core's own principal.

    By core, narrowed to ``tasks.write``, like the comment on failed work
    after completion: the typical completer is a runner, and the reason must
    reach whoever takes the task next.
    """
    core_ctx = await _core_context(session, task, ctx, Permission.TASKS_WRITE)
    lines = [
        f"Verification attempt #{row.attempt} failed at check {check['key']!r} "
        f"({check['kind']}): {outcome.reason}"
        + (f": {outcome.message}" if outcome.message else ""),
    ]
    for result in row.results:
        lines.append(f"- {result['key']} ({result['kind']}): {result['status']}")
    skipped = [c["key"] for c in row.checks[len(row.results) :]]
    if skipped:
        lines.append(f"Not run: {', '.join(skipped)}.")
    if exhausted:
        lines.append(
            f"{failures} failed attempts in a row: the task waits for a person "
            f"(status {task.status!r})."
        )
    else:
        lines.append(
            f"The task is back in {task.status!r} for another attempt "
            f"({failures} of {MAX_CONSECUTIVE_FAILURES} failed in a row)."
        )
    try:
        async with session.begin_nested():
            await add_comment(session, core_ctx, task_ref=str(task.id), body="\n".join(lines))
    except DomainError:
        # Still recorded on the attempt and in the journal.
        return


# --- shared ----------------------------------------------------------------------


async def _core_context(
    session: AsyncSession, task: Task, ctx: AuthContext, permission: Permission
) -> AuthContext:
    """Core's own principal, narrowed to the one permission it acts with."""
    core = await ensure_core_principal(session, task.tenant_id)
    return AuthContext(
        tenant_id=task.tenant_id,
        principal_id=core.id,
        principal_kind=core.kind,
        # No credential exists for core; its principal id stands in.
        api_key_id=core.id,
        permissions=frozenset({permission.value}),
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
    )


async def _lock_row(session: AsyncSession, verification_id: uuid.UUID) -> TaskVerification | None:
    row: TaskVerification | None = await session.scalar(
        select(TaskVerification)
        .where(TaskVerification.id == verification_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return row


def _authority_context(row: TaskVerification, trace_run_id: str) -> AuthContext:
    """The completer, as the credential snapshot taken at completion says."""
    authority = row.authority or {}
    iam = authority.get("iamPrincipalId")
    return AuthContext(
        tenant_id=row.tenant_id,
        principal_id=row.authority_principal_id,
        principal_kind=str(authority.get("principalKind") or "human"),
        api_key_id=uuid.UUID(str(authority["credentialId"])),
        permissions=frozenset(authority.get("permissions") or ()),
        request_id=f"verification:{row.id}",
        correlation_id=row.correlation_id,
        causation_id=None,
        trace_run_id=trace_run_id,
        iam_principal_id=uuid.UUID(iam) if iam else None,
    )


async def _record(
    session: AsyncSession,
    ctx: AuthContext,
    task: Task,
    row: TaskVerification,
    *,
    event_type: str,
    payload: dict[str, Any],
    session_id: uuid.UUID | None = None,
) -> None:
    await record_event(
        session,
        tenant_id=task.tenant_id,
        event_type=event_type,
        entity_type="task",
        entity_id=task.id,
        actor_id=ctx.principal_id,
        session_id=session_id,
        request_id=ctx.request_id,
        correlation_id=row.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "publicId": task.public_id,
            "taskId": str(task.id),
            "verificationId": str(row.id),
            "attempt": row.attempt,
            "trigger": row.trigger,
            "checks": len(row.checks),
            **payload,
        },
    )
