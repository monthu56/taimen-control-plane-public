"""Process definitions and instances (CP-ADR-0074).

A process is a catalog object with a key; every published version is
immutable and addressed as ``key@version``. The core runs the instances
itself: state, timers and the decision journal live beside their tasks.

Contract first (constitution art. V): the routes, their permissions and their
bodies are published now, and each answers ``501 not_implemented`` after its
permission check until the step of process-packages named in the error. The
definitions are implemented (P006): publishing checks a version by the catalog
schema and the language (domain/process_definition.py) and writes it
immutable with its hash. The instances are implemented (P009): reading with
the decision journal, the operator's commands, and an explicit start
(``POST /process-instances``, TAI-ADR-0055) — application/commands/
process_instances.py. The replay of a candidate version on real journals is
implemented (P014) — application/commands/process_replays.py.
"""

import uuid
from datetime import datetime
from typing import Any, Literal

import pydantic
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    PACKAGE_FILTER_DESCRIPTION,
    ErrorEnvelope,
    PageOut,
    ProcessCancelRequest,
    ProcessDefinitionOut,
    ProcessDefinitionPublishRequest,
    ProcessInstanceOut,
    ProcessInstanceStartRequest,
    ProcessInstanceStatus,
    ProcessJournalEntryOut,
    ProcessOpenElementOut,
    ProcessProblemOut,
    ProcessReplayOut,
    ProcessReplayRequest,
    ProcessResumeRequest,
    ProcessStageStateOut,
    ProcessSuspendRequest,
    ProcessTimerOut,
    ProcessVersionOut,
    page_body,
)
from control_plane.api.write_flow import execute_write
from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands import process_definitions as commands
from control_plane.application.commands import process_instances as instances
from control_plane.application.commands import process_replays as replays
from control_plane.application.common import decode_cursor, encode_cursor
from control_plane.application.queries.lists import clamp_limit
from control_plane.application.queries.package_links import attach_package, attach_packages
from control_plane.domain.enums import Permission
from control_plane.domain.errors import NotImplementedYetError, ValidationError
from control_plane.domain.process_definition import SpecError, split_document

router = APIRouter(tags=["processes"])

RESPONSES = {
    **ERROR_RESPONSES,
    501: {"model": ErrorEnvelope, "description": "Not implemented yet"},
}


def pending(route: str, step: str, adr: str = "CP-ADR-0074") -> NotImplementedYetError:
    return NotImplementedYetError(
        f"{route} is part of the accepted contract but not implemented yet",
        details={"adr": adr, "implementedBy": f"process-packages {step}"},
    )


# --- definitions ---------------------------------------------------------------


def definition_body(view: commands.ProcessDefinitionView) -> dict[str, Any]:
    row = view.row
    return ProcessDefinitionOut(
        id=row.id,
        tenant_id=row.tenant_id,
        workspace_id=row.workspace_id,
        key=row.key,
        version=row.version,
        latest_version=view.latest_version,
        display_name=row.display_name,
        definition_hash=row.definition_hash,
        identity_agent=row.identity_agent,
        owner=row.spec.get("owner"),
        expression_profile=row.expression_profile,
        spec=row.spec,
        warnings=[ProcessProblemOut.model_validate(item) for item in row.warnings],
        created_by=row.created_by,
        created_at=row.created_at,
    ).model_dump(mode="json", by_alias=True)


def version_body(view: commands.ProcessDefinitionView) -> dict[str, Any]:
    row = view.row
    return ProcessVersionOut(
        id=row.id,
        key=row.key,
        version=row.version,
        latest_version=view.latest_version,
        workspace_id=row.workspace_id,
        display_name=row.display_name,
        definition_hash=row.definition_hash,
        identity_agent=row.identity_agent,
        expression_profile=row.expression_profile,
        warnings=[ProcessProblemOut.model_validate(item) for item in row.warnings],
        created_by=row.created_by,
        created_at=row.created_at,
    ).model_dump(mode="json", by_alias=True)


