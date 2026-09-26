"""Task endpoints: CRUD, claim, complete.

Optimistic concurrency: GET returns ``ETag: "task-<version>"``; PATCH and
:complete require ``If-Match`` and answer ``409 version_conflict`` on mismatch.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.etag import format_task_etag, parse_if_match
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    ClaimOut,
    ClaimTaskRequest,
    PageOut,
    RelationCreateRequest,
    RunOut,
    RunStartRequest,
    TaskCompleteRequest,
    TaskCreateRequest,
    TaskOut,
    TaskRelationOut,
    TaskRequirementsSpec,
    TaskUpdateRequest,
    TaskVerificationOut,
    dump,
    page_body,
    work_document,
)
from control_plane.api.v1.task_bodies import task_bodies, task_body
from control_plane.api.write_flow import as_no_content, execute_write
from control_plane.application.commands import claims as claim_commands
from control_plane.application.commands import relations as relation_commands
from control_plane.application.commands import runs as run_commands
from control_plane.application.commands import tasks as commands
from control_plane.application.commands.eligibility import RequirementSpec
from control_plane.application.commands.tasks import _UNSET
from control_plane.application.queries import execution as execution_queries
from control_plane.application.queries import lists as queries
from control_plane.domain.errors import ValidationError


def _to_spec(requirements: TaskRequirementsSpec | None) -> RequirementSpec | None:
    if requirements is None:
        return None
    return RequirementSpec(
        roles=requirements.roles,
        capabilities=requirements.capabilities,
        skills=requirements.skills,
    )


router = APIRouter(tags=["tasks"])


@router.post("/tasks", response_model=TaskOut, status_code=201, responses=ERROR_RESPONSES)
async def create_task(
    payload: TaskCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        origin = work_document(payload.origin)
        if origin is None and payload.parent_task is not None:
            # A subtask filed together with its parent relation is, by
            # construction, a decomposition of that parent (CP-ADR-0062).
            parent = await relation_commands.resolve_task(db, ctx, payload.parent_task)
            origin = {"kind": "parent", "ref": f"task:{parent.id}"}
        task = await commands.create_task(
            db,
            ctx,
            title=payload.title,
            description=payload.description,
            priority=payload.priority,
            status=payload.status,
            type_id=payload.type_id,
            type_key=payload.type_key,
            type_version=payload.type_version,
            owner_id=payload.owner_id,
            assignee_id=payload.assignee_id,
            workspace_id=payload.workspace_id,
            custom_fields=payload.custom_fields,
            start_date=payload.start_date,
            due_date=payload.due_date,
            requirements=_to_spec(payload.requirements),
            goal_id=payload.goal_id,
            origin=origin,
            acceptance=work_document(payload.acceptance),
            evidence=work_document(payload.evidence),
        )
        if payload.parent_task is not None:
            await relation_commands.add_relation(
                db,
                ctx,
                from_task_ref=str(task.id),
                to_task_ref=payload.parent_task,
                relation_type="parent",
            )
        return 201, await task_body(db, ctx, task)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/tasks", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_tasks(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    status: str | None = Query(default=None),
    system_status_category: str | None = Query(default=None, alias="systemStatusCategory"),
    type_key: str | None = Query(default=None, alias="typeKey"),
    priority: str | None = Query(default=None),
    owner_id: uuid.UUID | None = Query(default=None, alias="ownerId"),
    assignee_id: uuid.UUID | None = Query(default=None, alias="assigneeId"),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    include_descendants: bool = Query(default=False, alias="includeDescendants"),
    project_id: uuid.UUID | None = Query(default=None, alias="projectId"),
    include_subprojects: bool = Query(default=False, alias="includeSubprojects"),
    start_from: datetime | None = Query(default=None, alias="startFrom"),
    start_to: datetime | None = Query(default=None, alias="startTo"),
    due_from: datetime | None = Query(default=None, alias="dueFrom"),
    due_to: datetime | None = Query(default=None, alias="dueTo"),
    sort: str | None = Query(default=None),
    goal_id: uuid.UUID | None = Query(default=None, alias="goalId"),
) -> JSONResponse:
    page = await queries.list_tasks(
        db,
        ctx,
        limit=limit,
        cursor=cursor,
        status=status,
        system_status_category=system_status_category,
        type_key=type_key,
        priority=priority,
        owner_id=owner_id,
        assignee_id=assignee_id,
        workspace_id=workspace_id,
        include_descendants=include_descendants,
        project_id=project_id,
        include_subprojects=include_subprojects,
        start_from=start_from,
        start_to=start_to,
        due_from=due_from,
        due_to=due_to,
        sort=sort,
        goal_id=goal_id,
    )
    return JSONResponse(page_body(await task_bodies(db, ctx, page.items), page.next_cursor))


@router.get(
    "/tasks/{task_ref}/claimability",
    responses=ERROR_RESPONSES,
    summary="Advisory diagnosis: can the caller claim this task, and why not",
)
async def get_task_claimability(task_ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    from control_plane.application.queries import discovery as discovery_queries

    return JSONResponse(await discovery_queries.explain_task_claimability(db, ctx, task_ref))


@router.get(
    "/tasks/{task_ref}/transitions",
    responses=ERROR_RESPONSES,
    summary="Where this task may move next, per the lifecycle of its type",
)
async def get_task_transitions(task_ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    from control_plane.application.queries import discovery as discovery_queries

    return JSONResponse(await discovery_queries.explain_task_transitions(db, ctx, task_ref))


@router.get(
    "/tasks/{task_ref}/verifications",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="Attempts of the verification stage, newest first (CP-ADR-0067)",
)
async def list_verifications(
    task_ref: str,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    from control_plane.application.queries import verifications as verification_queries

    page = await verification_queries.list_verifications(
        db, ctx, task_ref=task_ref, limit=limit, cursor=cursor
    )
    return JSONResponse(
        page_body([dump(TaskVerificationOut, v) for v in page.items], page.next_cursor)
    )


@router.get("/tasks/{task_ref}", response_model=TaskOut, responses=ERROR_RESPONSES)
async def get_task(task_ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    task = await queries.get_task(db, ctx, task_ref)
    return JSONResponse(
        await task_body(db, ctx, task), headers={"ETag": format_task_etag(task.version)}
    )


# Null on these is never "clear it": there is nothing to clear to. The planned
# dates are deliberately NOT here — an explicit null is how a date is removed.
_NON_NULLABLE_PATCH_FIELDS = (
    "title",
    "description",
    "priority",
    "status",
    "custom_fields",
    "acceptance",
    "evidence",
)


@router.patch("/tasks/{task_ref}", response_model=TaskOut, responses=ERROR_RESPONSES)
async def update_task(
    task_ref: str,
    payload: TaskUpdateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match)
    provided = payload.model_dump(exclude_unset=True)
    for field in _NON_NULLABLE_PATCH_FIELDS:
        if field in provided and provided[field] is None:
            raise ValidationError("invalid_field", f"{field} cannot be null")
    # Explicit null would silently be a no-op (None means "untouched" in the
    # command); make the contract loud: clearing is an EMPTY requirements object.
    if "requirements" in provided and provided["requirements"] is None:
        raise ValidationError(
            "invalid_field",
            "requirements cannot be null; send an empty object to clear them",
        )

    def pick(field: str) -> Any:
        return provided.get(field, _UNSET)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        task = await commands.update_task(
            db,
            ctx,
            task_ref=task_ref,
            expected_version=expected_version,
            claim_id=payload.claim_id,
            fencing_token=payload.fencing_token,
            title=pick("title"),
            description=pick("description"),
            priority=pick("priority"),
            status=pick("status"),
            owner_id=pick("owner_id"),
            assignee_id=pick("assignee_id"),
            workspace_id=pick("workspace_id"),
            custom_fields=pick("custom_fields"),
            start_date=pick("start_date"),
            due_date=pick("due_date"),
            requirements=_to_spec(payload.requirements),
            goal_id=pick("goal_id"),
            acceptance=(work_document(payload.acceptance) if "acceptance" in provided else _UNSET),
            evidence=work_document(payload.evidence) if "evidence" in provided else _UNSET,
        )
        return 200, await task_body(db, ctx, task)

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n"
        + payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
    return response


@router.post(
    "/tasks/{task_ref}:claim",
    response_model=ClaimOut,
    responses=ERROR_RESPONSES,
    summary="Atomically claim a task (lease + fencing token)",
)
async def claim_task(
    task_ref: str,
    payload: ClaimTaskRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        claim = await claim_commands.claim_task(
            db,
            ctx,
            settings,
            task_ref=task_ref,
            session_id=payload.session_id,
            ttl_seconds=payload.ttl_seconds,
            intent=payload.intent,
        )
        return 200, dump(ClaimOut, claim)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/tasks/{task_ref}:complete",
    response_model=TaskOut,
    responses=ERROR_RESPONSES,
)
async def complete_task(
    task_ref: str,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: TaskCompleteRequest | None = None,
    if_match: str | None = Header(default=None),
) -> JSONResponse:
    expected_version = parse_if_match(if_match)
    body = payload or TaskCompleteRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        task = await commands.complete_task(
            db,
            ctx,
            task_ref=task_ref,
            expected_version=expected_version,
            claim_id=body.claim_id,
            fencing_token=body.fencing_token,
        )
        return 200, await task_body(db, ctx, task)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"if-match:{expected_version}\n" + body.model_dump_json(exclude_unset=True),
        executor=executor,
    )


# --- v0.2: relations, requirements, runs --------------------------------------


@router.post(
    "/tasks/{task_ref}/relations",
    response_model=TaskRelationOut,
    status_code=201,
    responses=ERROR_RESPONSES,
)
async def add_relation(
    task_ref: str,
    payload: RelationCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        relation = await relation_commands.add_relation(
            db,
            ctx,
            from_task_ref=task_ref,
            to_task_ref=payload.to_task,
            relation_type=payload.type,
        )
        return 201, dump(TaskRelationOut, relation)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/tasks/{task_ref}/relations", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_relations(task_ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    relations = await execution_queries.list_task_relations(db, ctx, task_ref)
    return JSONResponse(page_body([dump(TaskRelationOut, r) for r in relations], None))


@router.delete(
    "/tasks/{task_ref}/relations/{relation_id}",
    status_code=204,
    responses=ERROR_RESPONSES,
)
async def remove_relation(
    task_ref: str,
    relation_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> Response:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        await relation_commands.remove_relation(db, ctx, task_ref=task_ref, relation_id=relation_id)
        return 204, {}

    return as_no_content(
        await execute_write(
            request, ctx, settings, session_factory, canonical_body="", executor=executor
        )
    )


@router.get("/tasks/{task_ref}/requirements", responses=ERROR_RESPONSES)
async def get_requirements(task_ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    requirements = await execution_queries.get_task_requirements(db, ctx, task_ref)
    return JSONResponse(requirements)


@router.post(
    "/tasks/{task_ref}:start-run",
    response_model=RunOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Start an execution attempt under a live claim",
)
async def start_run(
    task_ref: str,
    payload: RunStartRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        run = await run_commands.start_run(
            db,
            ctx,
            task_ref=task_ref,
            claim_id=payload.claim_id,
            fencing_token=payload.fencing_token,
            input_data=payload.input,
            metadata=payload.metadata,
            max_duration_seconds=payload.max_duration_seconds,
            max_actions=payload.max_actions,
        )
        return 201, dump(RunOut, run)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
