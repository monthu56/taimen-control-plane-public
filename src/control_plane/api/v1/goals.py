"""Goal endpoints: desired states and the work that serves them (CP-ADR-0062).

Optimistic concurrency as for tasks: GET returns ``ETag: "goal-<version>"``
and PATCH requires ``If-Match``.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    GoalCreateRequest,
    GoalOut,
    GoalUpdateRequest,
    PageOut,
    dump,
    page_body,
    work_document,
)
from control_plane.api.v1.task_bodies import task_bodies
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import goals as commands
from control_plane.application.commands.goals import _UNSET
from control_plane.application.queries import goals as queries
from control_plane.domain.errors import ValidationError

router = APIRouter(tags=["goals"])

_GOAL_ENTITY = "goal"

# Null on these is never "clear it"; ownerId and parentGoalId accept null.
_NON_NULLABLE_PATCH_FIELDS = ("title", "desired_state", "criteria", "status")


@router.post(
    "/goals",
    response_model=GoalOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="State a desired state that work items can serve",
)
async def create_goal(
    payload: GoalCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        goal = await commands.create_goal(
            db,
            ctx,
            title=payload.title,
            desired_state=payload.desired_state,
            criteria=work_document(payload.criteria),
            owner_id=payload.owner_id,
            workspace_id=payload.workspace_id,
            parent_goal_id=payload.parent_goal_id,
            created_from=work_document(payload.created_from),
        )
        return 201, dump(GoalOut, goal)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/goals", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_goals(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: str | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    owner_id: uuid.UUID | None = Query(default=None, alias="ownerId"),
    parent_goal_id: uuid.UUID | None = Query(default=None, alias="parentGoalId"),
) -> JSONResponse:
    page = await queries.list_goals(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        status=status,
        workspace_id=workspace_id,
        owner_id=owner_id,
        parent_goal_id=parent_goal_id,
    )
    return JSONResponse(page_body([dump(GoalOut, g) for g in page.items], page.next_cursor))


@router.get("/goals/{goal_id}", response_model=GoalOut, responses=ERROR_RESPONSES)
async def get_goal(goal_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    goal = await queries.get_goal(db, ctx, goal_id)
    return JSONResponse(
        dump(GoalOut, goal), headers={"ETag": format_etag(_GOAL_ENTITY, goal.version)}
    )


@router.patch("/goals/{goal_id}", response_model=GoalOut, responses=ERROR_RESPONSES)
async def update_goal(
    goal_id: uuid.UUID,
    payload: GoalUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match, _GOAL_ENTITY)
    provided = payload.model_dump(exclude_unset=True)
    for field in _NON_NULLABLE_PATCH_FIELDS:
        if field in provided and provided[field] is None:
            raise ValidationError("invalid_field", f"{field} cannot be null")

    def pick(field: str) -> Any:
        return provided.get(field, _UNSET)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        goal = await commands.update_goal(
            db,
            ctx,
            goal_id=goal_id,
            expected_version=expected_version,
            title=pick("title"),
            desired_state=pick("desired_state"),
            criteria=work_document(payload.criteria) if "criteria" in provided else _UNSET,
            owner_id=pick("owner_id"),
            status=pick("status"),
            parent_goal_id=pick("parent_goal_id"),
        )
        return 200, dump(GoalOut, goal)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get(
    "/goals/{goal_id}/work",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="Work items that serve this goal, newest first",
)
async def list_goal_work(
    goal_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    include_subgoals: bool = Query(default=False, alias="includeSubgoals"),
    system_status_category: str | None = Query(default=None, alias="systemStatusCategory"),
) -> JSONResponse:
    page = await queries.list_goal_work(
        db,
        ctx,
        goal_id=goal_id,
        include_subgoals=include_subgoals,
        limit=limit,
        cursor=cursor,
        system_status_category=system_status_category,
    )
    return JSONResponse(page_body(await task_bodies(db, ctx, page.items), page.next_cursor))
