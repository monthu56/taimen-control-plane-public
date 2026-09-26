"""Preconditions of an approval decision (TAI-ADR-0041 p.7, CP-ADR-0061).

A task type version may declare, next to what happens after a gate approval
is decided, what must hold before it may be approved: "the branch's CI is
green", read from an external observation (CP-ADR-0057). While a
precondition does not hold, ``approve`` is refused with
``409 approval_precondition_failed`` and nothing is recorded — a human does
not approve what core already knows it will not be able to carry out.
``reject`` has no preconditions.

The observation is looked up by core; the expressions that pick it
(``$.task``/``$.spawnedBy``) and the reason shown are read as the decider,
like an outcome's inputs.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import DateTime, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.commands.approval_outcomes import (
    DecisionContext,
    base_context,
    read_context,
)
from control_plane.application.commands.task_types import task_type_of
from control_plane.domain.approval_outcomes import (
    APPROVAL_PRECONDITION_FAILED,
    DEFAULT_GATE,
    OBSERVATION_ROOT,
    Precondition,
    parse_approval_schema,
    render,
)
from control_plane.domain.errors import ConflictError, ValidationError
from control_plane.domain.work_rules import ConditionError, VarPath, evaluate, walk
from control_plane.infrastructure.db.models import Approval, Event, Task

OBSERVATION_EVENT = "observation.recorded"
# Why a precondition did not hold, as reported in ``details.failed[].cause``.
CAUSE_UNRESOLVED = "unresolved_reference"
CAUSE_NO_OBSERVATION = "no_observation"
CAUSE_CONDITION_FALSE = "condition_false"
CAUSE_CONDITION_ERROR = "condition_error"


@dataclass(frozen=True)
class Unmet:
    index: int
    kind: str
    cause: str
    reason: str
    observation_id: str | None = None

    def as_details(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "index": self.index,
            "kind": self.kind,
            "cause": self.cause,
            "reason": self.reason,
        }
        if self.observation_id is not None:
            details["observationId"] = self.observation_id
        return details


def _resolved(template: str, context: DecisionContext) -> Any:
    """A selector's value, or ``None`` when it refers to nothing."""
    try:
        value = render(template, context.resolve)
    except ValidationError:  # a required (!) expression resolved to nothing
        return None
    return value if value not in ("", None) else None


def _reason(precondition: Precondition, context: DecisionContext) -> str:
    try:
        return str(render(precondition.reason, context.resolve))
    except ValidationError:
        return precondition.reason


async def newest_observation(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kind: str,
    source: str | None,
    task_id: str | None,
    external_ref_id: str | None,
) -> tuple[uuid.UUID, dict[str, Any]] | None:
    """The newest ``observation.recorded`` that matches, by ``observedAt``.

    The hot journal only: an observation moved to the archive (ADR-0038) is
    too old to vouch for anything, and missing counts as "does not hold".
    """
    payload = Event.payload
    observed_at = func.coalesce(
        cast(payload["observedAt"].astext, DateTime(timezone=True)), Event.occurred_at
    )
    query = select(Event.entity_id, payload).where(
        Event.tenant_id == tenant_id,
        Event.entity_type == "observation",
        Event.event_type == OBSERVATION_EVENT,
        payload["kind"].astext == kind,
    )
    if source is not None:
        query = query.where(payload["source"].astext == source)
    if task_id is not None:
        query = query.where(payload["taskId"].astext == task_id)
    if external_ref_id is not None:
        query = query.where(payload["externalRef"]["id"].astext == external_ref_id)
    row = (
        await session.execute(query.order_by(observed_at.desc(), Event.sequence.desc()).limit(1))
    ).first()
    if row is None:
        return None
    return row[0], dict(row[1] or {})


async def _unmet(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    index: int,
    precondition: Precondition,
    context: DecisionContext,
) -> Unmet | None:
    def unmet(cause: str, observation_id: uuid.UUID | None = None) -> Unmet:
        return Unmet(
            index=index,
            kind=precondition.kind,
            cause=cause,
            reason=_reason(precondition, context),
            observation_id=str(observation_id) if observation_id else None,
        )

    task_id: str | None = None
    external_ref_id: str | None = None
    if precondition.task is not None:
        task_id = _resolved(precondition.task, context)
        if task_id is None:
            return unmet(CAUSE_UNRESOLVED)
    if precondition.external_ref is not None:
        external_ref_id = _resolved(precondition.external_ref, context)
        if external_ref_id is None:
            return unmet(CAUSE_UNRESOLVED)
    if precondition.task is None and precondition.external_ref is None:
        task_id = str(context.task_id)

    found = await newest_observation(
        session,
        tenant_id,
        kind=precondition.kind,
        source=precondition.source,
        task_id=None if task_id is None else str(task_id),
        external_ref_id=None if external_ref_id is None else str(external_ref_id),
    )
    if found is None:
        return unmet(CAUSE_NO_OBSERVATION)
    observation_id, payload = found
    if precondition.condition is None:
        return None
    view = {**payload, "id": str(observation_id)}

    def resolve(path: VarPath) -> Any:
        return walk(view, path.segments) if path.root == OBSERVATION_ROOT else None

    try:
        holds = evaluate(precondition.condition, resolve, roots=frozenset({OBSERVATION_ROOT}))
    except ConditionError:
        return unmet(CAUSE_CONDITION_ERROR, observation_id)
    return None if holds else unmet(CAUSE_CONDITION_FALSE, observation_id)


async def require_preconditions(
    session: AsyncSession, ctx: AuthContext, approval: Approval
) -> None:
    """Refuse ``approve`` of a gate whose type's preconditions do not hold."""
    if not approval.gate or approval.task_id is None:
        return
    task = await session.get(Task, approval.task_id)
    if task is None:  # pragma: no cover - forbidden by the foreign key
        return
    task_type = await task_type_of(session, task)
    schema = parse_approval_schema(dict(task_type.approval_schema or {}))
    preconditions = schema.preconditions_for(DEFAULT_GATE, "approved")
    if not preconditions:
        return
    context = await base_context(session, approval, "approved")
    await read_context(
        session,
        ctx,
        context,
        (),
        extra=tuple(path for item in preconditions for path in item.paths()),
    )
    failed = [
        unmet
        for index, precondition in enumerate(preconditions)
        if (unmet := await _unmet(session, ctx.tenant_id, index, precondition, context))
    ]
    if failed:
        raise ConflictError(
            APPROVAL_PRECONDITION_FAILED,
            f"The approval cannot be approved yet: {failed[0].reason}",
            details={
                "approvalId": str(approval.id),
                "failed": [item.as_details() for item in failed],
            },
        )
