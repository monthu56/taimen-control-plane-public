"""Read-side queries: entity lists with stable cursor pagination.

Ordering is newest-first by ``(created_at, id)`` (tuple comparison in SQL),
which is stable under concurrent inserts; the cursor is opaque to clients.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Select, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    authorize,
    visible_objects,
)
from control_plane.application.common import (
    make_created_cursor,
    make_date_cursor,
    parse_created_cursor,
    parse_date_cursor,
)
from control_plane.domain.enums import (
    ClaimStatus,
    Permission,
    SessionStatus,
    TaskPriority,
)
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.domain.work_item import WORK_ITEM_CATEGORIES, normalize_planned_date
from control_plane.infrastructure.db.models import (
    Delegation,
    IamPrincipalBinding,
    Principal,
    Session,
    Task,
    TaskClaim,
    TaskType,
)

# Same bound as a lifecycle status key; a longer value cannot name one.
_STATUS_KEY_MAX = 64

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if limit < 1 or limit > MAX_LIMIT:
        raise ValidationError("invalid_limit", f"limit must be between 1 and {MAX_LIMIT}")
    return limit


async def _paginate[T](
    session: AsyncSession,
    stmt: Select[tuple[T]],
    *,
    created_col: InstrumentedAttribute[datetime],
    id_col: InstrumentedAttribute[uuid.UUID],
    limit: int,
    cursor: str | None,
) -> Page[T]:
    if cursor is not None:
        created_at, entity_id = parse_created_cursor(cursor)
        stmt = stmt.where(tuple_(created_col, id_col) < (created_at, entity_id))
    stmt = stmt.order_by(created_col.desc(), id_col.desc()).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = make_created_cursor(getattr(last, created_col.key), getattr(last, id_col.key))
    return Page(items=rows, next_cursor=next_cursor)


async def _paginate_by_date[T](
    session: AsyncSession,
    stmt: Select[tuple[T]],
    *,
    date_col: InstrumentedAttribute[datetime | None],
    id_col: InstrumentedAttribute[uuid.UUID],
    limit: int,
    cursor: str | None,
) -> Page[T]:
    """Soonest first, NULLs last, tie-broken by id (ADR-0049).

    Two properties make this safe to paginate. The order is TOTAL: equal dates
    — the common case for a whole team's end-of-sprint due date — are still
    separated by id, so no row can be skipped or repeated between pages. And
    the NULL tail is ordered too: a task without the date is not dropped from a
    date-ordered page, it comes after every task that has one. Excluding those
    is a filter's job, not the ordering's.
    """
    if cursor is not None:
        value, entity_id = parse_date_cursor(cursor)
        if value is None:
            # Already inside the NULL tail: only later ids remain.
            stmt = stmt.where(date_col.is_(None), id_col > entity_id)
        else:
            stmt = stmt.where(
                or_(tuple_(date_col, id_col) > (value, entity_id), date_col.is_(None))
            )
    stmt = stmt.order_by(date_col.asc().nulls_last(), id_col.asc()).limit(limit + 1)
    rows = list((await session.scalars(stmt)).all())
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = make_date_cursor(getattr(last, date_col.key), getattr(last, id_col.key))
    return Page(items=rows, next_cursor=next_cursor)


async def list_principals(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    kind: str | None = None,
) -> Page[Principal]:
    await authorize(ctx, Permission.PRINCIPALS_READ)
    stmt = select(Principal).where(Principal.tenant_id == ctx.tenant_id)
    if kind is not None:
        stmt = stmt.where(Principal.kind == kind)
    return await _paginate(
        session,
        stmt,
        created_col=Principal.created_at,
        id_col=Principal.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_principal(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> Principal:
    await authorize(ctx, Permission.PRINCIPALS_READ)
    principal = await session.scalar(
        select(Principal).where(Principal.id == principal_id, Principal.tenant_id == ctx.tenant_id)
    )
    if principal is None:
        raise NotFoundError("Principal not found", details={"principalId": str(principal_id)})
    return principal


async def list_iam_bindings(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> list[IamPrincipalBinding]:
    """Every federated identity bound to one principal, revoked ones included.

    Not paginated on purpose: a principal has a handful of identities at most,
    and an operator reads the list to see what closes when the principal goes.
    """
    principal = await get_principal(session, ctx, principal_id)
    rows = await session.scalars(
        select(IamPrincipalBinding)
        .where(
            IamPrincipalBinding.tenant_id == ctx.tenant_id,
            IamPrincipalBinding.principal_id == principal.id,
        )
        .order_by(IamPrincipalBinding.created_at.desc(), IamPrincipalBinding.id.desc())
    )
    return list(rows)


async def list_delegations(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[Delegation]:
    await authorize(ctx, Permission.DELEGATIONS_MANAGE)
    stmt = select(Delegation).where(Delegation.tenant_id == ctx.tenant_id)
    return await _paginate(
        session,
        stmt,
        created_col=Delegation.created_at,
        id_col=Delegation.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def list_sessions(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    status: str | None = None,
) -> Page[Session]:
    await authorize(ctx, Permission.SESSIONS_MANAGE)
    if status is not None and status not in set(SessionStatus):
        raise ValidationError("invalid_status", f"Unknown session status: {status}")
    stmt = select(Session).where(Session.tenant_id == ctx.tenant_id)
    if status is not None:
        stmt = stmt.where(Session.status == status)
    return await _paginate(
        session,
        stmt,
        created_col=Session.started_at,
        id_col=Session.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_session(session: AsyncSession, ctx: AuthContext, session_id: uuid.UUID) -> Session:
    work_session = await session.scalar(
        select(Session).where(Session.id == session_id, Session.tenant_id == ctx.tenant_id)
    )
    if work_session is None:
        raise NotFoundError("Session not found", details={"sessionId": str(session_id)})
    if work_session.principal_id != ctx.principal_id:
        await authorize(ctx, Permission.SESSIONS_MANAGE)
    return work_session


class TaskSort(StrEnum):
    """Orderings a task list may be read in (ADR-0049)."""

    CREATED_AT = "createdAt"
    START_DATE = "startDate"
    DUE_DATE = "dueDate"


TASK_SORTS = frozenset(s.value for s in TaskSort)


async def list_tasks(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    status: str | None = None,
    system_status_category: str | None = None,
    type_key: str | None = None,
    priority: str | None = None,
    owner_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    include_descendants: bool = False,
    project_id: uuid.UUID | None = None,
    include_subprojects: bool = False,
    start_from: datetime | None = None,
    start_to: datetime | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    sort: str | None = None,
    goal_id: uuid.UUID | None = None,
    goal_ids: list[uuid.UUID] | None = None,
) -> Page[Task]:
    await authorize(ctx, Permission.TASKS_READ)
    # Status keys are a tenant's own vocabulary since v0.8, so there is no
    # global set to validate against: an unknown key is an empty page, not a
    # 422. Categories ARE global, so those still get checked.
    if status is not None and len(status) > _STATUS_KEY_MAX:
        raise ValidationError("invalid_status", f"Unknown task status: {status}")
    if system_status_category is not None and system_status_category not in WORK_ITEM_CATEGORIES:
        raise ValidationError(
            "invalid_status_category",
            f"Unknown system status category: {system_status_category}",
            details={"known": sorted(WORK_ITEM_CATEGORIES)},
        )
    if priority is not None and priority not in set(TaskPriority):
        raise ValidationError("invalid_priority", f"Unknown priority: {priority}")
    if sort is not None and sort not in TASK_SORTS:
        raise ValidationError(
            "invalid_sort", f"Unknown sort: {sort}", details={"known": sorted(TASK_SORTS)}
        )
    stmt = select(Task).where(Task.tenant_id == ctx.tenant_id)
    # policy mode: only tasks in workspaces the principal may read, plus the
    # ones it owns or is assigned to (mirrors the task rule of the PDP model).
    workspaces = await visible_objects(ctx, Permission.TASKS_READ, "workspace")
    if workspaces is not None:
        stmt = stmt.where(
            or_(
                Task.workspace_id.in_([uuid.UUID(w) for w in workspaces]),
                Task.owner_id == ctx.principal_id,
                Task.assignee_id == ctx.principal_id,
                Task.created_by == ctx.principal_id,
            )
        )
    if status is not None:
        stmt = stmt.where(Task.status == status)
    if system_status_category is not None:
        stmt = stmt.where(Task.system_status_category == system_status_category)
    if type_key is not None:
        stmt = stmt.where(
            Task.type_id.in_(
                select(TaskType.id).where(
                    TaskType.tenant_id == ctx.tenant_id, TaskType.key == type_key
                )
            )
        )
    if priority is not None:
        stmt = stmt.where(Task.priority == priority)
    if owner_id is not None:
        stmt = stmt.where(Task.owner_id == owner_id)
    if assignee_id is not None:
        stmt = stmt.where(Task.assignee_id == assignee_id)
    # CP-ADR-0062: the goal a work item serves; a list is a goal subtree.
    if goal_id is not None:
        stmt = stmt.where(Task.goal_id == goal_id)
    if goal_ids is not None:
        stmt = stmt.where(Task.goal_id.in_(goal_ids))
    # Date bounds are inclusive and independent of the ordering; a bound on a
    # date implicitly excludes the rows that do not carry it, because NULL
    # satisfies no comparison.
    for column, bound, is_lower in (
        (Task.start_date, start_from, True),
        (Task.start_date, start_to, False),
        (Task.due_date, due_from, True),
        (Task.due_date, due_to, False),
    ):
        if bound is not None:
            moment = normalize_planned_date(bound)
            stmt = stmt.where(column >= moment if is_lower else column <= moment)
    if workspace_id is not None:
        if include_descendants:
            # v0.3 subtree query: the workspace and every descendant.
            from control_plane.application.commands.workspaces import workspace_subtree_ids

            subtree = await workspace_subtree_ids(session, ctx.tenant_id, workspace_id)
            if not subtree:
                raise NotFoundError(
                    "Workspace not found", details={"workspaceId": str(workspace_id)}
                )
            stmt = stmt.where(Task.workspace_id.in_(subtree))
        else:
            stmt = stmt.where(Task.workspace_id == workspace_id)
    if project_id is not None:
        # projectId is a filter, never a column: it expands to the project's
        # workspace scope and is applied BEFORE pagination, so pages can
        # neither skip nor duplicate rows (ADR-0035).
        from control_plane.application.queries.projects import (
            get_tenant_project,
            project_scope_workspace_ids,
        )

        project = await get_tenant_project(session, ctx, project_id)
        scope = await project_scope_workspace_ids(
            session, ctx.tenant_id, project, include_subprojects=include_subprojects
        )
        stmt = stmt.where(Task.workspace_id.in_(scope))

    # Every filter above is part of the statement BEFORE the cursor predicate
    # is applied, so paging can neither skip nor duplicate a row (ADR-0035).
    if sort in (TaskSort.START_DATE, TaskSort.DUE_DATE):
        return await _paginate_by_date(
            session,
            stmt,
            date_col=Task.start_date if sort == TaskSort.START_DATE else Task.due_date,
            id_col=Task.id,
            limit=clamp_limit(limit),
            cursor=cursor,
        )
    return await _paginate(
        session,
        stmt,
        created_col=Task.created_at,
        id_col=Task.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_task(session: AsyncSession, ctx: AuthContext, task_ref: str) -> Task:
    await authorize(ctx, Permission.TASKS_READ)
    conditions = [Task.tenant_id == ctx.tenant_id]
    try:
        conditions.append(Task.id == uuid.UUID(task_ref))
    except ValueError:
        conditions.append(Task.public_id == task_ref.upper())
    task = await session.scalar(select(Task).where(*conditions))
    if task is None:
        raise NotFoundError("Task not found", details={"task": task_ref})
    await authorize(ctx, Permission.TASKS_READ, resource=ResourceRef("task", str(task.id)))
    return task


async def list_claims(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    task_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    status: str | None = None,
) -> Page[TaskClaim]:
    await authorize(ctx, Permission.TASKS_READ)
    if status is not None and status not in set(ClaimStatus):
        raise ValidationError("invalid_status", f"Unknown claim status: {status}")
    stmt = select(TaskClaim).where(TaskClaim.tenant_id == ctx.tenant_id)
    if task_id is not None:
        stmt = stmt.where(TaskClaim.task_id == task_id)
    if session_id is not None:
        stmt = stmt.where(TaskClaim.session_id == session_id)
    if status is not None:
        stmt = stmt.where(TaskClaim.status == status)
    return await _paginate(
        session,
        stmt,
        created_col=TaskClaim.acquired_at,
        id_col=TaskClaim.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_claim(session: AsyncSession, ctx: AuthContext, claim_id: uuid.UUID) -> TaskClaim:
    await authorize(ctx, Permission.TASKS_READ)
    claim = await session.scalar(
        select(TaskClaim).where(TaskClaim.id == claim_id, TaskClaim.tenant_id == ctx.tenant_id)
    )
    if claim is None:
        raise NotFoundError("Claim not found", details={"claimId": str(claim_id)})
    return claim
