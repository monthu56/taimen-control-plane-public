"""Comment endpoints: the discussion of one work item (ADR-0050).

Comments are addressed under their task and never on their own, because the
task is part of a comment's identity: the same id reached through a different
work item is a mistake, not a shortcut.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PageOut,
    TaskCommentAuthorOut,
    TaskCommentCreateRequest,
    TaskCommentOut,
    TaskCommentRevisionOut,
    TaskCommentUpdateRequest,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import AuthContext
from control_plane.application.commands import task_comments as commands
from control_plane.application.queries import task_comments as queries
from control_plane.infrastructure.db.models import Principal, TaskComment

router = APIRouter(tags=["task-comments"])

_COMMENT_ENTITY = "comment"


def comment_body(comment: TaskComment, authors: dict[uuid.UUID, Principal]) -> dict[str, Any]:
    """A comment with its author in words (ADR-0050, amendment of 2026-09-30).

    The author is the caller at write time, a principal of the same tenant
    under a foreign key, so it is always among ``authors``.
    """
    principal = authors[comment.author_principal_id]
    author = TaskCommentAuthorOut(kind=principal.kind, display_name=principal.display_name)
    fields = {
        name: getattr(comment, name) for name in TaskCommentOut.model_fields if name != "author"
    }
    return TaskCommentOut.model_validate({**fields, "author": author}).model_dump(
        mode="json", by_alias=True
    )


async def _one_body(db: AsyncSession, ctx: AuthContext, comment: TaskComment) -> dict[str, Any]:
    return comment_body(comment, await queries.comment_authors(db, ctx, [comment]))


@router.post(
    "/tasks/{task_ref}/comments",
    response_model=TaskCommentOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Append a reply to a work item's discussion",
)
async def add_comment(
    task_ref: str,
    payload: TaskCommentCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        comment = await commands.add_comment(
            db,
            ctx,
            task_ref=task_ref,
            body=payload.body,
            run_id=payload.run_id,
            artifact_id=payload.artifact_id,
        )
        return 201, await _one_body(db, ctx, comment)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get(
    "/tasks/{task_ref}/comments",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="One page of the thread, oldest first",
)
async def list_comments(
    task_ref: str,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_comments(db, ctx, task_ref=task_ref, limit=limit, cursor=cursor)
    authors = await queries.comment_authors(db, ctx, page.items)
    return JSONResponse(page_body([comment_body(c, authors) for c in page.items], page.next_cursor))


@router.get(
    "/tasks/{task_ref}/comments/{comment_id}",
    response_model=TaskCommentOut,
    responses=ERROR_RESPONSES,
)
async def get_comment(
    task_ref: str, comment_id: uuid.UUID, ctx: AuthDep, db: DbDep
) -> JSONResponse:
    comment = await queries.get_comment(db, ctx, task_ref=task_ref, comment_id=comment_id)
    return JSONResponse(
        await _one_body(db, ctx, comment),
        headers={"ETag": format_etag(_COMMENT_ENTITY, comment.version)},
    )


@router.patch(
    "/tasks/{task_ref}/comments/{comment_id}",
    response_model=TaskCommentOut,
    responses=ERROR_RESPONSES,
    summary="Correct one's own comment; the previous version is kept",
)
async def edit_comment(
    task_ref: str,
    comment_id: uuid.UUID,
    payload: TaskCommentUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, _COMMENT_ENTITY)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        comment = await commands.edit_comment(
            db,
            ctx,
            task_ref=task_ref,
            comment_id=comment_id,
            body=payload.body,
            expected_version=expected_version,
        )
        return 200, await _one_body(db, ctx, comment)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get(
    "/tasks/{task_ref}/comments/{comment_id}/revisions",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="Superseded versions of a comment, oldest first",
)
async def list_comment_revisions(
    task_ref: str,
    comment_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_comment_revisions(
        db, ctx, task_ref=task_ref, comment_id=comment_id, limit=limit, cursor=cursor
    )
    return JSONResponse(
        page_body([dump(TaskCommentRevisionOut, r) for r in page.items], page.next_cursor)
    )
