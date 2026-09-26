"""Read-side queries for the organization model."""

import uuid

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.workspaces import (
    get_tenant_workspace,
    workspace_ancestor_ids,
)
from control_plane.application.queries.lists import Page, _paginate, clamp_limit
from control_plane.domain.enums import Permission, WorkspaceStatus
from control_plane.domain.errors import AuthorizationError, NotFoundError, ValidationError
from control_plane.infrastructure.db.models import (
    Capability,
    Principal,
    PrincipalCapability,
    PrincipalRole,
    PrincipalSkill,
    Role,
    Skill,
    Workspace,
    WorkspaceMember,
)


async def list_workspaces(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    parent_id: uuid.UUID | None = None,
    roots_only: bool = False,
    status: str | None = None,
) -> Page[Workspace]:
    await authorize(ctx, Permission.WORKSPACES_READ)
    if status is not None and status not in set(WorkspaceStatus):
        raise ValidationError("invalid_status", f"Unknown workspace status: {status}")
    stmt = select(Workspace).where(Workspace.tenant_id == ctx.tenant_id)
    if parent_id is not None:
        stmt = stmt.where(Workspace.parent_id == parent_id)
    elif roots_only:
        stmt = stmt.where(Workspace.parent_id.is_(None))
    if status is not None:
        stmt = stmt.where(Workspace.status == status)
    return await _paginate(
        session,
        stmt,
        created_col=Workspace.created_at,
        id_col=Workspace.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_workspace(
    session: AsyncSession, ctx: AuthContext, workspace_id: uuid.UUID
) -> Workspace:
    await authorize(ctx, Permission.WORKSPACES_READ)
    workspace = await session.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.tenant_id == ctx.tenant_id)
    )
    if workspace is None:
        raise NotFoundError("Workspace not found", details={"workspaceId": str(workspace_id)})
    return workspace


