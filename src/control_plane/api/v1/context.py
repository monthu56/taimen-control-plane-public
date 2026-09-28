"""Working-context endpoint: authoritative current state + durable memory.

The database transaction closes BEFORE the memory provider is called: a
slow or down provider degrades only the memory half and can never hold a
pooled connection while awaiting HTTP (memory outage must not throttle
coordination endpoints).

The knowledge graph (CP-ADR-0064) is read the same way: ``/context`` adds the
task context pack of a task whose type declares a context profile,
``/context/recall`` is the agent's pull (``cp_recall``) and
``/context-packs/{id}`` shows — and ``:replay`` reproduces — a recorded pack.
"""

import asyncio
import uuid
from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from control_plane.api.dependencies import AuthDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import ERROR_RESPONSES, ContextQueryRequest, RecallRequest
from control_plane.application.queries import recall
from control_plane.application.queries.context import fetch_memory, prepare_working_context
from control_plane.application.queries.task_context import (
    fetch_task_context,
    get_pack_record,
    public_record,
)
from control_plane.infrastructure.context_provider import ContextProvider, GraphProvider
from control_plane.infrastructure.db.engine import transaction

router = APIRouter(tags=["context"])


@router.post("/context", responses=ERROR_RESPONSES)
async def working_context(
    payload: ContextQueryRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    provider = cast(ContextProvider | None, getattr(request.app.state, "context_provider", None))
    async with transaction(session_factory) as db:
        body, memory_call = await prepare_working_context(
            db,
            ctx,
            settings,
            provider,
            query=payload.query,
            entity_anchors=payload.anchors,
            task_ref=payload.task,
            run_id=payload.run_id,
            workspace_id=payload.workspace_id,
            project_id=payload.project_id,
            include_subprojects=payload.include_subprojects,
            max_tokens=payload.max_tokens,
            include_memory=payload.include_memory,
            strategy=payload.strategy,
            as_of=payload.as_of,
        )
    if memory_call is not None and provider is not None:
        trace_run_id = getattr(request.state, "trace_run_id", "")
        task_call = memory_call.pop("taskContext", None)
        recall_half = fetch_memory(body, memory_call, provider, settings, trace_run_id=trace_run_id)
        if task_call is None:
            await recall_half
        else:
            # Both halves read Memory; neither waits for the other.
            _, body["taskContext"] = await asyncio.gather(
                recall_half,
                fetch_task_context(
                    task_call,
                    cast(GraphProvider, provider),
                    settings,
                    session_factory,
                    ctx,
                    trace_run_id=trace_run_id,
                ),
            )
    return JSONResponse(body)


@router.post("/context/recall", responses=ERROR_RESPONSES)
async def recall_graph(
    payload: RecallRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """Typed recall from the knowledge graph with the caller's visibility."""
    provider = recall.require_graph(getattr(request.app.state, "context_provider", None))
    async with transaction(session_factory) as db:
        call = await recall.prepare_recall(
            db,
            ctx,
            settings,
            anchor=payload.anchor,
            kind=payload.kind,
            query=payload.query,
            kinds=payload.kinds,
            relations=payload.relations,
            direction=payload.direction,
            depth=payload.depth,
            limit=payload.limit,
            as_of=payload.as_of,
            task_ref=payload.task,
            workspace_id=payload.workspace_id,
            budget_tokens=payload.budget_tokens,
            where=[c.to_memory() for c in payload.where],
        )
    body = await recall.fetch_recall(
        call, provider, settings, trace_run_id=getattr(request.state, "trace_run_id", "")
    )
    return JSONResponse(body)


@router.get("/context-packs/{pack_id}", responses=ERROR_RESPONSES)
async def get_context_pack(
    pack_id: uuid.UUID, ctx: AuthDep, session_factory: SessionFactoryDep
) -> JSONResponse:
    """A recorded task context pack: the request, the moment and what it used."""
    async with transaction(session_factory) as db:
        record = await get_pack_record(db, ctx, pack_id)
    return JSONResponse(public_record(record))


@router.post("/context-packs/{pack_id}:replay", responses=ERROR_RESPONSES)
async def replay_context_pack(
    pack_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    """Send a recorded pack's request again; ``reproduced`` and ``drift`` compare."""
    provider = recall.require_graph(getattr(request.app.state, "context_provider", None))
    async with transaction(session_factory) as db:
        record, scope = await recall.prepare_replay(db, ctx, settings, pack_id)
    body = await recall.fetch_replay(
        record, scope, provider, settings, trace_run_id=getattr(request.state, "trace_run_id", "")
    )
    return JSONResponse(body)
