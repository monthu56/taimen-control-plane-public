"""Knowledge snapshots and domain packs, proxied to Memory (CP-ADR-0060).

Like ``/context``, every database transaction here closes BEFORE the Memory
Service is called, so a slow memory never holds a pooled connection. Unlike
``/context`` there is no degraded answer: the caller asked for a write into
memory, so a Memory failure is the caller's failure (502).
"""

import uuid
from typing import Any, cast

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import AuthDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ErrorEnvelope,
    KnowledgeSnapshotRequest,
    WorkspaceKnowledgePacksRequest,
)
from control_plane.application.commands import knowledge as commands
from control_plane.infrastructure.context_provider import KnowledgeProvider
from control_plane.infrastructure.db.engine import transaction

router = APIRouter(tags=["knowledge"])

_MEMORY_RESPONSES: dict[int | str, dict[str, Any]] = {
    **ERROR_RESPONSES,
    502: {"model": ErrorEnvelope, "description": "Memory service failed (memory_unavailable)"},
    503: {"model": ErrorEnvelope, "description": "Memory provider not configured"},
}


def _provider(request: Request) -> KnowledgeProvider:
    return commands.require_provider(
        cast(KnowledgeProvider | None, getattr(request.app.state, "context_provider", None))
    )


def _trace(request: Request) -> str | None:
    return getattr(request.state, "trace_run_id", "") or None


@router.post("/knowledge/snapshots", responses=_MEMORY_RESPONSES)
async def submit_snapshot(
    payload: KnowledgeSnapshotRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async with transaction(session_factory) as db:
        target = await commands.prepare_snapshot(
            db, ctx, settings, workspace_id=payload.workspace_id
        )
    provider = _provider(request)
    snapshot = payload.snapshot_document()
    answer = await commands.reconcile_snapshot(
        provider, target, snapshot, trace_run_id=_trace(request)
    )
    async with transaction(session_factory) as db:
        await commands.record_snapshot_reconciled(db, ctx, target, snapshot, answer)
    return JSONResponse(answer)


@router.post("/knowledge/packs", responses=_MEMORY_RESPONSES)
async def register_pack(
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: dict[str, Any] = Body(...),
) -> JSONResponse:
    commands.authorize_pack_registration(ctx, settings)
    commands.require_pack_identity(payload)
    answer = await commands.register_pack(_provider(request), payload, trace_run_id=_trace(request))
    async with transaction(session_factory) as db:
        await commands.record_pack_registered(db, ctx, payload, answer)
    return JSONResponse(answer)


@router.put("/workspaces/{workspace_id}/knowledge-packs", responses=_MEMORY_RESPONSES)
async def set_workspace_packs(
    workspace_id: uuid.UUID,
    payload: WorkspaceKnowledgePacksRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async with transaction(session_factory) as db:
        target = await commands.prepare_workspace_packs(
            db, ctx, settings, workspace_id=workspace_id
        )
    packs = commands.require_pinned_packs(payload.packs)
    answer = await commands.set_workspace_packs(
        _provider(request),
        target,
        packs=packs,
        strict=payload.strict,
        trace_run_id=_trace(request),
    )
    async with transaction(session_factory) as db:
        await commands.record_workspace_packs_set(
            db, ctx, target, packs=packs, strict=payload.strict
        )
    return JSONResponse(answer)
