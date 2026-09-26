"""Goal read side: one goal, a page of goals, and the work that serves a goal.

Every query is scoped to the caller's tenant (CP-ADR-0062). ``goals.*`` are
decided on the goal's workspace (or the tenant, for a tenant-level goal), so in
policy mode a listing narrows to goals in workspaces the caller may read plus
tenant-level goals — exactly what ``get_goal`` would let it open one by one.
"""

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize, visible_objects
from control_plane.application.commands.goals import (
    MAX_GOAL_DEPTH,
    get_tenant_goal,
    goal_scope,
)
from control_plane.application.queries.lists import Page, _paginate, clamp_limit, list_tasks
from control_plane.domain.enums import Permission
from control_plane.domain.work_graph import validate_goal_status
from control_plane.infrastructure.db.models import Goal, Task


async def get_goal(session: AsyncSession, ctx: AuthContext, goal_id: uuid.UUID) -> Goal:
    await authorize(ctx, Permission.GOALS_READ)
    goal = await get_tenant_goal(session, ctx, goal_id)
    if goal.workspace_id is not None:
        await authorize(ctx, Permission.GOALS_READ, resource=goal_scope(goal.workspace_id))
    return goal


async def list_goals(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    status: str | None = None,
    workspace_id: uuid.UUID | None = None,
    owner_id: uuid.UUID | None = None,
    parent_goal_id: uuid.UUID | None = None,
) -> Page[Goal]:
    """Newest first; filters apply before the cursor, so pages stay stable."""
    await authorize(ctx, Permission.GOALS_READ)
    stmt = select(Goal).where(Goal.tenant_id == ctx.tenant_id)
    workspaces = await visible_objects(ctx, Permission.GOALS_READ, "workspace")
    if workspaces is not None:
        stmt = stmt.where(
            or_(
                Goal.workspace_id.in_([uuid.UUID(w) for w in workspaces]),
                Goal.workspace_id.is_(None),
            )
        )
    if status is not None:
        stmt = stmt.where(Goal.status == validate_goal_status(status))
    if workspace_id is not None:
        stmt = stmt.where(Goal.workspace_id == workspace_id)
    if owner_id is not None:
        stmt = stmt.where(Goal.owner_id == owner_id)
    if parent_goal_id is not None:
        stmt = stmt.where(Goal.parent_goal_id == parent_goal_id)
    return await _paginate(
        session,
        stmt,
        created_col=Goal.created_at,
        id_col=Goal.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def goal_subtree_ids(
    session: AsyncSession, ctx: AuthContext, goal_id: uuid.UUID
) -> list[uuid.UUID]:
    """The goal and every goal below it, breadth first, bounded in depth."""
    ids = [goal_id]
    frontier = [goal_id]
    for _ in range(MAX_GOAL_DEPTH):
        children = list(
            (
                await session.scalars(
                    select(Goal.id).where(
                        Goal.tenant_id == ctx.tenant_id, Goal.parent_goal_id.in_(frontier)
                    )
                )
            ).all()
        )
        frontier = [c for c in children if c not in ids]
        if not frontier:
            break
        ids.extend(frontier)
    return ids


async def list_goal_work(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    goal_id: uuid.UUID,
    include_subgoals: bool = False,
    limit: int | None = None,
    cursor: str | None = None,
    system_status_category: str | None = None,
) -> Page[Task]:
    """The work items that serve a goal (optionally its subgoals too).

    Two reads are being made, so both rights are needed: the goal is read
    under ``goals.read``, the tasks under ``tasks.read`` with the task
    listing's own visibility rules — a goal does not widen what tasks a
    caller may see.
    """
    goal = await get_goal(session, ctx, goal_id)
    goal_ids = await goal_subtree_ids(session, ctx, goal.id) if include_subgoals else [goal.id]
    return await list_tasks(
        session,
        ctx,
        limit=limit,
        cursor=cursor,
        system_status_category=system_status_category,
        goal_ids=goal_ids,
    )
