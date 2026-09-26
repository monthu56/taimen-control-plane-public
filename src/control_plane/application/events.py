"""Domain event recording.

Every successful mutating command writes, in the SAME transaction:
new domain state, an ``events`` row, and (when delivery is required) an
``outbox`` row. A ``pg_notify`` is issued inside the transaction as well —
PostgreSQL delivers it to listeners only on commit, so subscribers are never
woken up for state that was rolled back.
"""

import json
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.common import new_uuid, utcnow
from control_plane.domain.event_catalog import current_version
from control_plane.infrastructure.db.models import (
    Approval,
    Artifact,
    Event,
    Goal,
    OutboxRecord,
    ProjectProfile,
    Role,
    Run,
    SkillInvocation,
    Task,
    TaskClaim,
    WorkRule,
)

NOTIFY_CHANNEL = "cp_events"


# IAM identity of the request actor, set by the IAM authentication path and read
# here so that every event carries it without touching each of its ~140 callers.
current_iam_actor: ContextVar[uuid.UUID | None] = ContextVar("current_iam_actor", default=None)


# Entities whose events belong to a workspace (CP-ADR-0068): the entity's own
# workspace, else the workspace of the task it hangs off.
_WORKSPACE_ENTITIES: dict[str, type[Any]] = {
    "task": Task,
    "approval": Approval,
    "artifact": Artifact,
    "goal": Goal,
    "rule": WorkRule,
    "role": Role,
    "project": ProjectProfile,
    "run": Run,
    "claim": TaskClaim,
    "skill_invocation": SkillInvocation,
}


async def event_workspace(
    session: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> uuid.UUID | None:
    """Workspace an event about this entity belongs to; ``None`` = tenant level.

    ``session.get`` answers from the identity map when the command has already
    loaded the entity (the usual case), so this rarely costs a query. Sessions
    do not autoflush: an entity the command has only just added is flushed
    first, so that the lookup sees it.
    """
    if entity_type == "workspace":
        return entity_id
    model = _WORKSPACE_ENTITIES.get(entity_type)
    if model is None:
        return None
    await session.flush()
    entity = await session.get(model, entity_id)
    if entity is None:
        return None
    workspace_id: uuid.UUID | None = getattr(entity, "workspace_id", None)
    task_id: uuid.UUID | None = getattr(entity, "task_id", None)
    if workspace_id is None and task_id is not None:
        task = await session.get(Task, task_id)
        workspace_id = task.workspace_id if task is not None else None
    return workspace_id


async def record_event(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    event_type: str,
    entity_type: str,
    entity_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    request_id: str,
    correlation_id: str = "",
    causation_id: str | None = None,
    session_id: uuid.UUID | None = None,
    trace_run_id: str | None = None,
    payload: dict[str, Any] | None = None,
    deliver: bool = True,
    event_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
) -> Event:
    """Append one domain event (and its outbox record) to the current transaction.

    ``event_id``/``occurred_at`` let a command that must reference the event
    before appending it (e.g. an observation dedup key) fix them up front.
    The type must be in the event catalog: its current version is stamped on
    the event, an unknown type is a programming error (CP-ADR-0068).
    """
    schema_version = current_version(event_type)
    workspace_id = await event_workspace(session, entity_type, entity_id)
    occurred_at = occurred_at or utcnow()
    event = Event(
        id=event_id or new_uuid(),
        tenant_id=tenant_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_id=actor_id,
        session_id=session_id,
        correlation_id=correlation_id or request_id,
        causation_id=causation_id,
        request_id=request_id,
        trace_run_id=trace_run_id or None,
        iam_actor_id=current_iam_actor.get() if actor_id is not None else None,
        workspace_id=workspace_id,
        schema_version=schema_version,
        payload=payload or {},
        occurred_at=occurred_at,
    )
    session.add(event)
    await session.flush()  # populates event.sequence

    if deliver:
        session.add(
            OutboxRecord(
                id=new_uuid(),
                tenant_id=tenant_id,
                event_id=event.id,
                topic=event_type,
                payload={
                    "eventId": str(event.id),
                    "sequence": event.sequence,
                    "type": event_type,
                    "schemaVersion": schema_version,
                    "tenantId": str(tenant_id),
                    "workspaceId": str(workspace_id) if workspace_id else None,
                    "entityType": entity_type,
                    "entityId": str(entity_id),
                    "occurredAt": occurred_at.isoformat(),
                    "traceRunId": trace_run_id or None,
                    "payload": payload or {},
                },
                available_at=occurred_at,
                created_at=occurred_at,
            )
        )

    notify_payload = json.dumps({"tenantId": str(tenant_id), "sequence": event.sequence})
    await session.execute(select(func.pg_notify(NOTIFY_CHANNEL, notify_payload)))
    return event
