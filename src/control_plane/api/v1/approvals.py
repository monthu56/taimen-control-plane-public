"""Approval endpoints: human/agent-in-the-loop governance."""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ApprovalDecisionRequest,
    ApprovalOut,
    ApprovalOutcomeActionOut,
    ApprovalOutcomeOut,
    ApprovalRequestRequest,
    PageOut,
    dump,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import approval_outcomes as outcomes
from control_plane.application.commands import approvals as commands
from control_plane.application.queries import execution as queries

router = APIRouter(tags=["approvals"])


@router.post("/approvals", response_model=ApprovalOut, status_code=201, responses=ERROR_RESPONSES)
async def request_approval(
    payload: ApprovalRequestRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        approval = await commands.request_approval(
            db,
            ctx,
            task_ref=payload.task,
            artifact_id=payload.artifact_id,
            workspace_id=payload.workspace_id,
            required_role_id=payload.required_role_id,
            assigned_principal_id=payload.assigned_principal_id,
            comment=payload.comment,
            gate=payload.gate,
        )
        return 201, dump(ApprovalOut, approval)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/approvals", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_approvals(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: str | None = Query(default=None),
    task_id: uuid.UUID | None = Query(default=None, alias="taskId"),
) -> JSONResponse:
    page = await queries.list_approvals(
        db, ctx, limit=limit, cursor=cursor, status=status, task_id=task_id
    )
    return JSONResponse(page_body([dump(ApprovalOut, a) for a in page.items], page.next_cursor))


@router.get("/approvals/{approval_id}", response_model=ApprovalOut, responses=ERROR_RESPONSES)
async def get_approval(approval_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    approval = await queries.get_approval(db, ctx, approval_id)
    return JSONResponse(dump(ApprovalOut, approval))


async def _decision(
    approval_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: ApprovalDecisionRequest | None,
    *,
    approve: bool,
) -> JSONResponse:
    body = payload or ApprovalDecisionRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        approval = await commands.decide_approval(
            db, ctx, approval_id=approval_id, approve=approve, comment=body.comment
        )
        return 200, dump(ApprovalOut, approval)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"approve:{approve}\n" + body.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/approvals/{approval_id}:approve", response_model=ApprovalOut, responses=ERROR_RESPONSES
)
async def approve(
    approval_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: ApprovalDecisionRequest | None = None,
) -> JSONResponse:
    return await _decision(
        approval_id, request, ctx, settings, session_factory, payload, approve=True
    )


@router.post(
    "/approvals/{approval_id}:reject", response_model=ApprovalOut, responses=ERROR_RESPONSES
)
async def reject(
    approval_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: ApprovalDecisionRequest | None = None,
) -> JSONResponse:
    return await _decision(
        approval_id, request, ctx, settings, session_factory, payload, approve=False
    )


@router.post(
    "/approvals/{approval_id}:cancel", response_model=ApprovalOut, responses=ERROR_RESPONSES
)
async def cancel(
    approval_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: ApprovalDecisionRequest | None = None,
) -> JSONResponse:
    body = payload or ApprovalDecisionRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        approval = await commands.cancel_approval(
            db, ctx, approval_id=approval_id, comment=body.comment
        )
        return 200, dump(ApprovalOut, approval)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=body.model_dump_json(exclude_unset=True),
        executor=executor,
    )


def _outcome_body(view: outcomes.OutcomeView) -> dict[str, object]:
    return ApprovalOutcomeOut(
        approval_id=view.approval.id,
        outcome=view.outcome,
        outcome_status=view.approval.outcome_status,
        attempts=view.approval.outcome_attempts,
        last_error=view.approval.outcome_last_error,
        next_attempt_at=view.approval.outcome_next_attempt_at,
        actions=[ApprovalOutcomeActionOut(**action) for action in view.actions],
    ).model_dump(mode="json", by_alias=True)


@router.get(
    "/approvals/{approval_id}/outcome",
    response_model=ApprovalOutcomeOut,
    responses=ERROR_RESPONSES,
    summary="The declared outcome of a decision and what happened to each action",
)
async def get_approval_outcome(approval_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    approval = await queries.get_approval(db, ctx, approval_id)
    return JSONResponse(_outcome_body(await outcomes.outcome_view(db, approval)))


@router.post(
    "/approvals/{approval_id}:replay-outcome",
    response_model=ApprovalOutcomeOut,
    responses=ERROR_RESPONSES,
    summary="Resume a failed or stuck approval outcome at its first action that did not execute",
)
async def replay_outcome(
    approval_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        approval = await outcomes.replay_outcome(db, ctx, approval_id=approval_id)
        return 200, _outcome_body(await outcomes.outcome_view(db, approval))

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"replay-outcome:{approval_id}",
        executor=executor,
    )
