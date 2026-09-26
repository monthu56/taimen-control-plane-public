"""The caller's attention list and feedback on it (CP-ADR-0071).

``GET /me/attention`` answers "what needs me now" from the core's approvals,
tasks and runs; ``POST /me/attention/{itemKey}:feedback`` records whether an
item was worth showing.
"""

import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import ERROR_RESPONSES, AttentionFeedbackRequest
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import attention as commands
from control_plane.application.queries import attention as queries

router = APIRouter(tags=["attention"])


@router.get(
    "/me/attention",
    responses=ERROR_RESPONSES,
    summary="What needs the calling principal now, highest score first",
)
async def get_attention(
    ctx: AuthDep,
    db: DbDep,
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    include_descendants: bool = Query(default=False, alias="includeDescendants"),
) -> JSONResponse:
    result = await queries.get_attention(
        db, ctx, workspace_id=workspace_id, include_descendants=include_descendants
    )
    return JSONResponse(queries.attention_body(result))


@router.post(
    "/me/attention/{item_key}:feedback",
    responses=ERROR_RESPONSES,
    summary="Record whether an item of the caller's attention list was worth showing",
)
async def record_feedback(
    item_key: str,
    payload: AttentionFeedbackRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await commands.record_feedback(
            db, ctx, item_key=item_key, verdict=payload.verdict, comment=payload.comment
        )
        return (201 if result.created else 200), queries.feedback_body(result.feedback)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
