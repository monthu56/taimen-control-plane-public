"""Run endpoints: execution attempts under a claim."""

import json
import uuid

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.api.dependencies import AuthDep, DbDep, SessionFactoryDep, SettingsDep
from control_plane.api.v1.schemas import (
    ERROR_RESPONSES,
    CheckpointCreateRequest,
    CheckpointOut,
    ManifestCompileRequest,
    ManifestEphemeralRequest,
    PageOut,
    RunActionCreateRequest,
    RunActionFinishRequest,
    RunActionOut,
    RunCancelRequest,
    RunControlMessageAcknowledgeRequest,
    RunControlMessageCreateRequest,
    RunControlMessageOut,
    RunControlMessageResultOut,
    RunFailRequest,
    RunHandoffRequest,
    RunOut,
    RunRequestCancelRequest,
    RunSucceedRequest,
    RunSuspendRequest,
    dump,
    page_body,
)
from control_plane.api.v1.task_bodies import task_body
from control_plane.api.write_flow import execute_write
from control_plane.application.commands import execution as execution_commands
from control_plane.application.commands import manifests as manifest_commands
from control_plane.application.commands import run_controls as control_commands
from control_plane.application.commands import runs as commands
from control_plane.application.queries import execution as queries
from control_plane.domain.errors import ValidationError

router = APIRouter(tags=["runs"])


