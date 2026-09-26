"""Organization registry commands: roles, capabilities, skills, assignments.

Role       = organizational function ("who are you in the org?")
Capability = declared ability ("what can you potentially do?")
Skill      = registered executable interface ("which tool can be used?")

None of these grant API authorization: API keys and their permissions remain
the only authorization mechanism. Roles/capabilities/skills feed *task
eligibility* (see tasks requirements) and organizational semantics.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.principals import get_tenant_principal
from control_plane.application.commands.workspaces import require_active_workspace
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import Permission, SkillProtocol, SkillStatus
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.domain.skill_contract import (
    normalize_contract,
    require_safe_retries,
    validate_policy_columns,
)
from control_plane.infrastructure.db.models import (
    Capability,
    PrincipalCapability,
    PrincipalRole,
    PrincipalSkill,
    Role,
    Skill,
)

# --- roles --------------------------------------------------------------------


async def get_tenant_role(session: AsyncSession, ctx: AuthContext, role_id: uuid.UUID) -> Role:
    role = await session.scalar(
        select(Role).where(Role.id == role_id, Role.tenant_id == ctx.tenant_id)
    )
    if role is None:
        raise NotFoundError("Role not found", details={"roleId": str(role_id)})
    return role


async def create_role(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    slug: str,
    name: str,
    description: str = "",
    workspace_id: uuid.UUID | None = None,
) -> Role:
    await authorize(ctx, Permission.ORG_MANAGE)
    if workspace_id is not None:
        await require_active_workspace(session, ctx, workspace_id)

    existing = await session.scalar(
        select(Role.id).where(
            Role.tenant_id == ctx.tenant_id,
            Role.slug == slug,
            Role.workspace_id.is_(None)
            if workspace_id is None
            else Role.workspace_id == workspace_id,
        )
    )
    if existing is not None:
        raise ConflictError(
            "role_slug_conflict",
            "A role with this slug already exists in this scope",
            details={"slug": slug},
        )

    now = utcnow()
    role = Role(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        slug=slug,
        name=name,
        description=description,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(role)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="role.created",
        entity_type="role",
        entity_id=role.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "slug": slug,
            "name": name,
            "workspaceId": str(workspace_id) if workspace_id else None,
        },
    )
    return role


async def update_role(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    role_id: uuid.UUID,
    expected_version: int,
    name: str | None = None,
    description: str | None = None,
) -> Role:
    await authorize(ctx, Permission.ORG_MANAGE)
    role = await session.scalar(
        select(Role).where(Role.id == role_id, Role.tenant_id == ctx.tenant_id).with_for_update()
    )
    if role is None:
        raise NotFoundError("Role not found", details={"roleId": str(role_id)})
    if role.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Role version does not match If-Match",
            details={"expectedVersion": expected_version, "currentVersion": role.version},
        )

    changes: dict[str, Any] = {}
    if name is not None and name != role.name:
        changes["name"] = name
    if description is not None and description != role.description:
        changes["description"] = description
    if not changes:
        raise ValidationError("empty_update", "No fields to update")
    for field_name, value in changes.items():
        setattr(role, field_name, value)
    role.version += 1
    role.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="role.updated",
        entity_type="role",
        entity_id=role.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"changes": changes, "version": role.version},
    )
    return role


async def assign_role(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    role_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
) -> PrincipalRole:
    await authorize(ctx, Permission.ORG_MANAGE)
    await get_tenant_principal(session, ctx, principal_id)
    await get_tenant_role(session, ctx, role_id)
    if workspace_id is not None:
        await require_active_workspace(session, ctx, workspace_id)

    existing = await session.scalar(
        select(PrincipalRole).where(
            PrincipalRole.principal_id == principal_id,
            PrincipalRole.role_id == role_id,
            PrincipalRole.workspace_id.is_(None)
            if workspace_id is None
            else PrincipalRole.workspace_id == workspace_id,
        )
    )
    if existing is not None:
        return existing  # idempotent

    assignment = PrincipalRole(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        principal_id=principal_id,
        role_id=role_id,
        workspace_id=workspace_id,
        created_at=utcnow(),
    )
    session.add(assignment)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="role.assigned",
        entity_type="principal",
        entity_id=principal_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "roleId": str(role_id),
            "workspaceId": str(workspace_id) if workspace_id else None,
        },
    )
    return assignment


async def revoke_role(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    role_id: uuid.UUID,
    workspace_id: uuid.UUID | None = None,
) -> None:
    await authorize(ctx, Permission.ORG_MANAGE)
    await get_tenant_principal(session, ctx, principal_id)
    assignment = await session.scalar(
        select(PrincipalRole)
        .where(
            PrincipalRole.tenant_id == ctx.tenant_id,
            PrincipalRole.principal_id == principal_id,
            PrincipalRole.role_id == role_id,
            PrincipalRole.workspace_id.is_(None)
            if workspace_id is None
            else PrincipalRole.workspace_id == workspace_id,
        )
        .with_for_update()
    )
    if assignment is None:
        return  # idempotent
    await session.delete(assignment)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="role.revoked",
        entity_type="principal",
        entity_id=principal_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "roleId": str(role_id),
            "workspaceId": str(workspace_id) if workspace_id else None,
        },
    )


# --- capabilities -------------------------------------------------------------


async def get_tenant_capability(
    session: AsyncSession, ctx: AuthContext, capability_id: uuid.UUID
) -> Capability:
    capability = await session.scalar(
        select(Capability).where(
            Capability.id == capability_id, Capability.tenant_id == ctx.tenant_id
        )
    )
    if capability is None:
        raise NotFoundError("Capability not found", details={"capabilityId": str(capability_id)})
    return capability


async def create_capability(
    session: AsyncSession, ctx: AuthContext, *, name: str, description: str = ""
) -> Capability:
    await authorize(ctx, Permission.ORG_MANAGE)
    existing = await session.scalar(
        select(Capability.id).where(Capability.tenant_id == ctx.tenant_id, Capability.name == name)
    )
    if existing is not None:
        raise ConflictError(
            "capability_exists",
            "A capability with this name already exists",
            details={"name": name},
        )

    capability = Capability(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        name=name,
        description=description,
        created_at=utcnow(),
    )
    session.add(capability)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="capability.created",
        entity_type="capability",
        entity_id=capability.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"name": name},
    )
    return capability


async def assign_capability(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    capability_id: uuid.UUID,
    metadata: dict[str, Any] | None = None,
) -> PrincipalCapability:
    await authorize(ctx, Permission.ORG_MANAGE)
    await get_tenant_principal(session, ctx, principal_id)
    await get_tenant_capability(session, ctx, capability_id)

    existing = await session.scalar(
        select(PrincipalCapability).where(
            PrincipalCapability.principal_id == principal_id,
            PrincipalCapability.capability_id == capability_id,
        )
    )
    if existing is not None:
        return existing  # idempotent

    assignment = PrincipalCapability(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        principal_id=principal_id,
        capability_id=capability_id,
        metadata_json=metadata or {},
        created_at=utcnow(),
    )
    session.add(assignment)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="capability.assigned",
        entity_type="principal",
        entity_id=principal_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"capabilityId": str(capability_id)},
    )
    return assignment


async def revoke_capability(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    capability_id: uuid.UUID,
) -> None:
    await authorize(ctx, Permission.ORG_MANAGE)
    await get_tenant_principal(session, ctx, principal_id)
    assignment = await session.scalar(
        select(PrincipalCapability)
        .where(
            PrincipalCapability.tenant_id == ctx.tenant_id,
            PrincipalCapability.principal_id == principal_id,
            PrincipalCapability.capability_id == capability_id,
        )
        .with_for_update()
    )
    if assignment is None:
        return  # idempotent
    await session.delete(assignment)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="capability.revoked",
        entity_type="principal",
        entity_id=principal_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"capabilityId": str(capability_id)},
    )


# --- skills -------------------------------------------------------------------


async def get_tenant_skill(session: AsyncSession, ctx: AuthContext, skill_id: uuid.UUID) -> Skill:
    skill = await session.scalar(
        select(Skill).where(Skill.id == skill_id, Skill.tenant_id == ctx.tenant_id)
    )
    if skill is None:
        raise NotFoundError("Skill not found", details={"skillId": str(skill_id)})
    return skill


async def register_skill(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    name: str,
    version: str = "1.0.0",
    description: str = "",
    protocol: str | None,
    config: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    side_effects: str | None = None,
    risk_level: str | None = None,
    contract: dict[str, Any] | None = None,
) -> Skill:
    """Publish a skill version.

    Without ``contract`` the row is a catalog entry (the pre-M2.1 shape): it
    can be assigned and required, but not invoked. With a contract v1 the
    version is validated here once and stored normalized (ADR-0056 §1); the
    contract's ``inputs``/``outputs`` become ``inputSchema``/``outputSchema``
    and its implementation protocol becomes ``protocol``, so discovery and the
    invocation path read the same thing.
    """
    await authorize(ctx, Permission.ORG_MANAGE)
    normalized: dict[str, Any] | None = None
    if contract is not None:
        normalized = normalize_contract(contract)
        side_effects, risk_level = validate_policy_columns(side_effects, risk_level)
        require_safe_retries(normalized, side_effects)
        contract_protocol = normalized["implementation"]["protocol"]
        if protocol is not None and protocol != contract_protocol:
            raise ValidationError(
                "invalid_skill_contract",
                "protocol must match contract.implementation.protocol",
                details={"protocol": protocol, "implementationProtocol": contract_protocol},
            )
        for field_name, value in (("inputSchema", input_schema), ("outputSchema", output_schema)):
            if value is not None:
                raise ValidationError(
                    "invalid_skill_contract",
                    f"{field_name} is taken from the contract; do not pass both",
                    details={"field": field_name},
                )
        protocol = contract_protocol
        input_schema = normalized["inputs"]
        output_schema = normalized["outputs"]
    elif side_effects is not None or risk_level is not None:
        side_effects, risk_level = validate_policy_columns(side_effects, risk_level)
    if protocol is None:
        raise ValidationError("invalid_protocol", "protocol is required without a contract")
    if protocol not in set(SkillProtocol):
        raise ValidationError("invalid_protocol", f"Unknown skill protocol: {protocol}")

    existing = await session.scalar(
        select(Skill.id).where(
            Skill.tenant_id == ctx.tenant_id,
            Skill.name == name,
            Skill.version == version,
        )
    )
    if existing is not None:
        raise ConflictError(
            "skill_exists",
            "A skill with this name and version already exists",
            details={"name": name, "version": version},
        )

    now = utcnow()
    skill = Skill(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        name=name,
        version=version,
        description=description,
        protocol=protocol,
        config=config or {},
        input_schema=input_schema,
        output_schema=output_schema,
        side_effects=side_effects,
        risk_level=risk_level,
        contract=normalized,
        status=SkillStatus.ACTIVE,
        row_version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(skill)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="skill.registered",
        entity_type="skill",
        entity_id=skill.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "name": name,
            "version": version,
            "protocol": protocol,
            "invocable": normalized is not None,
            "sideEffects": side_effects,
            "riskLevel": risk_level,
        },
    )
    return skill


# Forward-only status moves of a published version (mirrors the trigger).
_SKILL_STATUS_MOVES = {
    SkillStatus.ACTIVE: frozenset({SkillStatus.DEPRECATED, SkillStatus.DISABLED}),
    SkillStatus.DEPRECATED: frozenset({SkillStatus.DISABLED}),
    SkillStatus.DISABLED: frozenset(),
}


async def update_skill(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    skill_id: uuid.UUID,
    expected_row_version: int,
    description: str | None = None,
    config: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    status: str | None = None,
) -> Skill:
    """Edit what a published version still allows: description and status.

    Everything else is the contract and is frozen (ADR-0056 §1): a change is a
    new version. The trigger ``skills_immutable`` enforces the same rule at
    the database; this check exists to answer 409 instead of 500.
    """
    await authorize(ctx, Permission.ORG_MANAGE)
    skill = await session.scalar(
        select(Skill)
        .where(Skill.id == skill_id, Skill.tenant_id == ctx.tenant_id)
        .with_for_update()
    )
    if skill is None:
        raise NotFoundError("Skill not found", details={"skillId": str(skill_id)})
    if skill.row_version != expected_row_version:
        raise ConflictError(
            "version_conflict",
            "Skill version does not match If-Match",
            details={
                "expectedVersion": expected_row_version,
                "currentVersion": skill.row_version,
            },
        )
    if status is not None and status not in set(SkillStatus):
        raise ValidationError("invalid_status", f"Unknown skill status: {status}")

    frozen = sorted(
        field_name
        for field_name, value in (
            ("config", config),
            ("inputSchema", input_schema),
            ("outputSchema", output_schema),
        )
        if value is not None
        and getattr(
            skill,
            {"inputSchema": "input_schema", "outputSchema": "output_schema"}.get(
                field_name, field_name
            ),
        )
        != value
    )
    if frozen:
        raise ConflictError(
            "skill_version_immutable",
            "A published skill version cannot change; publish a new version",
            details={"skillId": str(skill.id), "fields": frozen},
        )
    if (
        status is not None
        and status != skill.status
        and status not in _SKILL_STATUS_MOVES[SkillStatus(skill.status)]
    ):
        raise ConflictError(
            "invalid_status_transition",
            "Skill status may only move active -> deprecated -> disabled",
            details={"from": skill.status, "to": status},
        )

    changes: dict[str, Any] = {}
    for field_name, value in (("description", description), ("status", status)):
        if value is not None and getattr(skill, field_name) != value:
            changes[field_name] = value
    if not changes:
        raise ValidationError("empty_update", "No fields to update")
    for field_name, value in changes.items():
        setattr(skill, field_name, value)
    skill.row_version += 1
    skill.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="skill.updated",
        entity_type="skill",
        entity_id=skill.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"changedFields": sorted(changes), "rowVersion": skill.row_version},
    )
    return skill


async def assign_skill(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    skill_id: uuid.UUID,
    metadata: dict[str, Any] | None = None,
) -> PrincipalSkill:
    await authorize(ctx, Permission.ORG_MANAGE)
    await get_tenant_principal(session, ctx, principal_id)
    skill = await get_tenant_skill(session, ctx, skill_id)
    if skill.status == SkillStatus.DISABLED:
        raise ValidationError(
            "skill_disabled", "Cannot assign a disabled skill", details={"skillId": str(skill_id)}
        )

    existing = await session.scalar(
        select(PrincipalSkill).where(
            PrincipalSkill.principal_id == principal_id,
            PrincipalSkill.skill_id == skill_id,
        )
    )
    if existing is not None:
        return existing  # idempotent

    assignment = PrincipalSkill(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        principal_id=principal_id,
        skill_id=skill_id,
        metadata_json=metadata or {},
        created_at=utcnow(),
    )
    session.add(assignment)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="skill.assigned",
        entity_type="principal",
        entity_id=principal_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"skillId": str(skill_id)},
    )
    return assignment


async def revoke_skill(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    principal_id: uuid.UUID,
    skill_id: uuid.UUID,
) -> None:
    await authorize(ctx, Permission.ORG_MANAGE)
    await get_tenant_principal(session, ctx, principal_id)
    assignment = await session.scalar(
        select(PrincipalSkill)
        .where(
            PrincipalSkill.tenant_id == ctx.tenant_id,
            PrincipalSkill.principal_id == principal_id,
            PrincipalSkill.skill_id == skill_id,
        )
        .with_for_update()
    )
    if assignment is None:
        return  # idempotent
    await session.delete(assignment)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="skill.revoked",
        entity_type="principal",
        entity_id=principal_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"skillId": str(skill_id)},
    )
