"""Artifact commands: append-oriented work products.

Artifacts are immutable records (create + read only). Revisions are new
artifacts; large binaries live in external storage behind ``uri`` — the
database keeps references and small JSON content, never blobs.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands._child_ceiling import enforce_run_ceiling
from control_plane.application.commands.relations import resolve_task
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotFoundError, ValidationError
from control_plane.infrastructure.db.models import Artifact, Run


async def create_artifact(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    type_: str,
    name: str,
    task_ref: str | None = None,
    run_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    uri: str | None = None,
    content: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    supersedes_artifact_id: uuid.UUID | None = None,
) -> Artifact:
    await authorize(ctx, Permission.ARTIFACTS_WRITE)
    if not type_.strip():
        raise ValidationError("invalid_type", "type must not be empty")
    if not name.strip():
        raise ValidationError("invalid_name", "name must not be empty")

    if supersedes_artifact_id is not None:
        superseded = await session.scalar(
            select(Artifact).where(
                Artifact.id == supersedes_artifact_id, Artifact.tenant_id == ctx.tenant_id
            )
        )
        if superseded is None:
            raise NotFoundError(
                "Superseded artifact not found",
                details={"supersedesArtifactId": str(supersedes_artifact_id)},
            )

    task_id: uuid.UUID | None = None
    if task_ref is not None:
        task_id = (await resolve_task(session, ctx, task_ref)).id

    run: Run | None = None
    if run_id is not None:
        run = await session.scalar(
            select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
        )
        if run is None:
            raise NotFoundError("Run not found", details={"runId": str(run_id)})
        await enforce_run_ceiling(session, ctx, run=run, permission=Permission.ARTIFACTS_WRITE)
        if task_id is None:
            task_id = run.task_id
        elif task_id != run.task_id:
            raise ValidationError(
                "artifact_mismatch",
                "run does not belong to the given task",
                details={"runId": str(run_id), "taskId": str(task_id)},
            )

    if workspace_id is not None:
        from control_plane.application.commands.workspaces import get_tenant_workspace

        await get_tenant_workspace(session, ctx, workspace_id)

    artifact = Artifact(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        workspace_id=workspace_id,
        task_id=task_id,
        run_id=run_id,
        created_by_principal_id=ctx.principal_id,
        type=type_.strip(),
        name=name.strip(),
        uri=uri,
        content=content,
        supersedes_artifact_id=supersedes_artifact_id,
        metadata_json=metadata or {},
        created_at=utcnow(),
    )
    session.add(artifact)
    await session.flush()

    # The event carries references only — artifact content stays out of the
    # journal (it may be large; it may not belong in the audit stream).
    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="artifact.created",
        entity_type="artifact",
        entity_id=artifact.id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "type": artifact.type,
            "name": artifact.name,
            "taskId": str(task_id) if task_id else None,
            "runId": str(run_id) if run_id else None,
            "uri": uri,
            "supersedesArtifactId": (
                str(supersedes_artifact_id) if supersedes_artifact_id else None
            ),
        },
    )
    return artifact
