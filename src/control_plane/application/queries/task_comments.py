"""Work item comment read side: the thread, one reply, and its edit history.

Every query is scoped to the caller's tenant AND to the task in the path, so a
comment id guessed from another tenant resolves to nothing rather than to a
row (ADR-0050).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.relations import resolve_task
from control_plane.application.common import make_thread_cursor, parse_thread_cursor
from control_plane.application.queries.lists import Page, clamp_limit
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError
from control_plane.infrastructure.db.models import TaskComment, TaskCommentRevision


async def list_comments(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[TaskComment]:
    """One page of a thread, OLDEST first.

    A discussion is read forward, and forward order is also what makes the page
    stable while people are still talking: a comment written during pagination
    lands strictly after the cursor, so it is delivered on a later page instead
    of shifting the pages already read (newest-first would drop it silently
    ahead of the first page).
    """
    await authorize(ctx, Permission.TASKS_READ)
    task = await resolve_task(session, ctx, task_ref)

    effective_limit = clamp_limit(limit)
    stmt = select(TaskComment).where(
        TaskComment.tenant_id == ctx.tenant_id,
        TaskComment.task_id == task.id,
    )
    if cursor is not None:
        created_at, comment_id = parse_thread_cursor(cursor)
        stmt = stmt.where(
            (TaskComment.created_at > created_at)
            | ((TaskComment.created_at == created_at) & (TaskComment.id > comment_id))
        )
    stmt = stmt.order_by(TaskComment.created_at.asc(), TaskComment.id.asc()).limit(
        effective_limit + 1
    )

    rows = list((await session.scalars(stmt)).all())
    next_cursor: str | None = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_thread_cursor(rows[-1].created_at, rows[-1].id)
    return Page(items=rows, next_cursor=next_cursor)


async def get_comment(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    comment_id: uuid.UUID,
) -> TaskComment:
    await authorize(ctx, Permission.TASKS_READ)
    task = await resolve_task(session, ctx, task_ref)
    comment = await session.scalar(
        select(TaskComment).where(
            TaskComment.id == comment_id,
            TaskComment.tenant_id == ctx.tenant_id,
            TaskComment.task_id == task.id,
        )
    )
    if comment is None:
        raise NotFoundError("Comment not found", details={"commentId": str(comment_id)})
    return comment


async def list_comment_revisions(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    task_ref: str,
    comment_id: uuid.UUID,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[TaskCommentRevision]:
    """The superseded versions of one comment, oldest first.

    The audit of an edit is only an audit if it can be read; the prior text is
    the same class of data as the current text, so it is governed by the same
    read permission and not by a separate one.
    """
    comment = await get_comment(session, ctx, task_ref=task_ref, comment_id=comment_id)

    effective_limit = clamp_limit(limit)
    stmt = select(TaskCommentRevision).where(
        TaskCommentRevision.tenant_id == ctx.tenant_id,
        TaskCommentRevision.comment_id == comment.id,
    )
    if cursor is not None:
        created_at, revision_id = parse_thread_cursor(cursor)
        stmt = stmt.where(
            (TaskCommentRevision.created_at > created_at)
            | (
                (TaskCommentRevision.created_at == created_at)
                & (TaskCommentRevision.id > revision_id)
            )
        )
    stmt = stmt.order_by(TaskCommentRevision.created_at.asc(), TaskCommentRevision.id.asc()).limit(
        effective_limit + 1
    )

    rows = list((await session.scalars(stmt)).all())
    next_cursor: str | None = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_thread_cursor(rows[-1].created_at, rows[-1].id)
    return Page(items=rows, next_cursor=next_cursor)
