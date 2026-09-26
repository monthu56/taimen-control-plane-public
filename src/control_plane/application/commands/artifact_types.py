"""Artifact type versions: create and resolve (CP-ADR-0072 §6).

Shaped like the work item type registry (ADR-0048): a POST creates the next
version of a key, a version never changes after it is written. An artifact of
a registered type is checked against the LATEST version of its key; an
artifact whose type is not registered is accepted as before.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.artifact_type import (
    ArtifactTypeDefinition,
    validate_artifact_type_definition,
)
from control_plane.domain.enums import ArtifactTypeStatus, Permission
from control_plane.domain.errors import NotFoundError
from control_plane.infrastructure.db.models import ArtifactType


async def _lock_type_key(session: AsyncSession, tenant_id: uuid.UUID, key: str) -> None:
    """Serialize version allocation for one (tenant, key)."""
    await session.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(f"cp:at:{tenant_id}:{key}", 0)))
    )


def definition_of(artifact_type: ArtifactType) -> ArtifactTypeDefinition:
    return ArtifactTypeDefinition(
        metadata_schema=artifact_type.metadata_schema,
        media_types=list(artifact_type.media_types),
        max_bytes=artifact_type.max_bytes,
    )


async def create_artifact_type_version(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    key: str,
    display_name: str,
    description: str = "",
    metadata_schema: dict[str, Any] | None = None,
    media_types: list[str],
    max_bytes: int | None = None,
    global_max_bytes: int,
) -> ArtifactType:
    """Create the next version of ``key`` — never an in-place edit."""
    await authorize(ctx, Permission.ARTIFACT_TYPES_MANAGE)
    definition = validate_artifact_type_definition(
        metadata_schema=metadata_schema,
        media_types=media_types,
        max_bytes=max_bytes,
        global_max_bytes=global_max_bytes,
    )
    await _lock_type_key(session, ctx.tenant_id, key)
    current_max = await session.scalar(
        select(func.max(ArtifactType.version)).where(
            ArtifactType.tenant_id == ctx.tenant_id, ArtifactType.key == key
        )
    )
    version = int(current_max or 0) + 1

    now = utcnow()
    artifact_type = ArtifactType(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        key=key,
        version=version,
        display_name=display_name,
        description=description,
        metadata_schema=definition.metadata_schema,
        media_types=definition.media_types,
        max_bytes=definition.max_bytes,
        status=ArtifactTypeStatus.ACTIVE,
        created_by=ctx.principal_id,
        created_at=now,
        updated_at=now,
    )
    session.add(artifact_type)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="artifact_type.created",
        entity_type="artifact_type",
        entity_id=artifact_type.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "key": key,
            "version": version,
            "mediaTypes": definition.media_types,
            "maxBytes": definition.max_bytes,
            "declaresMetadataSchema": bool(definition.metadata_schema),
        },
    )
    return artifact_type


async def latest_artifact_type(
    session: AsyncSession, tenant_id: uuid.UUID, key: str
) -> ArtifactType | None:
    """The version an artifact of ``key`` is checked against, if registered."""
    return await session.scalar(
        select(ArtifactType)
        .where(ArtifactType.tenant_id == tenant_id, ArtifactType.key == key)
        .order_by(ArtifactType.version.desc())
        .limit(1)
    )


async def resolve_artifact_type(session: AsyncSession, ctx: AuthContext, ref: str) -> ArtifactType:
    """``key`` (latest version) or ``key@version``."""
    key, pinned, version_text = ref.partition("@")
    not_found = NotFoundError("Artifact type not found", details={"artifactType": ref})
    if not pinned:
        artifact_type = await latest_artifact_type(session, ctx.tenant_id, key)
    else:
        if not (version_text.isascii() and version_text.isdigit()) or len(version_text) > 9:
            raise not_found
        artifact_type = await session.scalar(
            select(ArtifactType).where(
                ArtifactType.tenant_id == ctx.tenant_id,
                ArtifactType.key == key,
                ArtifactType.version == int(version_text),
            )
        )
    if artifact_type is None:
        raise not_found
    return artifact_type
