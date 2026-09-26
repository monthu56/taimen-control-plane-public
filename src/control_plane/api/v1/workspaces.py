"""Workspace endpoints: hierarchy, archive, move, members."""

import uuid

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PageOut,
    WorkspaceCreateRequest,
    WorkspaceMemberOut,
    WorkspaceMemberRequest,
    WorkspaceMoveRequest,
    WorkspaceOut,
    WorkspaceUpdateRequest,
    dump,
    page_body,
)
from control_plane.api.write_flow import as_no_content, execute_write
from control_plane.application.commands import workspaces as commands
from control_plane.application.queries import org as queries
from control_plane.application.queries import projects as project_queries

router = APIRouter(tags=["workspaces"])


@router.post("/workspaces", response_model=WorkspaceOut, status_code=201, responses=ERROR_RESPONSES)
async def create_workspace(
    payload: WorkspaceCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace = await commands.create_workspace(
            db,
            ctx,
            slug=payload.slug,
            name=payload.name,
            description=payload.description,
            parent_id=payload.parent_id,
            type_id=payload.type_id,
            type_key=payload.type_key,
            custom_fields=payload.custom_fields,
        )
        return 201, dump(WorkspaceOut, workspace)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/workspaces", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_workspaces(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    parent_id: uuid.UUID | None = Query(default=None, alias="parentId"),
    roots_only: bool = Query(default=False, alias="rootsOnly"),
    status: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_workspaces(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        parent_id=parent_id,
        roots_only=roots_only,
        status=status,
    )
    return JSONResponse(page_body([dump(WorkspaceOut, w) for w in page.items], page.next_cursor))


@router.get("/workspaces/tree", responses=ERROR_RESPONSES)
async def get_workspace_tree(
    ctx: AuthDep,
    db: DbDep,
    root_id: uuid.UUID | None = Query(default=None, alias="rootId"),
    depth: int | None = Query(default=None, ge=0, le=64),
    include_archived: bool = Query(default=False, alias="includeArchived"),
    include_projects: bool = Query(default=True, alias="includeProjects"),
) -> JSONResponse:
    """The whole (sub)tree in one query, siblings in stable ``(slug, id)`` order."""
    roots = await project_queries.workspace_tree(
        db,
        ctx,
        root_id=root_id,
        depth=depth,
        include_archived=include_archived,
        include_projects=include_projects,
    )
    return JSONResponse({"roots": roots})


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceOut, responses=ERROR_RESPONSES)
async def get_workspace(workspace_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    workspace = await queries.get_workspace(db, ctx, workspace_id)
    return JSONResponse(
        dump(WorkspaceOut, workspace),
        headers={"ETag": format_etag("workspace", workspace.version)},
    )


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceOut, responses=ERROR_RESPONSES)
async def update_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, "workspace")

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace = await commands.update_workspace(
            db,
            ctx,
            workspace_id=workspace_id,
            expected_version=expected_version,
            name=payload.name,
            description=payload.description,
            slug=payload.slug,
            type_id=payload.type_id,
            type_key=payload.type_key,
            custom_fields=payload.custom_fields,
        )
        return 200, dump(WorkspaceOut, workspace)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/workspaces/{workspace_id}:archive",
    response_model=WorkspaceOut,
    responses=ERROR_RESPONSES,
)
async def archive_workspace(
    workspace_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace = await commands.archive_workspace(db, ctx, workspace_id=workspace_id)
        return 200, dump(WorkspaceOut, workspace)

    return await execute_write(
        request, ctx, settings, session_factory, canonical_body="", executor=executor
    )


@router.post(
    "/workspaces/{workspace_id}:move",
    response_model=WorkspaceOut,
    responses=ERROR_RESPONSES,
)
async def move_workspace(
    workspace_id: uuid.UUID,
    payload: WorkspaceMoveRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        workspace = await commands.move_workspace(
            db, ctx, workspace_id=workspace_id, new_parent_id=payload.new_parent_id
        )
        return 200, dump(WorkspaceOut, workspace)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/workspaces/{workspace_id}/members",
    response_model=WorkspaceMemberOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def add_member(
    workspace_id: uuid.UUID,
    payload: WorkspaceMemberRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        member = await commands.add_workspace_member(
            db, ctx, workspace_id=workspace_id, principal_id=payload.principal_id
        )
        return 201, dump(WorkspaceMemberOut, member)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/workspaces/{workspace_id}/members", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_members(
    workspace_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_workspace_members(db, ctx, workspace_id, limit=limit, cursor=cursor)
    return JSONResponse(
        page_body([dump(WorkspaceMemberOut, m) for m in page.items], page.next_cursor)
    )


@router.post(
    "/workspaces/{workspace_id}/members/{principal_id}:remove",
    status_code=204,
    responses=ERROR_RESPONSES,
)
async def remove_member(
    workspace_id: uuid.UUID,
    principal_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> Response:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await commands.remove_workspace_member(
            db, ctx, workspace_id=workspace_id, principal_id=principal_id
        )
        return 204, {}

    return as_no_content(
        await execute_write(
            request, ctx, settings, session_factory, canonical_body="", executor=executor
        )
    )