def process_request(document: dict[str, Any]) -> ProcessDefinitionPublishRequest:
    """The publish request of a package file of kind ``Process``.

    The object is the catalog document ``{apiVersion, kind, key, spec}``
    whose envelope the package check has already matched against the catalog
    schema and whose install variables the installer has substituted; it is
    published exactly as ``POST /process-definitions {key, spec}``
    (CP-ADR-0074 §11).
    """
    try:
        key, spec = split_document(document)
        return ProcessDefinitionPublishRequest.model_validate({"key": key, "spec": spec})
    except SpecError as exc:
        raise ValidationError(
            "invalid_process", exc.message, details={"kind": document.get("kind")}
        ) from exc
    except pydantic.ValidationError as exc:
        raise ValidationError(
            "invalid_process",
            "The process object is not {key, spec} of kind Process",
            details={
                "errors": [
                    {"path": "/" + "/".join(map(str, error["loc"])), "message": error["msg"]}
                    for error in exc.errors()
                ]
            },
        ) from exc


async def install_process(
    db: AsyncSession, ctx: AuthContext, document: dict[str, Any]
) -> commands.ProcessDefinitionView:
    """Install a package object of kind ``Process`` in the caller's transaction."""
    payload = process_request(document)
    return await commands.publish_process_definition(db, ctx, key=payload.key, spec=payload.spec)


@router.post(
    "/process-definitions",
    response_model=ProcessDefinitionOut,
    status_code=201,
    responses={
        **ERROR_RESPONSES,
        200: {"model": ProcessDefinitionOut, "description": "The same version, same hash"},
    },
    summary="Publish a process version: immutable, the same version with other content is 409",
)
async def publish_process_definition(
    payload: ProcessDefinitionPublishRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.PROCESSES_WRITE)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        view = await commands.publish_process_definition(
            db, ctx, key=payload.key, spec=payload.spec
        )
        return (201 if view.created else 200), await attach_package(
            db, ctx.tenant_id, "Process", definition_body(view)
        )

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(),
        executor=executor,
    )


