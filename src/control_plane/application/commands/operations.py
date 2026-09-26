"""Operator actions on delivery state and the event journal.

Two capabilities that used to require a DBA and a hand-written UPDATE:

* **redrive / rebuild** a parked Context Adapter tenant (ADR-0037). Redrive
  clears the parked marker and retries *the same position*; rebuild moves the
  cursor strictly BACKWARDS. Neither can move a cursor forward, so no API in
  this module can skip a poison event and open a permanent gap.
* **archive / prune** the journal under a safe horizon (ADR-0038): never past
  what any consumer still needs, never silently.

Every action writes a domain event in the same transaction — an operator
intervention is exactly the kind of thing an audit needs to see.
"""

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import utcnow
from control_plane.application.event_cursor import (
    EventPosition,
    LegacyFloor,
    decode_cursor,
    encode_position,
)
from control_plane.application.events import record_event
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.infrastructure.db.models import EventConsumerCursor, EventJournalFloor
from control_plane.worker.context_adapter import CONSUMER_NAME


async def _cursor_row(
    session: AsyncSession, tenant_id: uuid.UUID, *, for_update: bool = False
) -> EventConsumerCursor:
    row = await session.get(
        EventConsumerCursor, (CONSUMER_NAME, tenant_id), with_for_update=for_update
    )
    if row is None:
        raise NotFoundError(
            "No adapter cursor for this tenant",
            details={"consumer": CONSUMER_NAME, "tenantId": str(tenant_id)},
        )
    return row


def _require_own_tenant(ctx: AuthContext, tenant_id: uuid.UUID) -> None:
    """A foreign tenant id is a 404, not a 403: no cross-tenant enumeration."""
    if tenant_id != ctx.tenant_id:
        raise NotFoundError(
            "No adapter cursor for this tenant", details={"tenantId": str(tenant_id)}
        )


async def redrive_adapter(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    tenant_id: uuid.UUID,
    reason: str = "operator_redrive",
) -> EventConsumerCursor:
    """Clear the parked marker and retry the SAME position. Idempotent."""
    await authorize(ctx, Permission.OPERATIONS_MANAGE)
    _require_own_tenant(ctx, tenant_id)
    row = await _cursor_row(session, tenant_id, for_update=True)

    was_parked = row.parked_at is not None or row.next_attempt_at is not None
    parked_reason = row.parked_reason
    parked_event_id = row.parked_event_id
    # The cursor itself is deliberately untouched.
    row.parked_at = None
    row.parked_reason = None
    row.parked_event_id = None
    row.failure_count = 0
    row.next_attempt_at = None
    row.updated_at = utcnow()
    metadata = dict(row.metadata_ or {})
    metadata["last_error"] = None
    metadata["redrives_total"] = int(metadata.get("redrives_total", 0)) + 1
    metadata["last_redrive_at"] = utcnow().isoformat()
    row.metadata_ = metadata

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="context_adapter.redriven",
        entity_type="event_consumer",
        entity_id=ctx.tenant_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "consumer": CONSUMER_NAME,
            "wasParked": was_parked,
            "parkedReason": parked_reason,
            "parkedEventId": str(parked_event_id) if parked_event_id else None,
            "cursor": encode_position(EventPosition(row.tx_id, row.sequence)),
            "reason": reason,
        },
    )
    return row