async def list_workspace_members(
    session: AsyncSession,
    ctx: AuthContext,
    workspace_id: uuid.UUID,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[WorkspaceMember]:
    await authorize(ctx, Permission.WORKSPACES_READ)
    await get_workspace(session, ctx, workspace_id)
    stmt = select(WorkspaceMember).where(
        WorkspaceMember.tenant_id == ctx.tenant_id,
        WorkspaceMember.workspace_id == workspace_id,
    )
    return await _paginate(
        session,
        stmt,
        created_col=WorkspaceMember.created_at,
        id_col=WorkspaceMember.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def list_roles(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    workspace_id: uuid.UUID | None = None,
) -> Page[Role]:
    await authorize(ctx, Permission.ORG_READ)
    stmt = select(Role).where(Role.tenant_id == ctx.tenant_id)
    if workspace_id is not None:
        stmt = stmt.where(Role.workspace_id == workspace_id)
    return await _paginate(
        session,
        stmt,
        created_col=Role.created_at,
        id_col=Role.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_role(session: AsyncSession, ctx: AuthContext, role_id: uuid.UUID) -> Role:
    await authorize(ctx, Permission.ORG_READ)
    role = await session.scalar(
        select(Role).where(Role.id == role_id, Role.tenant_id == ctx.tenant_id)
    )
    if role is None:
        raise NotFoundError("Role not found", details={"roleId": str(role_id)})
    return role


async def role_assignment_scope(
    session: AsyncSession, tenant_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> ColumnElement[bool]:
    """Role assignments that count in ``workspace_id``: tenant-wide ones and
    those scoped to the workspace or one of its ancestors. Without a workspace
    only tenant-wide assignments count — the rule of approval eligibility."""
    scope: ColumnElement[bool] = PrincipalRole.workspace_id.is_(None)
    if workspace_id is not None:
        ancestors = await workspace_ancestor_ids(session, tenant_id, workspace_id)
        if ancestors:
            scope = scope | PrincipalRole.workspace_id.in_(ancestors)
    return scope


async def list_role_holders(
    session: AsyncSession,
    ctx: AuthContext,
    role_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[Principal]:
    """Principals who hold the role in ``workspace_id`` (CP-ADR-0068).

    Exactly the principals eligible to decide an approval that requires this
    role in that workspace: an addressee list for whoever tells them about it.
    """
    # Tenant-level actions (authz/catalog.yaml): who holds a role is org data.
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)
    role = await session.scalar(
        select(Role).where(Role.id == role_id, Role.tenant_id == ctx.tenant_id)
    )
    if role is None:
        raise NotFoundError("Role not found", details={"roleId": str(role_id)})
    if workspace_id is not None:
        await get_tenant_workspace(session, ctx, workspace_id)
    scope = await role_assignment_scope(session, ctx.tenant_id, workspace_id)
    holds = exists().where(
        PrincipalRole.principal_id == Principal.id,
        PrincipalRole.tenant_id == ctx.tenant_id,
        PrincipalRole.role_id == role_id,
        scope,
    )
    stmt = select(Principal).where(Principal.tenant_id == ctx.tenant_id, holds)
    return await _paginate(
        session,
        stmt,
        created_col=Principal.created_at,
        id_col=Principal.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def list_capabilities(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
) -> Page[Capability]:
    await authorize(ctx, Permission.ORG_READ)
    stmt = select(Capability).where(Capability.tenant_id == ctx.tenant_id)
    return await _paginate(
        session,
        stmt,
        created_col=Capability.created_at,
        id_col=Capability.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_capability(
    session: AsyncSession, ctx: AuthContext, capability_id: uuid.UUID
) -> Capability:
    await authorize(ctx, Permission.ORG_READ)
    capability = await session.scalar(
        select(Capability).where(
            Capability.id == capability_id, Capability.tenant_id == ctx.tenant_id
        )
    )
    if capability is None:
        raise NotFoundError("Capability not found", details={"capabilityId": str(capability_id)})
    return capability


async def list_skills(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    limit: int | None = None,
    cursor: str | None = None,
    name: str | None = None,
    status: str | None = None,
) -> Page[Skill]:
    await authorize(ctx, Permission.ORG_READ)
    stmt = select(Skill).where(Skill.tenant_id == ctx.tenant_id)
    if name is not None:
        stmt = stmt.where(Skill.name == name)
    if status is not None:
        stmt = stmt.where(Skill.status == status)
    return await _paginate(
        session,
        stmt,
        created_col=Skill.created_at,
        id_col=Skill.id,
        limit=clamp_limit(limit),
        cursor=cursor,
    )


async def get_skill_by_ref(session: AsyncSession, ctx: AuthContext, ref: str) -> tuple[Skill, bool]:
    """Read one version by id or reference; readable by whoever may call or
    execute skills too — a caller cannot honour a contract it cannot see.

    The second value says whether the caller may also see the legacy
    ``config`` (endpoints, headers of a catalog entry): that stays behind
    ``org.read``, as it was before skills became invocable.
    """
    from control_plane.application.commands.skill_invocations import resolve_skill_ref

    await authorize(ctx, Permission.ORG_READ, Permission.SKILLS_INVOKE, Permission.SKILLS_EXECUTE)
    skill = await resolve_skill_ref(session, ctx, ref)
    try:
        await authorize(ctx, Permission.ORG_READ)
    except AuthorizationError:
        return skill, False
    return skill, True


async def get_skill(session: AsyncSession, ctx: AuthContext, skill_id: uuid.UUID) -> Skill:
    await authorize(ctx, Permission.ORG_READ)
    skill = await session.scalar(
        select(Skill).where(Skill.id == skill_id, Skill.tenant_id == ctx.tenant_id)
    )
    if skill is None:
        raise NotFoundError("Skill not found", details={"skillId": str(skill_id)})
    return skill


async def list_principal_roles(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> list[tuple[PrincipalRole, Role]]:
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)
    rows = await session.execute(
        select(PrincipalRole, Role)
        .join(Role, Role.id == PrincipalRole.role_id)
        .where(
            PrincipalRole.tenant_id == ctx.tenant_id,
            PrincipalRole.principal_id == principal_id,
        )
        .order_by(PrincipalRole.created_at)
    )
    return [(assignment, role) for assignment, role in rows.all()]


async def list_principal_capabilities(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> list[tuple[PrincipalCapability, Capability]]:
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)
    rows = await session.execute(
        select(PrincipalCapability, Capability)
        .join(Capability, Capability.id == PrincipalCapability.capability_id)
        .where(
            PrincipalCapability.tenant_id == ctx.tenant_id,
            PrincipalCapability.principal_id == principal_id,
        )
        .order_by(PrincipalCapability.created_at)
    )
    return [(assignment, capability) for assignment, capability in rows.all()]


async def list_principal_skills(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> list[tuple[PrincipalSkill, Skill]]:
    await authorize(ctx, Permission.ORG_READ, Permission.PRINCIPALS_READ)
    rows = await session.execute(
        select(PrincipalSkill, Skill)
        .join(Skill, Skill.id == PrincipalSkill.skill_id)
        .where(
            PrincipalSkill.tenant_id == ctx.tenant_id,
            PrincipalSkill.principal_id == principal_id,
        )
        .order_by(PrincipalSkill.created_at)
    )
    return [(assignment, skill) for assignment, skill in rows.all()]
