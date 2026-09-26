"""Project template versions: create, deprecate, resolve.

A template version is immutable from the moment it is written (ADR-0030): the
only mutation the database trigger permits is ``active -> deprecated``. Editing
a template therefore always means "create the next version", and a project keeps
pointing at the exact version it was created against.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import Permission, ProjectTemplateStatus
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.domain.project import (
    MAX_VIEWS,
    Lifecycle,
    parse_lifecycle,
    validate_config_document,
    validate_governance,
    validate_json_schema_document,
)
from control_plane.infrastructure.db.models import ProjectTemplate


async def _lock_template_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize version allocation for one (tenant, key)."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:tpl:{tenant_id}:{key}", 0)))
    )


async def get_tenant_template(
    session: AsyncSession, ctx: AuthContext, template_id: uuid.UUID
) -> ProjectTemplate:
    template = await session.scalar(
        select(ProjectTemplate).where(
            ProjectTemplate.id == template_id, ProjectTemplate.tenant_id == ctx.tenant_id
        )
    )
    if template is None:
        raise NotFoundError("Project template not found", details={"templateId": str(template_id)})
    return template


async def resolve_template(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    template_id: uuid.UUID | None,
    template_key: str | None,
    template_version: int | None,
) -> ProjectTemplate:
    """By id, or by key (+optional version; newest active version by default)."""
    if template_id is not None:
        return await get_tenant_template(session, ctx, template_id)
    if template_key is None:
        raise ValidationError("invalid_template_reference", "Provide templateId or templateKey")
    stmt = select(ProjectTemplate).where(
        ProjectTemplate.tenant_id == ctx.tenant_id, ProjectTemplate.key == template_key
    )
    if template_version is not None:
        stmt = stmt.where(ProjectTemplate.version == template_version)
    else:
        stmt = stmt.where(ProjectTemplate.status == ProjectTemplateStatus.ACTIVE)
    template = await session.scalar(stmt.order_by(ProjectTemplate.version.desc()).limit(1))
    if template is None:
        raise NotFoundError(
            "Project template not found",
            details={"templateKey": template_key, "templateVersion": template_version},
        )
    return template


def template_lifecycle(template: ProjectTemplate) -> Lifecycle:
    return parse_lifecycle(template.lifecycle_schema)


async def create_template_version(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    display_name: str,
    description: str = "",
    field_schema: dict[str, Any] | None = None,
    lifecycle_schema: dict[str, Any] | None = None,
    default_config: dict[str, Any] | None = None,
    default_views: list[Any] | None = None,
    governance_schema: dict[str, Any] | None = None,
    memory_defaults: dict[str, Any] | None = None,
) -> ProjectTemplate:
    """Create the next version of ``key`` — never an in-place edit."""
    await authorize(ctx, Permission.PROJECT_TEMPLATES_MANAGE)
    await _lock_template_key(session, ctx.tenant_id, key)

    schema = field_schema or {}
    validate_json_schema_document(schema, field_name="fieldSchema")
    lifecycle = parse_lifecycle(lifecycle_schema or _DEFAULT_LIFECYCLE)
    config = validate_config_document(default_config or {}, field_name="defaultConfig")
    views = list(default_views or [])
    if len(views) > MAX_VIEWS:
        raise ValidationError(
            "invalid_template",
            f"at most {MAX_VIEWS} default views are allowed",
            details={"field": "defaultViews", "maxViews": MAX_VIEWS},
        )
    # governance_schema is the declared ceiling shipped with the template; it
    # is type-checked with the same lattice as any other governance record.
    governance = validate_governance(governance_schema or {})
    memory = memory_defaults or {}
    validate_config_document({"memory": memory}, field_name="memoryDefaults")
    if governance:
        config["governance"] = validate_governance(
            {**governance, **(config.get("governance") or {})}
        )

    current_max = await session.scalar(
        select(func.max(ProjectTemplate.version)).where(
            ProjectTemplate.tenant_id == ctx.tenant_id, ProjectTemplate.key == key
        )
    )
    version = int(current_max or 0) + 1

    now = utcnow()
    template = ProjectTemplate(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        key=key,
        version=version,
        display_name=display_name,
        description=description,
        field_schema=schema,
        lifecycle_schema=lifecycle_schema or _DEFAULT_LIFECYCLE,
        default_config=config,
        default_views=views,
        governance_schema=governance,
        memory_defaults=memory,
        status=ProjectTemplateStatus.ACTIVE,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    session.add(template)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="project_template.created",
        entity_type="project_template",
        entity_id=template.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": key,
            "version": version,
            "displayName": display_name,
            "initialStatus": lifecycle.initial_status,
        },
    )
    return template


async def deprecate_template(
    session: AsyncSession, ctx: AuthContext, *, template_id: uuid.UUID
) -> ProjectTemplate:
    await authorize(ctx, Permission.PROJECT_TEMPLATES_MANAGE)
    template = await get_tenant_template(session, ctx, template_id)
    if template.status == ProjectTemplateStatus.DEPRECATED:
        return template  # idempotent

    template.status = ProjectTemplateStatus.DEPRECATED
    template.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="project_template.deprecated",
        entity_type="project_template",
        entity_id=template.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"key": template.key, "version": template.version},
    )
    return template


# A template with no lifecycle of its own still needs one, and the default has
# to be product-neutral: the five system categories under generic names.
_DEFAULT_LIFECYCLE: dict[str, Any] = {
    "initialStatus": "planned",
    "statuses": [
        {"key": "planned", "displayName": "Planned", "category": "planned"},
        {"key": "active", "displayName": "Active", "category": "active"},
        {"key": "paused", "displayName": "Paused", "category": "paused"},
        {"key": "completed", "displayName": "Completed", "category": "terminal_success"},
        {"key": "cancelled", "displayName": "Cancelled", "category": "terminal_cancelled"},
    ],
    "transitions": [
        {"from": "planned", "to": ["active", "cancelled"]},
        {"from": "active", "to": ["paused", "completed", "cancelled"]},
        {"from": "paused", "to": ["active", "cancelled"]},
    ],
}
