"""Work item type registry endpoints (ADR-0048).

Shaped like ``/project-templates``: a POST creates the NEXT version of a key,
never edits one, and ``:deprecate`` retires a version from resolution without
touching the tasks that already carry it.
"""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PageOut,
    TaskTypeCreateRequest,
    TaskTypeOut,
    dump,
    page_body,
    work_document,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import authorize
from control_plane.application.commands import task_types as commands
from control_plane.application.common import make_created_cursor, parse_created_cursor
from control_plane.application.queries.lists import clamp_limit
from control_plane.domain.enums import Permission
from control_plane.infrastructure.db.models import TaskType

router = APIRouter(tags=["task-types"])


@router.post(
    "/task-types",
    response_model=TaskTypeOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Create the next immutable version of a work item type",
)
async def create_task_type(
    payload: TaskTypeCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        task_type = await commands.create_task_type_version(
            db,
            ctx,
            key=payload.key,
            display_name=payload.display_name,
            description=payload.description,
            field_schema=payload.field_schema,
            lifecycle_schema=payload.lifecycle_schema,
            execution=payload.execution,
            approval_schema=payload.approval_schema,
            context_schema=payload.context_schema,
            instructions=payload.instructions,
            completion_schema=payload.completion_schema,
            artifact_schema=payload.artifact_schema,
            acceptance=work_document(payload.acceptance),
        )
        return 201, dump(TaskTypeOut, task_type)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/task-types", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_task_types(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    key: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> JSONResponse:
    await authorize(ctx, Permission.TASK_TYPES_READ)
    effective_limit = clamp_limit(limit)
    stmt = select(TaskType).where(TaskType.tenant_id == ctx.tenant_id)
    if key is not None:
        stmt = stmt.where(TaskType.key == key)
    if status is not None:
        stmt = stmt.where(TaskType.status == status)
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(
            (TaskType.created_at < created_at)
            | ((TaskType.created_at == created_at) & (TaskType.id < entity_id))
        )
    stmt = stmt.order_by(TaskType.created_at.desc(), TaskType.id.desc()).limit(effective_limit + 1)
    rows = list((await db.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > effective_limit:
        rows = rows[:effective_limit]
        next_cursor = make_created_cursor(rows[-1].created_at, rows[-1].id)
    return JSONResponse(page_body([dump(TaskTypeOut, t) for t in rows], next_cursor))


@router.get("/task-types/{type_id}", response_model=TaskTypeOut, responses=ERROR_RESPONSES)
async def get_task_type(type_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    await authorize(ctx, Permission.TASK_TYPES_READ)
    return JSONResponse(dump(TaskTypeOut, await commands.get_tenant_task_type(db, ctx, type_id)))


@router.post(
    "/task-types/{type_id}:deprecate", response_model=TaskTypeOut, responses=ERROR_RESPONSES
)
async def deprecate_task_type(
    type_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        task_type = await commands.deprecate_task_type(db, ctx, type_id=type_id)
        return 200, dump(TaskTypeOut, task_type)

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )
