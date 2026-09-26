"""Effective Harness Manifest commands (HRS-2).

The manifest is compiled server-side from authoritative state inside the same
transaction that reads it, and stored as immutable evidence of one run. Two
entry points:

``compile_for_run``
    internal, used by ``start_run``: every run gets version 1 or the run does
    not start. A run without evidence is worse than no run at all.
``compile_run_manifest``
    the public command: re-compiles under the same fencing gate as a
    checkpoint and creates a new version *only* if the frozen base changed.

Idempotent by content: an unchanged ``base_hash`` returns the active version
instead of appending a duplicate, which is what makes "a new version means a
real configuration change" a true statement rather than a convention.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands._child_ceiling import ceiling_of_run
from control_plane.application.commands.tasks import enforce_claim_gate
from control_plane.application.common import new_uuid, utcnow
from control_plane.application.events import record_event
from control_plane.application.queries.harness import (
    current_event_cursor,
    resolve_executable_skills,
)
from control_plane.application.queries.tool_policy import (
    project_policy_for_task,
    resolve_effective_tool_policy,
)
from control_plane.domain.enums import Permission, RunStatus
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from control_plane.domain.harness_manifest import (
    COMPILE_REASONS,
    MAX_DECLARED_BYTES,
    BudgetInput,
    CapturedInput,
    IdentityInput,
    RunInput,
    ToolInput,
    compile_manifest,
    validate_ephemeral,
)
from control_plane.domain.project import guard_json_document, reject_secret_material
from control_plane.domain.redaction import reject_unsafe_durable_payload
from control_plane.infrastructure.db.models import (
    Principal,
    Run,
    RunHarnessManifest,
    RunManifestEphemeral,
    Session,
    Task,
)

_BUDGET_CEILING_KEYS = ("maxRunDurationSeconds", "maxRunActions", "maxConcurrentRuns")


@dataclass(frozen=True)
class ManifestResult:
    manifest: RunHarnessManifest
    created: bool


def _validate_memory_reference(memory: dict[str, Any] | None) -> dict[str, Any] | None:
    """A *reference* to a Context Pack — never its content (ADR-0028)."""
    if memory is None:
        return None
    guard_json_document(memory, label="memory", max_bytes=MAX_DECLARED_BYTES)
    reject_secret_material(memory, label="memory")
    reject_unsafe_durable_payload(
        memory, code="unsafe_manifest_payload", subject="Memory reference"
    )
    return memory


async def _active_manifest(
    session: AsyncSession, run_id: uuid.UUID, *, for_update: bool = False
) -> RunHarnessManifest | None:
    stmt = (
        select(RunHarnessManifest)
        .where(RunHarnessManifest.run_id == run_id)
        .order_by(RunHarnessManifest.version.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    manifest: RunHarnessManifest | None = await session.scalar(stmt)
    return manifest


async def compile_for_run(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run: Run,
    task: Task,
    reason: str,
    declared: dict[str, Any] | None = None,
    memory_reference: dict[str, Any] | None = None,
) -> ManifestResult:
    """Compile and persist the manifest for ``run`` (caller holds the locks)."""
    if reason not in COMPILE_REASONS:
        raise ValidationError(
            "invalid_compile_reason",
            f"reason must be one of {list(COMPILE_REASONS)}",
            details={"reason": reason},
        )

    # A run launched by a parent sees only the skills its handle granted
    # (HRS-7); a root run has no handle and is not narrowed.
    bound = await ceiling_of_run(session, ctx.tenant_id, run.id)
    child_grant_skills = frozenset(bound[1].skills) if bound is not None else None

    work_session = await session.get(Session, run.session_id)
    principal = await session.get(Principal, run.principal_id)
    project_policy = await project_policy_for_task(session, ctx, task)
    tools = await resolve_executable_skills(
        session,
        ctx,
        harness_capabilities=work_session.harness_capabilities if work_session else None,
    )
    # The same revisions discovery reports, captured inside the evidence: an
    # auditor can replay "which tools were visible" instead of trusting the
    # list the manifest happens to contain.
    tool_policy = await resolve_effective_tool_policy(session, ctx, run_id=run.id)
    # Cursor first, like every other context read: a transition committing
    # mid-build must land after the captured cursor, never vanish behind it.
    event_cursor = await current_event_cursor(session, ctx.tenant_id)

    active = await _active_manifest(session, run.id, for_update=True)
    if reason == "provider_fallback":
        declared_attempt = ((declared or {}).get("model") or {}).get("attempt")
        previous = active.model_attempt if active else 0
        if not isinstance(declared_attempt, int) or declared_attempt <= previous:
            raise ValidationError(
                "invalid_fallback_attempt",
                "A provider fallback must declare model.attempt greater than the active one",
                details={"declared": declared_attempt, "active": previous},
            )

    compiled = compile_manifest(
        identity=IdentityInput(
            tenant_id=str(ctx.tenant_id),
            principal_id=str(run.principal_id),
            principal_kind=principal.kind if principal else "unknown",
            session_id=str(run.session_id),
            control_level=work_session.control_level if work_session else "connected",
            harness_type=work_session.harness_type if work_session else None,
            harness_version=work_session.harness_version if work_session else None,
            protocol_version=work_session.protocol_version if work_session else None,
            harness_capabilities=tuple(
                work_session.harness_capabilities or [] if work_session else []
            ),
        ),
        run=RunInput(
            run_id=str(run.id),
            task_id=str(run.task_id),
            claim_id=str(run.claim_id),
            attempt=run.attempt,
            fencing_token=run.fencing_token,
        ),
        project_policy=project_policy,
        tools=tuple(
            ToolInput(
                skill_id=tool["id"],
                name=tool["name"],
                version=tool["version"],
                protocol=tool["protocol"],
                status=tool["status"],
            )
            for tool in tools
        ),
        budgets=BudgetInput(
            max_duration_seconds=run.max_duration_seconds,
            max_actions=run.max_actions,
            governance_ceiling={
                key: project_policy.governance[key]
                for key in _BUDGET_CEILING_KEYS
                if key in project_policy.governance
            },
        ),
        captured=CapturedInput(
            event_cursor=event_cursor,
            task_version=task.version,
            claim_epoch=task.claim_epoch,
            run_attempt=run.attempt,
            captured_at=utcnow().isoformat(),
            memory=_validate_memory_reference(memory_reference),
        ),
        declared=declared,
        catalog_revision=tool_policy.catalog_revision,
        policy_revision=tool_policy.policy_revision,
        child_grant_skills=child_grant_skills,
    )

    if active is not None and active.base_hash == compiled.base_hash:
        # Same effective configuration: the cursor moved, the policy did not.
        return ManifestResult(manifest=active, created=False)

    manifest = RunHarnessManifest(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        run_id=run.id,
        task_id=run.task_id,
        project_id=uuid.UUID(project_policy.project_id) if project_policy.project_id else None,
        version=(active.version + 1) if active else 1,
        base_hash=compiled.base_hash,
        snapshot_hash=compiled.snapshot_hash,
        base=compiled.base,
        provenance=compiled.provenance,
        captured=compiled.captured,
        compile_reason=reason,
        model_attempt=compiled.model_attempt,
        supersedes_version=active.version if active else None,
        created_by=ctx.principal_id,
        created_at=utcnow(),
    )
    session.add(manifest)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.manifest_compiled",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        # References and hashes only: the audit trail points at evidence, it
        # does not copy it (ADR-0019).
        payload={
            "taskId": str(run.task_id),
            "manifestId": str(manifest.id),
            "version": manifest.version,
            "baseHash": manifest.base_hash,
            "reason": reason,
            "modelAttempt": manifest.model_attempt,
            "supersedesVersion": manifest.supersedes_version,
        },
    )
    return ManifestResult(manifest=manifest, created=True)


async def _live_run_for_write(
    session: AsyncSession, ctx: AuthContext, run_id: uuid.UUID
) -> tuple[Run, Task]:
    """Same gate as a checkpoint: running, own run, live claim, right epoch."""
    probe = await session.scalar(
        select(Run).where(Run.id == run_id, Run.tenant_id == ctx.tenant_id)
    )
    if probe is None:
        raise NotFoundError("Run not found", details={"runId": str(run_id)})
    task = await session.scalar(select(Task).where(Task.id == probe.task_id).with_for_update())
    run = await session.scalar(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if task is None or run is None:  # pragma: no cover - FK guarantees existence
        raise NotFoundError("Run not found", details={"runId": str(run_id)})
    if run.status != RunStatus.RUNNING:
        raise ConflictError(
            "run_not_active",
            "Run is not running",
            details={"runId": str(run.id), "status": run.status},
        )
    if run.principal_id != ctx.principal_id:
        raise AuthorizationError(
            "Run belongs to another principal",
            code="run_holder_mismatch",
            details={"runId": str(run.id)},
        )
    live_claim = await enforce_claim_gate(
        session, ctx, task, claim_id=run.claim_id, fencing_token=run.fencing_token
    )
    if live_claim is None or live_claim.id != run.claim_id:
        raise ConflictError(
            "stale_claim",
            "The run's claim is no longer live",
            details={"runId": str(run.id), "claimId": str(run.claim_id)},
        )
    return run, task


async def compile_run_manifest(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    reason: str = "recompile",
    declared: dict[str, Any] | None = None,
    memory_reference: dict[str, Any] | None = None,
) -> ManifestResult:
    await authorize(ctx, Permission.TASKS_CLAIM)
    if reason == "run_started":
        raise ValidationError(
            "invalid_compile_reason",
            "run_started is reserved for the automatic compilation at run start",
        )
    run, task = await _live_run_for_write(session, ctx, run_id)
    return await compile_for_run(
        session,
        ctx,
        run=run,
        task=task,
        reason=reason,
        declared=declared,
        memory_reference=memory_reference,
    )


async def record_manifest_ephemeral(
    session: AsyncSession,
    ctx: AuthContext,
    *,
    run_id: uuid.UUID,
    kind: str,
    summary: str,
    data: dict[str, Any] | None = None,
) -> RunManifestEphemeral:
    """Attach a steering/warning marker to the active manifest version.

    Deliberately a separate row rather than a mutation: the frozen base cannot
    change silently, and an operator reading the manifest can always tell a
    temporary correction from durable configuration.
    """
    await authorize(ctx, Permission.TASKS_CLAIM)
    run, _ = await _live_run_for_write(session, ctx, run_id)
    payload = validate_ephemeral(kind, summary, data)

    manifest = await _active_manifest(session, run.id, for_update=True)
    if manifest is None:
        raise NotFoundError("Run has no harness manifest", details={"runId": str(run.id)})
    last_seq = (
        await session.scalar(
            select(func.max(RunManifestEphemeral.seq)).where(
                RunManifestEphemeral.manifest_id == manifest.id
            )
        )
    ) or 0
    record = RunManifestEphemeral(
        id=new_uuid(),
        tenant_id=ctx.tenant_id,
        run_id=run.id,
        manifest_id=manifest.id,
        seq=last_seq + 1,
        kind=kind,
        summary=summary,
        data=payload,
        created_by=ctx.principal_id,
        created_at=utcnow(),
    )
    session.add(record)
    await session.flush()

    await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="run.manifest_ephemeral_recorded",
        entity_type="run",
        entity_id=run.id,
        actor_id=ctx.principal_id,
        session_id=run.session_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "taskId": str(run.task_id),
            "manifestId": str(manifest.id),
            "version": manifest.version,
            "seq": record.seq,
            "kind": record.kind,
        },
    )
    return record
