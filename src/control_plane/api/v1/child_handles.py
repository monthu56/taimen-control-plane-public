"""Child run handle endpoints (HRS-7)."""

import json
import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ChildRunLaunchRequest,
    ChildRunRevokeRequest,
)
from control_plane.api.v1.task_bodies import task_body
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import child_runs as commands
from control_plane.application.queries import child_runs as queries
from control_plane.domain.errors import ValidationError

router = APIRouter(tags=["child-runs"])


@router.post(
    "/runs/{run_id}/child-handles",
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Launch a child run under this Run and return its durable handle",
)
async def launch_child(
    run_id: uuid.UUID,
    payload: ChildRunLaunchRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    raw_key = request.headers.get("Idempotency-Key")
    idempotency_key = raw_key.strip() if raw_key is not None else ""
    if not idempotency_key or len(idempotency_key) > 200:
        raise ValidationError(
            "idempotency_key_required",
            "Idempotency-Key is required and must be 1..200 characters",
        )

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await commands.launch_child_run(
            db,
            ctx,
            parent_run_id=run_id,
            correlation_id=payload.correlation_id,
            title=payload.title,
            description=payload.description,
            priority=payload.priority,
            workspace_id=payload.workspace_id,
            owner_id=payload.owner_id,
            assignee_id=payload.assignee_id,
            grant=payload.grant.model_dump(exclude_unset=True) if payload.grant else None,
            cancellation_policy=payload.cancellation_policy,
            expires_in_seconds=payload.expires_in_seconds,
        )
        view = await queries.get_child_handle_view(db, ctx, result.handle)
        return (201 if result.created else 200), {
            "childHandle": queries.child_handle_body(view),
            "childTask": await task_body(db, ctx, result.child_task),
            # Present exactly once, on the call that created the handle.
            "handleToken": result.token,
        }

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
    body = json.loads(bytes(response.body))
    response.headers["Location"] = f"/api/v1/child-handles/{body['childHandle']['id']}"
    return response


@router.get(
    "/runs/{run_id}/child-handles",
    responses=ERROR_RESPONSES,
    summary="List child handles launched by this Run",
)
async def list_child_handles(
    run_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    active: bool = Query(default=False),
) -> JSONResponse:
    page = await queries.list_child_handles(
        db, ctx, run_id, limit=limit, cursor=cursor, active_only=active
    )
    return JSONResponse(
        {
            "items": [queries.child_handle_body(view) for view in page.items],
            "nextCursor": page.next_cursor,
            "hasMore": page.has_more,
        }
    )


@router.get(
    "/child-handles/{ref}",
    responses=ERROR_RESPONSES,
    summary="Resolve a child handle by id or by ch1_ token",
)
async def resolve_child_handle(ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    view = await queries.resolve_child_handle(db, ctx, ref)
    return JSONResponse({"childHandle": queries.child_handle_body(view)})


@router.post(
    "/child-handles/{handle_id}:revoke",
    responses=ERROR_RESPONSES,
    summary="Withdraw a child handle, optionally asking the child to stop",
)
async def revoke_child_handle(
    handle_id: uuid.UUID,
    payload: ChildRunRevokeRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        handle = await commands.revoke_child_handle(
            db,
            ctx,
            handle_id=handle_id,
            reason=payload.reason,
            cancel_child=payload.cancel_child,
        )
        view = await queries.get_child_handle_view(db, ctx, handle)
        return 200, {"childHandle": queries.child_handle_body(view)}

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
