"""Workspace type registry: what a node is and what may nest inside it.

A declarative constraint on the single workspace tree (ADR-0029), not an
extension mechanism: types carry a JSON Schema for node custom fields and a
list of allowed child type keys. Parent/child rules are checked under the same
per-tenant advisory lock as every other structural mutation, so two concurrent
creates cannot both slip past the check.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import Permission, WorkspaceStatus
from control_plane.domain.errors import ConflictError, NotFoundError, ValidationError
from control_plane.domain.project import validate_json_schema_document
from control_plane.infrastructure.db.models import Workspace, WorkspaceType

SYSTEM_TYPE_KEY = "generic"
ANY_CHILD = "*"
MAX_ALLOWED_CHILD_TYPES = 100


async def ensure_system_workspace_type(
    session: AsyncSession, tenant_id: uuid.UUID
) -> WorkspaceType:
    """The per-tenant fallback type, created once at bootstrap/migration."""
    existing = await session.scalar(
        select(WorkspaceType).where(
            WorkspaceType.tenant_id == tenant_id, WorkspaceType.is_system.is_(True)
        )
    )
    if existing is not None:
        return existing
    now = utcnow()
    workspace_type = WorkspaceType(
        id=new_uuid(),
        tenant_id=tenant_id,
        key=SYSTEM_TYPE_KEY,
        display_name="Generic Workspace",
        description="System default workspace type.",
        field_schema={},
        allowed_child_types=[ANY_CHILD],
        is_system=True,
        status=WorkspaceStatus.ACTIVE,
        version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(workspace_type)
    await session.flush()
    return workspace_type


async def get_tenant_workspace_type(
    session: AsyncSession, ctx: AuthContext, type_id: uuid.UUID, *, for_update: bool = False
) -> WorkspaceType:
    stmt = select(WorkspaceType).where(
        WorkspaceType.id == type_id, WorkspaceType.tenant_id == ctx.tenant_id
    )
    if for_update:
        stmt = stmt.with_for_update()
    workspace_type = await session.scalar(stmt)
    if workspace_type is None:
        raise NotFoundError("Workspace type not found", details={"workspaceTypeId": str(type_id)})
    return workspace_type


async def resolve_workspace_type(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    type_id: uuid.UUID | None,
    type_key: str | None,
) -> WorkspaceType:
    """Resolve a type by id or key, falling back to the tenant's system type."""
    if type_id is not None and type_key is not None:
        raise ValidationError(
            "invalid_workspace_type", "Provide either typeId or typeKey, not both"
        )
    if type_id is not None:
        return await get_tenant_workspace_type(session, ctx, type_id)
    if type_key is not None:
        workspace_type = await session.scalar(
            select(WorkspaceType).where(
                WorkspaceType.tenant_id == ctx.tenant_id, WorkspaceType.key == type_key
            )
        )
        if workspace_type is None:
            raise NotFoundError("Workspace type not found", details={"typeKey": type_key})
        return workspace_type
    return await ensure_system_workspace_type(session, ctx.tenant_id)


def check_child_allowed(parent: WorkspaceType, child: WorkspaceType) -> None:
    allowed = list(parent.allowed_child_types or [])
    if ANY_CHILD in allowed or child.key in allowed:
        return
    raise ValidationError(
        "child_type_not_allowed",
        f"Workspace type {child.key!r} is not allowed under {parent.key!r}",
        details={
            "parentTypeKey": parent.key,
            "childTypeKey": child.key,
            "allowedChildTypes": allowed,
        },
    )


def _validate_type_payload(*, field_schema: dict[str, Any], allowed_child_types: list[str]) -> None:
    validate_json_schema_document(field_schema, field_name="fieldSchema")
    if len(allowed_child_types) > MAX_ALLOWED_CHILD_TYPES:
        raise ValidationError(
            "invalid_workspace_type",
            f"at most {MAX_ALLOWED_CHILD_TYPES} allowed child types",
            details={"maxAllowedChildTypes": MAX_ALLOWED_CHILD_TYPES},
        )
    for value in allowed_child_types:
        if not value or len(value) > 63:
            raise ValidationError(
                "invalid_workspace_type",
                "allowedChildTypes entries must be non-empty type keys or '*'",
                details={"value": value},
            )