async def rebuild_adapter(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    tenant_id: uuid.UUID,
    cursor: str | None = None,
    reason: str = "operator_rebuild",
) -> EventConsumerCursor:
    """Move the cursor strictly backwards so the tenant re-delivers history."""
    await authorize(ctx, Permission.OPERATIONS_MANAGE)
    _require_own_tenant(ctx, tenant_id)
    row = await _cursor_row(session, tenant_id, for_update=True)

    target = EventPosition(0, 0)
    if cursor is not None:
        decoded = decode_cursor(cursor)
        if isinstance(decoded, LegacyFloor):
            raise ValidationError(
                "invalid_cursor",
                "Rebuild requires a positional cursor, not a legacy sequence floor",
            )
        target = decoded
    if (target.tx_id, target.sequence) > (row.tx_id, row.sequence):
        # Otherwise rebuild would become a way to skip a poison event.
        raise ValidationError(
            "cursor_must_not_advance",
            "Rebuild may only move the cursor backwards",
            details={
                "currentCursor": encode_position(EventPosition(row.tx_id, row.sequence)),
                "requestedCursor": encode_position(target),
            },
        )
    await assert_cursor_servable(session, ctx.tenant_id, target)

    previous = encode_position(EventPosition(row.tx_id, row.sequence))
    row.tx_id = target.tx_id
    row.sequence = target.sequence
    row.parked_at = None
    row.parked_reason = None
    row.parked_event_id = None
    row.failure_count = 0
    row.next_attempt_at = None
    row.updated_at = utcnow()
    metadata = dict(row.metadata_ or {})
    metadata["last_error"] = None
    metadata["rebuilds_total"] = int(metadata.get("rebuilds_total", 0)) + 1
    metadata["last_rebuild_at"] = utcnow().isoformat()
    row.metadata_ = metadata

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="context_adapter.rebuilt",
        entity_type="event_consumer",
        entity_id=ctx.tenant_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "consumer": CONSUMER_NAME,
            "fromCursor": previous,
            "toCursor": encode_position(target),
            "reason": reason,
        },
    )
    return row


# --- journal retention --------------------------------------------------------


async def journal_floor(
    session: AsyncSession, tenant_id: uuid.UUID, *, for_update: bool = False
) -> EventJournalFloor:
    """The singleton floor row.

    ``for_update`` serializes every decision that depends on it — concurrent
    archive/prune runs and a racing rebuild all queue on this one row, so the
    floor can never move backwards and a cursor can never be left underneath
    it (ADR-0038).
    """
    row = await session.get(EventJournalFloor, tenant_id, with_for_update=for_update)
    if row is None:  # pragma: no cover - seeded by the migration
        row = EventJournalFloor(
            id=1, journal_tx_id=0, journal_sequence=0, archive_tx_id=0, archive_sequence=0
        )
        session.add(row)
        await session.flush()
    return row


async def assert_cursor_servable(
    session: AsyncSession, tenant_id: uuid.UUID, position: EventPosition
) -> None:
    """A cursor below the archive floor cannot be served — say so explicitly.

    Takes the floor row FOR UPDATE so a prune cannot slip underneath a rebuild
    between the check and the write.
    """
    floor = await journal_floor(session, tenant_id, for_update=True)
    if (position.tx_id, position.sequence) < (floor.archive_tx_id, floor.archive_sequence):
        raise ValidationError(
            "cursor_below_journal_floor",
            "The requested cursor is older than the retained journal",
            details={
                "floorCursor": encode_position(
                    EventPosition(floor.archive_tx_id, floor.archive_sequence)
                ),
                "requestedCursor": encode_position(position),
            },
        )


async def _consumer_floor(session: AsyncSession, tenant_id: uuid.UUID) -> EventPosition | None:
    """Lowest confirmed position across this tenant's consumers — the ceiling.

    Rows are locked so a concurrent rebuild cannot lower a cursor underneath
    the horizon we are about to act on.
    """
    row = (
        await session.execute(
            text(
                "SELECT tx_id, sequence FROM event_consumer_cursors "
                "WHERE tenant_id = :tenant ORDER BY tx_id ASC, sequence ASC LIMIT 1"
                " FOR UPDATE"
            ),
            {"tenant": tenant_id},
        )
    ).first()
    if row is None:
        return None
    return EventPosition(tx_id=row[0], sequence=row[1])


