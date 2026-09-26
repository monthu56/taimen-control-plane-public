"""Claim endpoints: list/get, heartbeat, release, reclaim."""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ClaimHeartbeatRequest,
    ClaimOut,
    ClaimReclaimRequest,
    ClaimReleaseRequest,
    PageOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import claims as commands
from control_plane.application.queries import lists as queries

router = APIRouter(tags=["claims"])


@router.get("/claims", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_claims(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    task_id: uuid.UUID | None = Query(default=None, alias="taskId"),
    session_id: uuid.UUID | None = Query(default=None, alias="sessionId"),
    status: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_claims(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        task_id=task_id,
        session_id=session_id,
        status=status,
    )
    return JSONResponse(page_body([dump(ClaimOut, c) for c in page.items], page.next_cursor))


@router.get("/claims/{claim_id}", response_model=ClaimOut, responses=ERROR_RESPONSES)
async def get_claim(claim_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    claim = await queries.get_claim(db, ctx, claim_id)
    return JSONResponse(dump(ClaimOut, claim))


@router.post(
    "/claims/{claim_id}:heartbeat",
    response_model=ClaimOut,
    responses=ERROR_RESPONSES,
)
async def heartbeat_claim(
    claim_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: ClaimHeartbeatRequest | None = None,
) -> JSONResponse:
    ttl_seconds = payload.ttl_seconds if payload else None

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        claim = await commands.heartbeat_claim(
            db, ctx, settings, claim_id=claim_id, ttl_seconds=ttl_seconds
        )
        return 200, dump(ClaimOut, claim)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True) if payload else "",
        executor=executor,
    )


@router.post(
    "/claims/{claim_id}:release",
    response_model=ClaimOut,
    responses=ERROR_RESPONSES,
)
async def release_claim(
    claim_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: ClaimReleaseRequest | None = None,
) -> JSONResponse:
    reason = payload.reason if payload else "released"

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        claim = await commands.release_claim(db, ctx, claim_id=claim_id, reason=reason)
        return 200, dump(ClaimOut, claim)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True) if payload else "",
        executor=executor,
    )


@router.post(
    "/claims/{claim_id}:reclaim",
    response_model=ClaimOut,
    responses=ERROR_RESPONSES,
    summary="Take over an expired claim atomically (new fencing token)",
)
async def reclaim_claim(
    claim_id: uuid.UUID,
    payload: ClaimReclaimRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        claim = await commands.reclaim_claim(
            db,
            ctx,
            settings,
            claim_id=claim_id,
            session_id=payload.session_id,
            ttl_seconds=payload.ttl_seconds,
            intent=payload.intent,
        )
        return 200, dump(ClaimOut, claim)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