async def create_workspace_type(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    display_name: str,
    description: str = "",
    field_schema: dict[str, Any] | None = None,
    allowed_child_types: list[str] | None = None,
) -> WorkspaceType:
    await authorize(ctx, Permission.WORKSPACES_MANAGE)
    # Types constrain the tree, so they take the same per-tenant structural
    # lock: the uniqueness pre-check below is then race-free.
    from control_plane.application.commands.workspaces import lock_workspace_tree

    await lock_workspace_tree(session, ctx.tenant_id)
    schema = field_schema or {}
    children = allowed_child_types if allowed_child_types is not None else [ANY_CHILD]
    _validate_type_payload(field_schema=schema, allowed_child_types=children)

    existing = await session.scalar(
        select(WorkspaceType.id).where(
            WorkspaceType.tenant_id == ctx.tenant_id, WorkspaceType.key == key
        )
    )
    if existing is not None:
        raise ConflictError(
            "workspace_type_exists",
            "A workspace type with this key already exists",
            details={"key": key},
        )

    now = utcnow()
    workspace_type = WorkspaceType(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        key=key,
        display_name=display_name,
        description=description,
        field_schema=schema,
        allowed_child_types=children,
        is_system=False,
        status=WorkspaceStatus.ACTIVE,
        version=1,
        created_at=now,
        updated_at=now,
    )
    try:
        # SAVEPOINT backstop: the unique index must surface as a 409, never as
        # a poisoned transaction.
        async with session.begin_nested():
            session.add(workspace_type)
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            "workspace_type_exists",
            "A workspace type with this key already exists",
            details={"key": key},
        ) from exc

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace_type.created",
        entity_type="workspace_type",
        entity_id=workspace_type.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"key": key, "displayName": display_name, "allowedChildTypes": children},
    )
    return workspace_type


async def update_workspace_type(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    type_id: uuid.UUID,
    expected_version: int,
    display_name: str | None = None,
    description: str | None = None,
    field_schema: dict[str, Any] | None = None,
    allowed_child_types: list[str] | None = None,
) -> WorkspaceType:
    await authorize(ctx, Permission.WORKSPACES_MANAGE)
    workspace_type = await get_tenant_workspace_type(session, ctx, type_id, for_update=True)
    if workspace_type.version != expected_version:
        raise ConflictError(
            "version_conflict",
            "Workspace type version does not match If-Match",
            details={
                "workspaceTypeId": str(type_id),
                "expectedVersion": expected_version,
                "currentVersion": workspace_type.version,
            },
        )

    changes: dict[str, Any] = {}
    if display_name is not None and display_name != workspace_type.display_name:
        changes["displayName"] = display_name
        workspace_type.display_name = display_name
    if description is not None and description != workspace_type.description:
        changes["description"] = description
        workspace_type.description = description
    if field_schema is not None:
        validate_json_schema_document(field_schema, field_name="fieldSchema")
        if field_schema != workspace_type.field_schema:
            changes["fieldSchema"] = True
            workspace_type.field_schema = field_schema
    if allowed_child_types is not None:
        _validate_type_payload(
            field_schema=workspace_type.field_schema, allowed_child_types=allowed_child_types
        )
        if allowed_child_types != workspace_type.allowed_child_types:
            if workspace_type.is_system and ANY_CHILD not in allowed_child_types:
                raise ValidationError(
                    "system_type_immutable",
                    "The system workspace type must keep accepting any child type",
                    details={"workspaceTypeId": str(type_id)},
                )
            changes["allowedChildTypes"] = allowed_child_types
            workspace_type.allowed_child_types = allowed_child_types
    if not changes:
        raise ValidationError("empty_update", "No fields to update")

    workspace_type.version += 1
    workspace_type.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace_type.updated",
        entity_type="workspace_type",
        entity_id=workspace_type.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"changes": changes, "version": workspace_type.version},
    )
    return workspace_type


async def archive_workspace_type(
    session: AsyncSession, ctx: AuthContext, *, type_id: uuid.UUID
) -> WorkspaceType:
    await authorize(ctx, Permission.WORKSPACES_MANAGE)
    # Structural: without the tree lock a workspace could be created against
    # this type between the in-use check and the status write.
    from control_plane.application.commands.workspaces import lock_workspace_tree

    await lock_workspace_tree(session, ctx.tenant_id)
    workspace_type = await get_tenant_workspace_type(session, ctx, type_id, for_update=True)
    if workspace_type.is_system:
        raise ValidationError(
            "system_type_immutable",
            "The system workspace type cannot be archived",
            details={"workspaceTypeId": str(type_id)},
        )
    if workspace_type.status == WorkspaceStatus.ARCHIVED:
        return workspace_type  # idempotent

    # Archiving a type in use would leave the tree describing itself with a
    # type nobody may select — refuse instead of producing that state.
    in_use = await session.scalar(
        select(func.count())
        .select_from(Workspace)
        .where(
            Workspace.tenant_id == ctx.tenant_id,
            Workspace.type_id == type_id,
            Workspace.status == WorkspaceStatus.ACTIVE,
        )
    )
    if in_use:
        raise ValidationError(
            "workspace_type_in_use",
            "Active workspaces still use this type",
            details={"workspaceTypeId": str(type_id), "activeWorkspaces": int(in_use)},
        )

    workspace_type.status = WorkspaceStatus.ARCHIVED
    workspace_type.version += 1
    workspace_type.updated_at = utcnow()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="workspace_type.archived",
        entity_type="workspace_type",
        entity_id=workspace_type.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={"key": workspace_type.key},
    )
    return workspace_type
