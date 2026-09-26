"""The executor instructions of one task, assembled from its layers (CP-ADR-0066).

Read-side helper shared by run start (the hash and refs recorded on the run),
``GET /runs/{id}/context`` and the working context: all three must agree on
what "the instructions of this task" are, so they are assembled in one place.

No authorization here: every caller has already authorized reading the task or
the run, and the project layer is the prose of the task's own project — not the
project's configuration, which stays behind ``projects:read``.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.queries.projects import effective_config_for, project_for_workspace
from control_plane.domain.agent_instructions import (
    PROJECT_SETTING,
    SOURCE_PROJECT,
    SOURCE_TASK_TYPE,
    assemble_instructions,
    layer,
)
from control_plane.infrastructure.db.models import ProjectProfile, Task, TaskType


async def _project_layer(
    session: AsyncSession, tenant_id: uuid.UUID, task: Task
) -> dict[str, Any] | None:
    if task.workspace_id is None:
        return None
    project_id = await project_for_workspace(session, tenant_id, task.workspace_id)
    if project_id is None:
        return None
    project = await session.get(ProjectProfile, project_id)
    if project is None or project.tenant_id != tenant_id:  # pragma: no cover - FK
        return None
    effective = await effective_config_for(session, tenant_id, project)
    text = (effective.config.get("settings") or {}).get(PROJECT_SETTING)
    if not isinstance(text, str) or not text:
        return None
    layers = effective.provenance.get("layers") or []
    revision = (layers[-1] if layers else {}).get("revision")
    return layer(SOURCE_PROJECT, f"project:{project.id}", revision, text)


async def instructions_for_task(
    session: AsyncSession, tenant_id: uuid.UUID, task: Task
) -> dict[str, Any]:
    """``{layers: [{source, ref, version, text}], hash}`` for ``task``."""
    task_type = await session.get(TaskType, task.type_id)
    type_layer = (
        layer(
            SOURCE_TASK_TYPE, f"taskType:{task_type.key}", task_type.version, task_type.instructions
        )
        if task_type is not None and task_type.instructions
        else None
    )
    return assemble_instructions(
        project=await _project_layer(session, tenant_id, task), task_type=type_layer
    )
