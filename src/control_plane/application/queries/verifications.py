"""Read side of the verification stage: the attempts of one task (CP-ADR-0067)."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.relations import resolve_task
from control_plane.application.common import decode_cursor, encode_cursor
from control_plane.application.queries.lists import Page, clamp_limit
from control_plane.domain.enums import Permission
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.db.models import TaskVerification


async def list_verifications(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[TaskVerification]:
    """One page of a task's attempts, NEWEST first.

    The attempt number orders them and is the cursor: it is unique per task
    and never reused, so a page stays stable while new attempts open.
    """
    await authorize(ctx, Permission.TASKS_READ)
    task = await resolve_task(session, ctx, task_ref)
    effective_limit = clamp_limit(limit)
    stmt = select(TaskVerification).where(
        TaskVerification.tenant_id == ctx.tenant_id, TaskVerification.task_id == task.id
    )
    if cursor is not None:
        try:
            before = int(decode_cursor(cursor)["a"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
        stmt = stmt.where(TaskVerification.attempt < before)
    rows = list(
        (
            await session.scalars(
                stmt.order_by(TaskVerification.attempt.desc()).limit(effective_limit + 1)
            )
        ).all()
    )
    next_cursor: str | None = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = encode_cursor({"a": rows[-1].attempt})
    return Page(items=rows, next_cursor=next_cursor)
