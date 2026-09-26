"""Task response bodies with the derived owning project and task type.

``projectId`` is not a column (ADR-0035): it is resolved from the workspace
tree. ``typeKey`` / ``typeVersion`` are columns of another table (ADR-0048),
denormalized here so a reader does not need a second request to name a status.
``verification`` is the newest attempt of the verification stage (CP-ADR-0067).

All are resolved for the WHOLE page with one query each, never one query per
row.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.v1.schemas import TaskOut, dump
from control_plane.application.authorization import AuthContext
from control_plane.application.commands.verification import latest_attempts, summary
from control_plane.application.queries.projects import projects_for_workspaces
from control_plane.infrastructure.db.models import Task, TaskType


async def task_body(db: AsyncSession, ctx: AuthContext, task: Task) -> dict[str, Any]:
    bodies = await task_bodies(db, ctx, [task])
    return bodies[0]


async def task_bodies(
    db: AsyncSession, ctx: AuthContext, tasks: list[Task]
) -> list[dict[str, Any]]:
    workspace_ids = [t.workspace_id for t in tasks if t.workspace_id is not None]
    mapping = await projects_for_workspaces(db, ctx.tenant_id, workspace_ids)
    types = await _types_for(db, ctx, tasks)
    attempts = await latest_attempts(db, ctx.tenant_id, [task.id for task in tasks])
    return [
        dump(
            TaskOut,
            task,
            projectId=(
                str(mapping[task.workspace_id])
                if task.workspace_id is not None and task.workspace_id in mapping
                else None
            ),
            typeKey=types.get(task.type_id, ("", 0))[0],
            typeVersion=types.get(task.type_id, ("", 0))[1],
            verification=summary(attempts.get(task.id)),
        )
        for task in tasks
    ]


async def _types_for(
    db: AsyncSession, ctx: AuthContext, tasks: list[Task]
) -> dict[Any, tuple[str, int]]:
    type_ids = {task.type_id for task in tasks}
    if not type_ids:
        return {}
    rows = (
        await db.execute(
            select(TaskType.id, TaskType.key, TaskType.version).where(
                TaskType.tenant_id == ctx.tenant_id, TaskType.id.in_(type_ids)
            )
        )
    ).all()
    return {row[0]: (row[1], row[2]) for row in rows}
