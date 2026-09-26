"""Project commands: profile CRUD, lifecycle transitions, config revisions.

A Project is a profile attached one-to-one to a Workspace (ADR-0031). Creation
is atomic with the workspace it lands on, concurrency is settled by the
``UNIQUE (workspace_id)`` index rather than by application checks, and every
mutation writes state + event + outbox in one transaction like the rest of the
codebase.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.external_references import register_external_reference
from control_plane.application.commands.project_templates import (
    resolve_template,
    template_lifecycle,
)
from control_plane.application.commands.workspaces import (
    create_workspace,
    get_tenant_workspace,
    lock_workspace_tree,
)
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.queries.projects import (
    ancestor_effective_governance,
    effective_config_for,
    get_tenant_project,
    parent_project_id,
)
from control_plane.domain.enums import (
    Permission,
    ProjectStatus,
    ProjectTemplateStatus,
    WorkspaceStatus,
)
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.domain.project import (
    Lifecycle,
    governance_violations,
    guard_json_document,
    locked_setting_violations,
    validate_against_schema,
    validate_config_document,
)
from control_plane.infrastructure.db.models import (
    ExternalReference,
    ProjectConfigRevision,
    ProjectProfile,
    ProjectTemplate,
    Workspace,
)


def _require_status_in_lifecycle(lifecycle: Lifecycle, status_key: str, *, field: str) -> str:
    if status_key not in lifecycle.categories:
        raise ValidationError(
            "status_not_in_lifecycle",
            f"Status {status_key!r} is not declared by the template lifecycle",
            details={
                "field": field,
                "statusKey": status_key,
                "known": sorted(lifecycle.categories),
            },
        )
    return lifecycle.category_of(status_key)


async def _assert_settings_allowed(
    session: AsyncSession,
    ctx: AuthContext,
    project: ProjectProfile,
    settings: dict[str, Any],
) -> None:
    """Ancestors may lock settings keys; a descendant cannot take them back."""
    if not settings:
        return
    effective = await effective_config_for(session, ctx.tenant_id, project)
    violations = locked_setting_violations(settings, effective.locked_settings)
    if violations:
        raise ValidationError(
            "setting_locked",
            "An ancestor project locked these settings",
            details={"lockedSettings": violations},
        )


async def create_project(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    workspace_id: uuid.UUID | None = None,
    workspace_slug: str | None = None,
    workspace_name: str | None = None,
    parent_workspace_id: uuid.UUID | None = None,
    workspace_type_key: str | None = None,
    template_id: uuid.UUID | None = None,
    template_key: str | None = None,
    template_version: int | None = None,
    status_key: str | None = None,
    owner_principal_id: uuid.UUID | None = None,
    start_date: datetime | None = None,
    target_date: datetime | None = None,
    custom_fields: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> ProjectProfile:
    """Attach a project to an existing workspace, or create both atomically."""
    await authorize(ctx, Permission.PROJECTS_MANAGE)
    await lock_workspace_tree(session, ctx.tenant_id)

    if workspace_id is None:
        if not workspace_slug:
            raise ValidationError(
                "invalid_project_request",
                "Provide workspaceId, or workspaceSlug to create the workspace",
            )
        workspace = await create_workspace(
            session,
            ctx,
            slug=workspace_slug,
            name=workspace_name or workspace_slug,
            parent_id=parent_workspace_id,
            type_key=workspace_type_key,
        )
    else:
        workspace = await get_tenant_workspace(session, ctx, workspace_id)
        if workspace.status != WorkspaceStatus.ACTIVE:
            raise ValidationError(
                "workspace_archived",
                "An archived workspace cannot receive a project profile",
                details={"workspaceId": str(workspace.id)},
            )

    template = await resolve_template(
        session,
        ctx,
        template_id=template_id,
        template_key=template_key,
        template_version=template_version,
    )
    if template.status != ProjectTemplateStatus.ACTIVE:
        raise ValidationError(
            "template_deprecated",
            "A deprecated template cannot be used for a new project",
            details={"templateId": str(template.id)},
        )

    lifecycle = template_lifecycle(template)
    initial_status = status_key or lifecycle.initial_status
    category = _require_status_in_lifecycle(lifecycle, initial_status, field="statusKey")

    fields = custom_fields or {}
    validate_against_schema(
        template.field_schema, fields, code="custom_fields_invalid", field_name="customFields"
    )
    profile_settings = settings or {}
    guard_json_document(profile_settings, label="settings")
    validate_config_document({"settings": profile_settings}, field_name="settings")

    if owner_principal_id is not None:
        await _assert_tenant_principal(session, ctx, owner_principal_id)

    now = utcnow()
    project = ProjectProfile(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace.id,
        template_id=template.id,
        status_key=initial_status,
        system_status_category=category,
        owner_principal_id=owner_principal_id,
        start_date=start_date,
        target_date=target_date,
        custom_fields=fields,
        settings=profile_settings,
        active_config_revision_id=None,
        status=ProjectStatus.ACTIVE,
        version=1,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    # Under the tenant tree lock this pre-check is race-free and gives the
    # caller a clean 409. The unique index below is the backstop, held inside
    # a SAVEPOINT so a violation rolls back the insert rather than poisoning
    # the whole transaction (which would turn a conflict into a 500).
    existing = await session.scalar(
        select(ProjectProfile.id).where(ProjectProfile.workspace_id == workspace.id)
    )
    if existing is not None:
        raise ConflictError(
            "project_exists",
            "This workspace already has a project profile",
            details={"workspaceId": str(workspace.id)},
        )
    try:
        async with session.begin_nested():
            session.add(project)
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "project_exists",
            "This workspace already has a project profile",
            details={"workspaceId": str(workspace.id)},
        ) from exc

    await _assert_settings_allowed(session, ctx, project, profile_settings)
    # A brand-new project has no revision yet, so its own governance is empty
    # and cannot weaken anything; the ceiling still gets recorded for audit.
    parent_id = await parent_project_id(session, ctx.tenant_id, project)

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="project.created",
        entity_type="project",
        entity_id=project.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "workspaceId": str(workspace.id),
            "parentProjectId": str(parent_id) if parent_id else None,
            "templateKey": template.key,
            "templateVersion": template.version,
            "statusKey": initial_status,
            "systemStatusCategory": category,
        },
    )
    return project


async def _assert_tenant_principal(
    session: AsyncSession, ctx: AuthContext, principal_id: uuid.UUID
) -> None:
    from control_plane.application.commands.principals import get_tenant_principal

    await get_tenant_principal(session, ctx, principal_id)


async def update_project(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    project_id: uuid.UUID,
    expected_version: int,
    owner_principal_id: uuid.UUID | None = None,
    clear_owner: bool = False,
    start_date: datetime | None = None,
    target_date: datetime | None = None,
    custom_fields: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    template_id: uuid.UUID | None = None,
    template_key: str | None = None,
    template_version: int | None = None,
) -> ProjectProfile:
    await authorize(ctx, Permission.PROJECTS_MANAGE)
    project = await get_tenant_project(session, ctx, project_id, for_update=True)
    _require_version(project, expected_version)
    if project.status == ProjectStatus.ARCHIVED:
        raise ValidationError(
            "project_archived",
            "An archived project cannot be updated",
            details={"projectId": str(project_id)},
        )

    template = await session.get(ProjectTemplate, project.template_id)
    assert template is not None
    changes: dict[str, Any] = {}

    if template_version is not None and template_id is None and template_key is None:
        raise ValidationError(
            "invalid_template_reference",
            "templateVersion needs templateKey (or templateId) to select a template",
            details={"field": "templateVersion"},
        )
    if template_id is not None or template_key is not None:
        new_template = await resolve_template(
            session,
            ctx,
            template_id=template_id,
            template_key=template_key,
            template_version=template_version,
        )
        if new_template.id != template.id:
            if new_template.status != ProjectTemplateStatus.ACTIVE:
                raise ValidationError(
                    "template_deprecated",
                    "A deprecated template cannot be selected",
                    details={"templateId": str(new_template.id)},
                )
            # Moving to another template version must not leave the project
            # describing itself with a status the new lifecycle never declared.
            lifecycle = template_lifecycle(new_template)
            project.system_status_category = _require_status_in_lifecycle(
                lifecycle, project.status_key, field="statusKey"
            )
            template = new_template
            project.template_id = new_template.id
            changes["templateKey"] = new_template.key
            changes["templateVersion"] = new_template.version

    if custom_fields is not None:
        validate_against_schema(
            template.field_schema,
            custom_fields,
            code="custom_fields_invalid",
            field_name="customFields",
        )
        project.custom_fields = custom_fields
        changes["customFields"] = True
    elif "templateKey" in changes:
        validate_against_schema(
            template.field_schema,
            project.custom_fields,
            code="custom_fields_invalid",
            field_name="customFields",
        )

    if settings is not None:
        guard_json_document(settings, label="settings")
        validate_config_document({"settings": settings}, field_name="settings")
        await _assert_settings_allowed(session, ctx, project, settings)
        project.settings = settings
        changes["settings"] = True

    if clear_owner:
        project.owner_principal_id = None
        changes["ownerPrincipalId"] = None
    elif owner_principal_id is not None:
        await _assert_tenant_principal(session, ctx, owner_principal_id)
        project.owner_principal_id = owner_principal_id
        changes["ownerPrincipalId"] = str(owner_principal_id)
    if start_date is not None:
        project.start_date = start_date
        changes["startDate"] = start_date.isoformat()
    if target_date is not None:
        project.target_date = target_date
        changes["targetDate"] = target_date.isoformat()

    if not changes:
        raise ValidationError("empty_update", "No fields to update")

    project.version += 1
    project.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="project.updated",
        entity_type="project",
        entity_id=project.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"changes": sorted(changes), "version": project.version},
    )
    return project


def _require_version(project: ProjectProfile, expected_version: int) -> None:
    if project.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Project version does not match If-Match",
            details={
                "projectId": str(project.id),
                "expectedVersion": expected_version,
                "currentVersion": project.version,
            },
        )


async def transition_project(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    project_id: uuid.UUID,
    expected_version: int,
    status_key: str,
    comment: str = "",
) -> ProjectProfile:
    await authorize(ctx, Permission.PROJECTS_MANAGE)
    project = await get_tenant_project(session, ctx, project_id, for_update=True)
    _require_version(project, expected_version)
    if project.status == ProjectStatus.ARCHIVED:
        raise ValidationError(
            "project_archived",
            "An archived project cannot change lifecycle status",
            details={"projectId": str(project_id)},
        )

    template = await session.get(ProjectTemplate, project.template_id)
    assert template is not None
    lifecycle = template_lifecycle(template)
    category = _require_status_in_lifecycle(lifecycle, status_key, field="statusKey")

    if status_key == project.status_key:
        raise ValidationError(
            "invalid_transition",
            "Project is already in this status",
            details={"statusKey": status_key},
        )
    if not lifecycle.allows(project.status_key, status_key):
        raise ValidationError(
            "invalid_transition",
            f"Transition {project.status_key!r} -> {status_key!r} is not declared",
            details={
                "from": project.status_key,
                "to": status_key,
                "allowed": sorted(lifecycle.transitions.get(project.status_key, frozenset())),
            },
        )

    previous_status = project.status_key
    previous_category = project.system_status_category
    project.status_key = status_key
    project.system_status_category = category
    project.version += 1
    project.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="project.status_changed",
        entity_type="project",
        entity_id=project.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "fromStatusKey": previous_status,
            "fromSystemStatusCategory": previous_category,
            "statusKey": status_key,
            "systemStatusCategory": category,
            "comment": comment,
            "version": project.version,
        },
    )
    return project


async def archive_project(
    session: AsyncSession, ctx: AuthContext, *, project_id: uuid.UUID
) -> ProjectProfile:
    """Retire the project record only: workspace, tasks and history stay put."""
    await authorize(ctx, Permission.PROJECTS_MANAGE)
    project = await get_tenant_project(session, ctx, project_id, for_update=True)
    if project.status == ProjectStatus.ARCHIVED:
        return project  # idempotent

    project.status = ProjectStatus.ARCHIVED
    project.archived_at = utcnow()
    project.version += 1
    project.updated_at = project.archived_at

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="project.archived",
        entity_type="project",
        entity_id=project.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"workspaceId": str(project.workspace_id), "version": project.version},
    )
    return project


# --- configuration revisions --------------------------------------------------


async def create_config_revision(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    project_id: uuid.UUID,
    config: dict[str, Any],
    comment: str = "",
) -> ProjectConfigRevision:
    """Write an immutable revision. Creating it does NOT activate it."""
    await authorize(ctx, Permission.PROJECTS_MANAGE)
    project = await get_tenant_project(session, ctx, project_id, for_update=True)
    if project.status == ProjectStatus.ARCHIVED:
        raise ValidationError(
            "project_archived",
            "An archived project cannot receive config revisions",
            details={"projectId": str(project_id)},
        )

    normalized = validate_config_document(config)
    template = await session.get(ProjectTemplate, project.template_id)
    assert template is not None
    lifecycle = template_lifecycle(template)
    _require_status_in_lifecycle(lifecycle, project.status_key, field="statusKey")

    ceiling = await ancestor_effective_governance(session, ctx.tenant_id, project)
    violations = governance_violations(normalized["governance"], ceiling)
    if violations:
        raise ValidationError(
            "governance_weakened",
            "A project may only tighten its ancestors' governance",
            details={"violations": violations},
        )
    await _assert_settings_allowed(session, ctx, project, normalized["settings"])

    next_revision = (
        int(
            (
                await session.scalar(
                    select(func.max(ProjectConfigRevision.revision)).where(
                        ProjectConfigRevision.project_id == project.id
                    )
                )
            )
            or 0
        )
        + 1
    )

    revision = ProjectConfigRevision(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        project_id=project.id,
        revision=next_revision,
        config=normalized,
        validation={
            "templateId": str(template.id),
            "templateKey": template.key,
            "templateVersion": template.version,
            "checkedAt": utcnow().isoformat(),
            "governanceCeiling": ceiling,
        },
        comment=comment,
        created_by=ctx.principal_id,
        created_at=utcnow(),
        activated_at=None,
    )
    session.add(revision)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="project.config_revision_created",
        entity_type="project",
        entity_id=project.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"revision": next_revision, "revisionId": str(revision.id), "comment": comment},
    )
    return revision


async def activate_config_revision(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    project_id: uuid.UUID,
    revision: int,
    expected_version: int,
) -> tuple[ProjectProfile, ProjectConfigRevision]:
    """Point the project at one revision — the only authoritative pointer."""
    await authorize(ctx, Permission.PROJECTS_MANAGE)
    project = await get_tenant_project(session, ctx, project_id, for_update=True)
    _require_version(project, expected_version)
    if project.status == ProjectStatus.ARCHIVED:
        raise ValidationError(
            "project_archived",
            "An archived project cannot activate config revisions",
            details={"projectId": str(project_id)},
        )

    row = await session.scalar(
        select(ProjectConfigRevision).where(
            ProjectConfigRevision.project_id == project.id,
            ProjectConfigRevision.revision == revision,
            ProjectConfigRevision.tenant_id == ctx.tenant_id,
        )
    )
    if row is None:
        raise NotFoundError(
            "Config revision not found",
            details={"projectId": str(project_id), "revision": revision},
        )

    template = await session.get(ProjectTemplate, project.template_id)
    assert template is not None
    lifecycle = template_lifecycle(template)
    # Re-validate at activation: the template, the ancestry or the ancestors'
    # governance may have changed since the revision was written.
    _require_status_in_lifecycle(lifecycle, project.status_key, field="statusKey")
    ceiling = await ancestor_effective_governance(session, ctx.tenant_id, project)
    violations = governance_violations(dict(row.config.get("governance") or {}), ceiling)
    if violations:
        raise ValidationError(
            "governance_weakened",
            "A project may only tighten its ancestors' governance",
            details={"violations": violations, "revision": revision},
        )
    await _assert_settings_allowed(session, ctx, project, dict(row.config.get("settings") or {}))

    now = utcnow()
    project.active_config_revision_id = row.id
    project.version += 1
    project.updated_at = now
    row.activated_at = now

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="project.config_revision_activated",
        entity_type="project",
        entity_id=project.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "revision": revision,
            "revisionId": str(row.id),
            "version": project.version,
        },
    )
    return project, row


# --- external references ------------------------------------------------------


async def add_external_reference(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    project_id: uuid.UUID,
    external_system: str,
    external_type: str,
    external_id: str,
    metadata: dict[str, Any] | None = None,
) -> tuple[ExternalReference, bool]:
    """Map an external identifier onto a project. Returns (row, created).

    The project scope is one case of the generic command (ADR-0047), not a
    parallel implementation: a second copy of idempotency, locking and conflict
    semantics would drift from the first one within a release.
    """
    return await register_external_reference(
        session,
        ctx,
        entity_type="project",
        entity_ref=str(project_id),
        external_system=external_system,
        external_type=external_type,
        external_id=external_id,
        metadata=metadata,
    )


async def project_workspace(session: AsyncSession, project: ProjectProfile) -> Workspace:
    workspace = await session.get(Workspace, project.workspace_id)
    assert workspace is not None
    return workspace