@router.get("/runs", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_runs(
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
    task_id: uuid.UUID | None = Query(default=None, alias="taskId"),
    claim_id: uuid.UUID | None = Query(default=None, alias="claimId"),
    status: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_runs(
        db, ctx, limit=limit, cursor=cursor, task_id=task_id, claim_id=claim_id, status=status
    )
    return JSONResponse(page_body([dump(RunOut, r) for r in page.items], page.next_cursor))


@router.get("/runs/{run_id}", response_model=RunOut, responses=ERROR_RESPONSES)
async def get_run(run_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    run = await queries.get_run(db, ctx, run_id)
    return JSONResponse(dump(RunOut, run))


@router.post(
    "/runs/{run_id}/control-messages",
    response_model=RunControlMessageResultOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Append one durable Active Turn Control message",
)
async def create_control_message(
    run_id: uuid.UUID,
    payload: RunControlMessageCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    raw_key = request.headers.get("Idempotency-Key")
    idempotency_key = raw_key.strip() if raw_key is not None else ""
    if not idempotency_key or len(idempotency_key) > 200:
        raise ValidationError(
            "idempotency_key_required",
            "Idempotency-Key is required and must be 1..200 characters",
        )

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await control_commands.create_control_message(
            db,
            ctx,
            run_id=run_id,
            operation=payload.operation,
            causal_position=payload.causal_position,
            directive=payload.directive,
            reason=payload.reason,
            idempotency_key=idempotency_key,
            expected_run_version=payload.expected_run_version,
        )
        return 201, {
            "controlMessage": dump(RunControlMessageOut, result.control_message),
            "runVersion": result.run_version,
        }

    response = await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )
    body = json.loads(bytes(response.body))
    response.headers["Location"] = (
        f"/api/v1/runs/{run_id}/control-messages/{body['controlMessage']['id']}"
    )
    return response


@router.get(
    "/runs/{run_id}/control-messages",
    responses=ERROR_RESPONSES,
    summary="List durable Run control messages in Run-local sequence order",
)
async def list_control_messages(
    run_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_run_control_messages(db, ctx, run_id, limit=limit, cursor=cursor)
    return JSONResponse(
        {
            "items": [dump(RunControlMessageOut, message) for message in page.items],
            "nextCursor": page.next_cursor,
            "hasMore": page.has_more,
        }
    )


@router.post(
    "/runs/{run_id}/control-messages/{message_id}:acknowledge",
    response_model=RunControlMessageResultOut,
    responses=ERROR_RESPONSES,
    summary="Acknowledge the oldest accepted control message at a safe boundary",
)
async def acknowledge_control_message(
    run_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: RunControlMessageAcknowledgeRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await control_commands.acknowledge_control_message(
            db,
            ctx,
            run_id=run_id,
            message_id=message_id,
            status=payload.status,
            claim_id=payload.claim_id,
            fencing_token=payload.fencing_token,
            expected_run_version=payload.expected_run_version,
            expected_message_version=payload.expected_message_version,
            safe_boundary=payload.safe_boundary,
            reason=payload.reason,
        )
        return 200, {
            "controlMessage": dump(RunControlMessageOut, result.control_message),
            "runVersion": result.run_version,
        }

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get(
    "/runs/{run_id}/context",
    responses=ERROR_RESPONSES,
    summary="Structured execution context for a run (task, claim, artifacts, checkpoints, skills)",
)
async def get_run_context(run_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    return JSONResponse(await queries.get_run_context(db, ctx, run_id))


@router.get("/runs/{run_id}/checkpoints", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_checkpoints(
    run_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_run_checkpoints(db, ctx, run_id, limit=limit, cursor=cursor)
    return JSONResponse(page_body([dump(CheckpointOut, c) for c in page.items], page.next_cursor))


@router.post(
    "/runs/{run_id}/checkpoints",
    response_model=CheckpointOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Append a durable execution checkpoint (live owner only)",
)
async def create_checkpoint(
    run_id: uuid.UUID,
    payload: CheckpointCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        checkpoint = await execution_commands.create_checkpoint(
            db, ctx, run_id=run_id, kind=payload.kind, data=payload.data
        )
        return 201, dump(CheckpointOut, checkpoint)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get(
    "/runs/{run_id}/harness-manifest",
    responses=ERROR_RESPONSES,
    summary="Effective Harness Manifest of a run (frozen base, provenance, captured state)",
)
async def get_harness_manifest(
    run_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    version: int | None = Query(default=None, ge=1),
) -> JSONResponse:
    return JSONResponse(await queries.get_run_manifest(db, ctx, run_id, version=version))


@router.get(
    "/runs/{run_id}/harness-manifests",
    response_model=PageOut,
    responses=ERROR_RESPONSES,
    summary="Manifest version history of a run (newest first)",
)
async def list_harness_manifests(run_id: uuid.UUID, ctx: AuthDep, db: DbDep) -> JSONResponse:
    return JSONResponse(page_body(await queries.list_run_manifests(db, ctx, run_id), None))


@router.post(
    "/runs/{run_id}/harness-manifest:compile",
    responses=ERROR_RESPONSES,
    summary="Recompile the manifest; a new version appears only if the frozen base changed",
)
async def compile_harness_manifest(
    run_id: uuid.UUID,
    payload: ManifestCompileRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await manifest_commands.compile_run_manifest(
            db,
            ctx,
            run_id=run_id,
            reason=payload.reason,
            declared=payload.declared_sections(),
            memory_reference=payload.memory,
        )
        manifest = result.manifest
        # 201 only when a version was actually appended; an unchanged
        # configuration is a successful no-op, not a new piece of evidence.
        return (201 if result.created else 200), {
            "created": result.created,
            "manifest": {
                "id": str(manifest.id),
                "runId": str(manifest.run_id),
                "version": manifest.version,
                "baseHash": manifest.base_hash,
                "snapshotHash": manifest.snapshot_hash,
                "compileReason": manifest.compile_reason,
                "modelAttempt": manifest.model_attempt,
                "supersedesVersion": manifest.supersedes_version,
                "createdAt": manifest.created_at.isoformat(),
            },
        }

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/runs/{run_id}/harness-manifest/ephemeral",
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Record an ephemeral steering/warning marker (never changes the frozen base)",
)
async def record_manifest_ephemeral(
    run_id: uuid.UUID,
    payload: ManifestEphemeralRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        record = await manifest_commands.record_manifest_ephemeral(
            db,
            ctx,
            run_id=run_id,
            kind=payload.kind,
            summary=payload.summary,
            data=payload.data,
        )
        return 201, {
            "id": str(record.id),
            "runId": str(record.run_id),
            "manifestId": str(record.manifest_id),
            "seq": record.seq,
            "kind": record.kind,
            "summary": record.summary,
            "data": record.data,
            "createdAt": record.created_at.isoformat(),
        }

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.get("/runs/{run_id}/actions", response_model=PageOut, responses=ERROR_RESPONSES)
async def list_actions(
    run_id: uuid.UUID,
    ctx: AuthDep,
    db: DbDep,
    limit: int | None = Query(default=None),
    cursor: str | None = Query(default=None),
) -> JSONResponse:
    page = await queries.list_run_actions(db, ctx, run_id, limit=limit, cursor=cursor)
    return JSONResponse(page_body([dump(RunActionOut, a) for a in page.items], page.next_cursor))


@router.post(
    "/runs/{run_id}/actions",
    response_model=RunActionOut,
    status_code=201,
    responses=ERROR_RESPONSES,
    summary="Record an execution action (audit trail; enforces the run budget)",
)
async def record_action(
    run_id: uuid.UUID,
    payload: RunActionCreateRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        action = await execution_commands.record_run_action(
            db,
            ctx,
            run_id=run_id,
            action=payload.action,
            status=payload.status,
            skill_ref=payload.skill,
            external_reference=payload.external_reference,
            metadata=payload.metadata,
        )
        return 201, dump(RunActionOut, action)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/runs/{run_id}/actions/{action_id}:finish",
    response_model=RunActionOut,
    responses=ERROR_RESPONSES,
)
async def finish_action(
    run_id: uuid.UUID,
    action_id: uuid.UUID,
    payload: RunActionFinishRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        action = await execution_commands.finish_run_action(
            db,
            ctx,
            action_id=action_id,
            status=payload.status,
            external_reference=payload.external_reference,
            metadata=payload.metadata,
        )
        return 200, dump(RunActionOut, action)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/runs/{run_id}:suspend",
    responses=ERROR_RESPONSES,
    summary="Pause execution: run -> suspended, claim released (waiting semantics)",
)
async def suspend_run(
    run_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: RunSuspendRequest | None = None,
) -> JSONResponse:
    body = payload or RunSuspendRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        run, task = await commands.suspend_run(
            db,
            ctx,
            run_id=run_id,
            reason=body.reason,
            waiting_for_approval_id=body.waiting_for_approval_id,
        )
        return 200, {"run": dump(RunOut, run), "task": await task_body(db, ctx, task)}

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=body.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/runs/{run_id}:handoff",
    responses=ERROR_RESPONSES,
    summary="Checkpoint, suspend and release a run for a new human-operated harness",
)
async def prepare_handoff(
    run_id: uuid.UUID,
    payload: RunHandoffRequest,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
) -> JSONResponse:
    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        result = await commands.prepare_handoff(
            db,
            ctx,
            run_id=run_id,
            reason=payload.reason,
            checkpoint_kind=payload.checkpoint.kind,
            checkpoint_data=payload.checkpoint.data.model_dump(by_alias=True),
        )
        return 200, {
            "run": dump(RunOut, result.run),
            "task": await task_body(db, ctx, result.task),
            "checkpoint": dump(CheckpointOut, result.checkpoint),
            "eventCursor": result.event_cursor,
            "resume": {
                "taskId": str(result.task.id),
                "previousRunId": str(result.run.id),
                "nextAction": "claim_and_start_new_run",
            },
        }

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=payload.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/runs/{run_id}:request-cancel",
    response_model=RunOut,
    responses=ERROR_RESPONSES,
    summary="Signal cooperative cancellation (authoritative stop is :cancel)",
)
async def request_cancel_run(
    run_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: RunRequestCancelRequest | None = None,
) -> JSONResponse:
    body = payload or RunRequestCancelRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        run = await commands.request_cancel_run(db, ctx, run_id=run_id, reason=body.reason)
        return 200, dump(RunOut, run)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=body.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post(
    "/runs/{run_id}:succeed",
    responses=ERROR_RESPONSES,
    summary="Finish a run successfully (atomically completes the task by default)",
)
async def succeed_run(
    run_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: RunSucceedRequest | None = None,
) -> JSONResponse:
    body = payload or RunSucceedRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        run, task = await commands.succeed_run(
            db, ctx, run_id=run_id, output=body.output, complete_task=body.complete_task
        )
        return 200, {"run": dump(RunOut, run), "task": await task_body(db, ctx, task)}

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=body.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post("/runs/{run_id}:fail", responses=ERROR_RESPONSES)
async def fail_run(
    run_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: RunFailRequest | None = None,
) -> JSONResponse:
    body = payload or RunFailRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        run = await commands.fail_run(
            db, ctx, run_id=run_id, failure_reason=body.failure_reason, output=body.output
        )
        return 200, dump(RunOut, run)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=body.model_dump_json(exclude_unset=True),
        executor=executor,
    )


@router.post("/runs/{run_id}:cancel", responses=ERROR_RESPONSES)
async def cancel_run(
    run_id: uuid.UUID,
    request: Request,
    ctx: AuthDep,
    settings: SettingsDep,
    session_factory: SessionFactoryDep,
    payload: RunCancelRequest | None = None,
) -> JSONResponse:
    body = payload or RunCancelRequest()

    async def executor(db: AsyncSession) -> tuple[int, dict[str, object]]:
        run = await commands.cancel_run(db, ctx, run_id=run_id, reason=body.reason)
        return 200, dump(RunOut, run)

    return await execute_write(
        request,
        ctx,
        settings,
        session_factory,
        canonical_body=body.model_dump_json(exclude_unset=True),
        executor=executor,
    )
