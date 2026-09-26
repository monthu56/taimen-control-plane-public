"""Artifact endpoints: append-oriented work products.

Records are created and read here; their bytes are uploaded through
``PUT /artifact-contents``, handed out by ``GET /artifacts/{id}/content`` and
removed by an administrator with ``:purge-content`` (CP-ADR-0072).
"""

import uuid
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import (
    AuthDep,
    ContentStoreDep,
    DbDep,
    SessionFactoryDep,
    SettingsDep,
)
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ArtifactCreateRequest,
    ArtifactOut,
    ArtifactPurgeContentRequest,
    PageOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import artifacts as commands
from control_plane.application.queries import execution as queries
from control_plane.infrastructure.db.engine import transaction

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
            content_ref_value=payload.content_ref,
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
async def get_artifact(
    artifact_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    for_task: str | None = Query(default=None, alias="forTask"),
) -> JSONResponse:
    artifact = await queries.get_artifact(db, ctx, artifact_id, for_task_ref=for_task)
    return JSONResponse(dump(ArtifactOut, artifact))


# Media types a browser would execute or render as a document: always a
# download, never displayed in the API's origin (CP-ADR-0072 §5).
_ACTIVE_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xml",
        "application/xml",
        "text/javascript",
        "application/javascript",
    }
)


def is_active_content(media_type: str) -> bool:
    base = media_type.split(";", 1)[0].strip().lower()
    return base in _ACTIVE_MEDIA_TYPES or base.endswith("+xml")


def content_disposition(name: str, media_type: str) -> str:
    kind = "attachment" if is_active_content(media_type) else "inline"
    return f"{kind}; filename*=UTF-8''{quote(name, safe='')}"


@router.get(
    "/artifacts/{artifact_id}/content",
    responses={
        **ERROR_RESPONSES,
        200: {"content": {"application/octet-stream": {}}, "description": "The bytes"},
    },
    response_class=StreamingResponse,
    summary="Stream the stored content of an artifact",
)
async def get_artifact_content(
    artifact_id: uuid.UUID,
    ctx: AuthDep,
    session_factory: SessionFactoryDep,
    store: ContentStoreDep,
    for_task: str | None = Query(default=None, alias="forTask"),
) -> StreamingResponse:
    opened: commands.OpenedContent | None = None
    try:
        # The read is journaled and committed before the first byte leaves.
        async with transaction(session_factory) as session:
            opened = await commands.open_content(
                session, ctx, store, artifact_id, for_task_ref=for_task
            )
    except BaseException:
        if opened is not None:
            await opened.stream.aclose()
        raise
    artifact = opened.artifact
    media_type = artifact.media_type or "application/octet-stream"
    return StreamingResponse(
        opened.stream.chunks(),
        headers={
            "Content-Type": media_type,
            "Content-Length": str(opened.stream.size),
            "Content-Disposition": content_disposition(artifact.name, media_type),
            "ETag": f'"sha256:{artifact.sha256}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )


@router.post(
    "/artifacts/{artifact_id}:purge-content",
    response_model=ArtifactOut,
    responses=ERROR_RESPONSES,
    summary="Remove the stored bytes of an artifact (admin); the record stays",
)
async def purge_artifact_content(
    artifact_id: uuid.UUID,
    payload: ArtifactPurgeContentRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    store: ContentStoreDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        artifact = await commands.purge_content(db, ctx, store, artifact_id, reason=payload.reason)
        return 200, dump(ArtifactOut, artifact)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
