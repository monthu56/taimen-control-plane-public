"""Artifact endpoints: append-oriented work products."""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ArtifactCreateRequest,
    ArtifactOut,
    PageOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import artifacts as commands
from control_plane.application.queries import execution as queries

router = APIRouter(tags=["artifacts"])


@router.post("/artifacts", response_model=ArtifactOut, status_code=201, responses=ERROR_RESPONSES)
async def create_artifact(
    payload: ArtifactCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        artifact = await commands.create_artifact(
            db,
            ctx,
            type_=payload.type,
            name=payload.name,
            task_ref=payload.task,
            run_id=payload.run_id,
            workspace_id=payload.workspace_id,
            uri=payload.uri,
            content=payload.content,
            metadata=payload.metadata,
            supersedes_artifact_id=payload.supersedes_artifact_id,
        )
        return 201, dump(ArtifactOut, artifact)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/artifacts", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_artifacts(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    task_id: uuid.UUID | None = Query(default=None, alias="taskId"),
    run_id: uuid.UUID | None = Query(default=None, alias="runId"),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    type_: str | None = Query(default=None, alias="type"),
) -> JSONResponse:
    page = await queries.list_artifacts(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        task_id=task_id,
        run_id=run_id,
        workspace_id=workspace_id,
        type_=type_,
    )
    return JSONResponse(page_body([dump(ArtifactOut, a) for a in page.items], page.next_cursor))


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut, responses=ERROR_RESPONSES)
async def get_artifact(artifact_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    artifact = await queries.get_artifact(db, ctx, artifact_id)
    return JSONResponse(dump(ArtifactOut, artifact))