async def _horizon(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    settings: Settings,
    before_seconds: int | None,
    max_events: int | None,
) -> tuple[EventPosition | None, int]:
    """Highest position that is both old enough and confirmed by all consumers."""
    min_age = (
        settings.journal_retention_min_age_seconds
        if before_seconds is None
        else max(before_seconds, 0)
    )
    cutoff = utcnow() - timedelta(seconds=min_age)
    consumer = await _consumer_floor(session, tenant_id)
    if consumer is None:
        # No consumer has ever registered: nothing is confirmed, so nothing is
        # safe to move. Refusing beats guessing.
        raise ConflictError(
            "retention_blocked_by_consumer",
            "No consumer cursor exists; nothing is confirmed as delivered",
            details={"consumer": CONSUMER_NAME},
        )
    # The outbox still references events by FK, and an undelivered record is
    # work in flight: the horizon stops just below the oldest such event.
    pending = (
        await session.execute(
            text(
                "SELECT e.tx_id, e.sequence FROM outbox o JOIN events e ON e.id = o.event_id"
                " WHERE o.delivered_at IS NULL AND o.tenant_id = :tenant"
                " ORDER BY e.tx_id ASC, e.sequence ASC LIMIT 1"
            ),
            {"tenant": tenant_id},
        )
    ).first()
    ceiling = (consumer.tx_id, consumer.sequence)
    if pending is not None:
        ceiling = min(ceiling, (pending[0], pending[1] - 1))
    stmt = text(
        """
        SELECT tx_id, sequence FROM events
         WHERE tenant_id = :tenant
           AND occurred_at <= :cutoff
           AND (tx_id, sequence) <= (:tx, :seq)
         ORDER BY tx_id DESC, sequence DESC
         LIMIT 1
        """
    )
    row = (
        await session.execute(
            stmt,
            {"tenant": tenant_id, "cutoff": cutoff, "tx": ceiling[0], "seq": ceiling[1]},
        )
    ).first()
    if row is None:
        return None, min_age
    horizon = EventPosition(tx_id=row[0], sequence=row[1])
    if max_events is not None:
        capped = (
            await session.execute(
                text(
                    """
                    SELECT tx_id, sequence FROM events
                     WHERE tenant_id = :tenant AND (tx_id, sequence) <= (:tx, :seq)
                     ORDER BY tx_id ASC, sequence ASC
                     LIMIT 1 OFFSET :offset
                    """
                ),
                {
                    "tenant": tenant_id,
                    "tx": horizon.tx_id,
                    "seq": horizon.sequence,
                    "offset": max_events - 1,
                },
            )
        ).first()
        if capped is not None:
            horizon = EventPosition(tx_id=capped[0], sequence=capped[1])
    return horizon, min_age


async def archive_journal(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    before_seconds: int | None = None,
    max_events: int | None = None,
) -> dict[str, Any]:
    """Move confirmed, aged events out of ``events`` into ``event_archive``."""
    await authorize(ctx, Permission.OPERATIONS_MANAGE)
    # Lock FIRST: the horizon is computed from consumer cursors, so a
    # concurrent archive/prune/rebuild must not interleave with it.
    floor = await journal_floor(session, ctx.tenant_id, for_update=True)
    horizon, min_age = await _horizon(session, ctx.tenant_id, settings, before_seconds, max_events)
    if horizon is None:
        return {
            "archived": 0,
            "journalFloorCursor": encode_position(
                EventPosition(floor.journal_tx_id, floor.journal_sequence)
            ),
            "archiveFloorCursor": encode_position(
                EventPosition(floor.archive_tx_id, floor.archive_sequence)
            ),
            "minAgeSeconds": min_age,
        }

    # The journal trigger only lets DELETE through while this
    # transaction-local flag is set — the single audited exception to
    # append-only (ADR-0038). SET LOCAL dies with the transaction.
    await session.execute(text("SET LOCAL cp.journal_archiving = 'on'"))
    # Delivered outbox rows are delivery bookkeeping, not audit: they go with
    # the event. Undelivered ones cannot be here — the horizon excluded them.
    await session.execute(
        text(
            "DELETE FROM outbox WHERE tenant_id = :tenant AND event_id IN"
            " (SELECT id FROM events WHERE tenant_id = :tenant"
            "  AND (tx_id, sequence) <= (:tx, :seq))"
        ),
        {"tenant": ctx.tenant_id, "tx": horizon.tx_id, "seq": horizon.sequence},
    )
    moved = await session.execute(
        text(
            """
            WITH moved AS (
                DELETE FROM events
                 WHERE tenant_id = :tenant AND (tx_id, sequence) <= (:tx, :seq)
                RETURNING *
            )
            INSERT INTO event_archive (
                sequence, tx_id, id, tenant_id, event_type, entity_type, entity_id,
                actor_id, session_id, correlation_id, causation_id, request_id,
                trace_run_id, iam_actor_id, workspace_id, schema_version, payload,
                occurred_at, archived_at
            )
            SELECT sequence, tx_id, id, tenant_id, event_type, entity_type, entity_id,
                   actor_id, session_id, correlation_id, causation_id, request_id,
                   trace_run_id, iam_actor_id, workspace_id, schema_version, payload,
                   occurred_at, now()
              FROM moved
            RETURNING sequence
            """
        ),
        {"tenant": ctx.tenant_id, "tx": horizon.tx_id, "seq": horizon.sequence},
    )
    count = len(moved.all())
    if (horizon.tx_id, horizon.sequence) > (floor.journal_tx_id, floor.journal_sequence):
        floor.journal_tx_id = horizon.tx_id
        floor.journal_sequence = horizon.sequence
        floor.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="event_journal.archived",
        entity_type="event_journal",
        entity_id=ctx.tenant_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "archived": count,
            "throughCursor": encode_position(horizon),
            "minAgeSeconds": min_age,
        },
    )
    return {
        "archived": count,
        "journalFloorCursor": encode_position(
            EventPosition(floor.journal_tx_id, floor.journal_sequence)
        ),
        "archiveFloorCursor": encode_position(
            EventPosition(floor.archive_tx_id, floor.archive_sequence)
        ),
        "minAgeSeconds": min_age,
    }


async def prune_journal(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    before_seconds: int | None = None,
    max_events: int | None = None,
) -> dict[str, Any]:
    """Physically delete archived events. The only lossy operation here."""
    await authorize(ctx, Permission.OPERATIONS_MANAGE)
    min_age = (
        settings.journal_retention_min_age_seconds
        if before_seconds is None
        else max(before_seconds, 0)
    )
    cutoff = utcnow() - timedelta(seconds=min_age)
    floor = await journal_floor(session, ctx.tenant_id, for_update=True)
    consumer = await _consumer_floor(session, ctx.tenant_id)
    if consumer is None:
        raise ConflictError(
            "retention_blocked_by_consumer",
            "No consumer cursor exists; nothing is confirmed as delivered",
            details={"consumer": CONSUMER_NAME},
        )
    row = (
        await session.execute(
            text(
                """
                SELECT tx_id, sequence FROM event_archive
                 WHERE tenant_id = :tenant
                   AND occurred_at <= :cutoff
                   AND (tx_id, sequence) <= (:tx, :seq)
                 ORDER BY tx_id DESC, sequence DESC
                 LIMIT 1
                """
            ),
            {
                "tenant": ctx.tenant_id,
                "cutoff": cutoff,
                "tx": consumer.tx_id,
                "seq": consumer.sequence,
            },
        )
    ).first()
    if row is None:
        return {
            "pruned": 0,
            "archiveFloorCursor": encode_position(
                EventPosition(floor.archive_tx_id, floor.archive_sequence)
            ),
        }
    horizon = EventPosition(tx_id=row[0], sequence=row[1])
    if max_events is not None:
        capped = (
            await session.execute(
                text(
                    """
                    SELECT tx_id, sequence FROM event_archive
                     WHERE tenant_id = :tenant AND (tx_id, sequence) <= (:tx, :seq)
                     ORDER BY tx_id ASC, sequence ASC
                     LIMIT 1 OFFSET :offset
                    """
                ),
                {
                    "tenant": ctx.tenant_id,
                    "tx": horizon.tx_id,
                    "seq": horizon.sequence,
                    "offset": max_events - 1,
                },
            )
        ).first()
        if capped is not None:
            horizon = EventPosition(tx_id=capped[0], sequence=capped[1])

    deleted = await session.execute(
        text("DELETE FROM event_archive WHERE (tx_id, sequence) <= (:tx, :seq) RETURNING sequence"),
        {"tx": horizon.tx_id, "seq": horizon.sequence},
    )
    count = len(deleted.all())
    if (horizon.tx_id, horizon.sequence) > (floor.archive_tx_id, floor.archive_sequence):
        floor.archive_tx_id = horizon.tx_id
        floor.archive_sequence = horizon.sequence
        floor.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="event_journal.pruned",
        entity_type="event_journal",
        entity_id=ctx.tenant_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "pruned": count,
            "throughCursor": encode_position(horizon),
            "minAgeSeconds": min_age,
        },
    )
    return {
        "pruned": count,
        "archiveFloorCursor": encode_position(
            EventPosition(floor.archive_tx_id, floor.archive_sequence)
        ),
    }