@router.get("/process-definitions", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_process_definitions(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    key: str | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
    governed_by: str | None = Query(
        default=None,
        alias="governedBy",
        description="Only processes whose elements refer to this document (CP-ADR-0076 §6)",
    ),
    package: str | None = Query(default=None, description=PACKAGE_FILTER_DESCRIPTION),
) -> JSONResponse:
    after_key: str | None = None
    if cursor is not None:
        after_key = decode_cursor(cursor).get("k")
        if not isinstance(after_key, str):
            raise ValidationError("invalid_cursor", "Malformed pagination cursor")
    views, next_key = await commands.list_process_definitions(
        db,
        ctx,
        limit=clamp_limit(limit),
        after_key=after_key,
        key=key,
        workspace_id=workspace_id,
        governed_by=governed_by,
        package=package,
    )
    next_cursor = encode_cursor({"k": next_key}) if next_key is not None else None
    items = [definition_body(view) for view in views]
    await attach_packages(db, ctx.tenant_id, "Process", items)
    return JSONResponse(page_body(items, next_cursor))


@router.get(
    "/process-definitions/{ref}",
    response_model=ProcessDefinitionOut,
    responses=ERROR_RESPONSES,
    summary="A process by key (latest version) or key@version",
)
async def get_process_definition(ref: str, ctx: AuthDep, db: DbDep) -> JSONResponse:
    view = await commands.resolve_process_definition(db, ctx, ref)
    return JSONResponse(await attach_package(db, ctx.tenant_id, "Process", definition_body(view)))


@router.get(
    "/process-definitions/{key}/versions",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="Versions of a process, newest first, without their spec (ProcessVersionOut)",
)
async def list_process_versions(
    key: str,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    before: int | None = None
    if cursor is not None:
        before = decode_cursor(cursor).get("v")
        if not isinstance(before, int) or isinstance(before, bool):
            raise ValidationError("invalid_cursor", "Malformed pagination cursor")
    views, next_version = await commands.list_process_versions(
        db, ctx, key, limit=clamp_limit(limit), before_version=before
    )
    next_cursor = encode_cursor({"v": next_version}) if next_version is not None else None
    return JSONResponse(page_body([version_body(view) for view in views], next_cursor))


@router.post(
    "/process-definitions/{key}:replay",
    response_model=ProcessReplayOut,
    responses=RESPONSES,
    summary="Feed the journals of real instances to a candidate version; nothing is written",
)
async def replay_process_definition(
    key: str, payload: ProcessReplayRequest, ctx: AuthDep, session_factory: SessionFactoryDep
) -> JSONResponse:
    await authorize(ctx, Permission.PACKAGES_TEST)
    report = await replays.replay_candidate(
        session_factory,
        ctx,
        key=key,
        spec=payload.spec,
        instance_ids=payload.instance_ids,
        limit=payload.limit,
    )
    return JSONResponse(
        ProcessReplayOut.model_validate(report.out()).model_dump(mode="json", by_alias=True)
    )


# --- instances -----------------------------------------------------------------


def instance_body(view: instances.InstanceView) -> dict[str, Any]:
    instance = view.instance
    stages = (instance.state or {}).get("stages") or {}
    return ProcessInstanceOut(
        id=instance.id,
        tenant_id=instance.tenant_id,
        workspace_id=instance.workspace_id,
        definition_key=instance.definition_key,
        definition_version=instance.definition_version,
        instance_key=instance.instance_key,
        status=instance.status,
        outcome=instance.outcome,
        data=instance.data or {},
        stages=[
            ProcessStageStateOut(id=sid, state=record["state"]) for sid, record in stages.items()
        ],
        open_elements=[
            ProcessOpenElementOut.model_validate(item) for item in instances.open_elements(instance)
        ],
        timers=[
            ProcessTimerOut(
                id=timer.id,
                element=timer.element,
                due_at=timer.due_at,
                state=timer.state,
                provisional=timer.provisional,
                remaining_seconds=(
                    None if timer.remaining_seconds is None else int(timer.remaining_seconds)
                ),
            )
            for timer in view.timers
        ],
        started_at=instance.started_at,
        updated_at=instance.updated_at,
        completed_at=instance.completed_at,
    ).model_dump(mode="json", by_alias=True)


@router.post(
    "/process-instances",
    response_model=ProcessInstanceOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Start an instance without a trigger event; a key that has one is 409",
)
async def start_process_instance(
    payload: ProcessInstanceStartRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    await authorize(ctx, Permission.PROCESSES_OPERATE)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        instance = await instances.start_process_instance(
            db,
            ctx,
            process=payload.process,
            key=payload.key,
            data=payload.data,
            workspace_id=payload.workspace_id,
        )
        return 201, instance_body(await instances.instance_view(db, instance))

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(),
        executor=executor,
    )


@router.get("/process-instances", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_process_instances(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    definition_key: str | None = Query(default=None, alias="definitionKey"),
    instance_key: str | None = Query(default=None, alias="instanceKey"),
    status: ProcessInstanceStatus | None = Query(default=None),
    workspace_id: uuid.UUID | None = Query(default=None, alias="workspaceId"),
) -> JSONResponse:
    after: tuple[datetime, uuid.UUID] | None = None
    if cursor is not None:
        decoded = decode_cursor(cursor)
        try:
            after = (datetime.fromisoformat(decoded["t"]), uuid.UUID(decoded["i"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid_cursor", "Malformed pagination cursor") from exc
    rows, following = await instances.list_instances(
        db,
        ctx,
        limit=clamp_limit(limit),
        after=after,
        definition_key=definition_key,
        instance_key=instance_key,
        status=status,
        workspace_id=workspace_id,
    )
    items = [instance_body(await instances.instance_view(db, row)) for row in rows]
    next_cursor = (
        encode_cursor({"t": following[0].isoformat(), "i": str(following[1])})
        if following is not None
        else None
    )
    return JSONResponse(page_body(items, next_cursor))


@router.get(
    "/process-instances/{instance_id}",
    response_model=ProcessInstanceOut,
    responses=ERROR_RESPONSES,
    summary="State of an instance: data, stages, open elements, timers",
)
async def get_process_instance(instance_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    instance = await instances.get_instance(db, ctx, instance_id)
    return JSONResponse(instance_body(await instances.instance_view(db, instance)))


JournalKind = Literal[
    "input",
    "transition",
    "stage",
    "milestone",
    "timer",
    "intent",
    "vote",
    "recall",
    "compensation",
    "migration",
    "error",
]


@router.get(
    "/process-instances/{instance_id}/journal",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="Decision journal of an instance (ProcessJournalEntryOut), oldest first",
)
async def get_process_instance_journal(
    instance_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    kind: JournalKind | None = Query(default=None),
) -> JSONResponse:
    after: tuple[int, int] | None = None
    if cursor is not None:
        decoded = decode_cursor(cursor)
        seq, index = decoded.get("s"), decoded.get("n")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (seq, index)):
            raise ValidationError("invalid_cursor", "Malformed pagination cursor")
        after = (int(seq), int(index))  # type: ignore[arg-type]
    entries, following = await instances.instance_journal(
        db, ctx, instance_id, limit=clamp_limit(limit), after=after, kind=kind
    )
    items = [
        ProcessJournalEntryOut.model_validate(entry).model_dump(mode="json", by_alias=True)
        for entry in entries
    ]
    next_cursor = (
        encode_cursor({"s": following[0], "n": following[1]}) if following is not None else None
    )
    return JSONResponse(page_body(items, next_cursor))


async def _command(
    request: Request,
    ctx: AuthContext,
    settings: Any,
    session_factory: Any,
    instance_id: uuid.UUID,
    payload: pydantic.BaseModel,
    *,
    action: str,
    reason: str | None,
    compensate: bool = True,
) -> JSONResponse:
    await authorize(ctx, Permission.PROCESSES_OPERATE)

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        instance = await instances.command_instance(
            db, ctx, instance_id, action=action, reason=reason, compensate=compensate
        )
        return 200, instance_body(await instances.instance_view(db, instance))

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=f"{action}:{instance_id}:{payload.model_dump_json()}",
        executor=executor,
    )


@router.post(
    "/process-instances/{instance_id}:suspend",
    response_model=ProcessInstanceOut,
    responses=ERROR_RESPONSES,
    summary="Suspend an instance; its timers freeze",
)
async def suspend_process_instance(
    instance_id: uuid.UUID,
    payload: ProcessSuspendRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    return await _command(
        request,
        ctx,
        settings,
        session_factory,
        instance_id,
        payload,
        action="suspend",
        reason=payload.reason,
    )


@router.post(
    "/process-instances/{instance_id}:resume",
    response_model=ProcessInstanceOut,
    responses=ERROR_RESPONSES,
    summary="Resume a suspended instance; frozen timers get their remaining time back",
)
async def resume_process_instance(
    instance_id: uuid.UUID,
    payload: ProcessResumeRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    return await _command(
        request,
        ctx,
        settings,
        session_factory,
        instance_id,
        payload,
        action="resume",
        reason=payload.reason,
    )


@router.post(
    "/process-instances/{instance_id}:cancel",
    response_model=ProcessInstanceOut,
    responses=ERROR_RESPONSES,
    summary="Cancel an instance, compensating completed steps first by default",
)
async def cancel_process_instance(
    instance_id: uuid.UUID,
    payload: ProcessCancelRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    return await _command(
        request,
        ctx,
        settings,
        session_factory,
        instance_id,
        payload,
        action="cancel",
        reason=payload.reason,
        compensate=payload.compensate,
    )