async def adapter_diagnostics(session: AsyncSession, ctx: AuthContext) -> dict[str, Any]:
    """Read-only view of this tenant's delivery state (never mutates).

    ``operations.manage`` is accepted as well as ``operations.read``: the
    write endpoints return this body, and requiring read separately would
    make a key holding exactly the documented manage permission fail AFTER
    its mutation, rolling the un-park back.
    """
    await authorize(ctx, Permission.OPERATIONS_READ, Permission.OPERATIONS_MANAGE)
    row = await session.get(EventConsumerCursor, (CONSUMER_NAME, ctx.tenant_id))
    floor = await journal_floor(session, ctx.tenant_id)
    floor_body = {
        "journalFloorCursor": encode_position(
            EventPosition(floor.journal_tx_id, floor.journal_sequence)
        ),
        "archiveFloorCursor": encode_position(
            EventPosition(floor.archive_tx_id, floor.archive_sequence)
        ),
    }
    if row is None:
        # Before the adapter's first cycle there is no cursor row yet. Return
        # the SAME shape rather than a shorter one: a client must not have to
        # branch on whether a key exists.
        return {
            "consumer": CONSUMER_NAME,
            "tenantId": str(ctx.tenant_id),
            "cursor": None,
            "lagEvents": None,
            "lagCapped": False,
            "parked": False,
            "parkedAt": None,
            "parkedReason": None,
            "parkedEventId": None,
            "failureCount": 0,
            "nextAttemptAt": None,
            "updatedAt": None,
            "deliveredTotal": 0,
            "duplicatesTotal": 0,
            "failuresTotal": 0,
            "redrivesTotal": 0,
            "lastDeliveryAt": None,
            "journal": floor_body,
        }
    position = EventPosition(row.tx_id, row.sequence)
    lag = await session.scalar(
        text(
            "SELECT count(*) FROM (SELECT 1 FROM events WHERE tenant_id = :tenant "
            "AND (tx_id, sequence) > (:tx, :seq) LIMIT 1000) c"
        ),
        {"tenant": ctx.tenant_id, "tx": row.tx_id, "seq": row.sequence},
    )
    metadata = row.metadata_ or {}
    return {
        "consumer": CONSUMER_NAME,
        "tenantId": str(ctx.tenant_id),
        "cursor": encode_position(position),
        "lagEvents": int(lag or 0),
        "lagCapped": int(lag or 0) >= 1000,
        "parked": row.parked_at is not None,
        "parkedAt": row.parked_at.isoformat() if row.parked_at else None,
        "parkedReason": row.parked_reason,
        "parkedEventId": str(row.parked_event_id) if row.parked_event_id else None,
        "failureCount": row.failure_count,
        "nextAttemptAt": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
        "updatedAt": row.updated_at.isoformat(),
        "deliveredTotal": int(metadata.get("delivered_total", 0)),
        "duplicatesTotal": int(metadata.get("duplicates_total", 0)),
        "failuresTotal": int(metadata.get("failures_total", 0)),
        "redrivesTotal": int(metadata.get("redrives_total", 0)),
        "lastDeliveryAt": metadata.get("last_delivery_at"),
        "journal": floor_body,
    }


async def parked_tenant_count(session: AsyncSession) -> int:
    return int(
        (
            await session.scalar(
                select(func.count())
                .select_from(EventConsumerCursor)
                .where(EventConsumerCursor.parked_at.is_not(None))
            )
        )
        or 0
    )
