"""Knowledge snapshots and domain packs through the core (CP-ADR-0060).

Nobody but the core talks to the Memory Service (TAI-ADR-0031 p.6): a
connector hands the core a snapshot of what it sees, the core authorizes the
caller on the workspace, computes WHERE the knowledge lands (namespace of the
workspace tree root) and WHO may see it (the workspace scope), and forwards the
document with its own identity. The client never names a namespace or scope.

Each operation is split so that no database transaction is open while the
Memory Service is awaited: ``prepare_*`` runs in one transaction (authorization
and resolution), the provider call runs outside, and the journal event (a
reconciled snapshot, a registered pack, a namespace's enabled packs) is
appended in a second transaction.

A snapshot that opened, changed or closed nodes is also journaled as
``knowledge.changed`` with their natural keys (CP-ADR-0076 §7): the event a
rule reacts to when a regulation changes. The keys come from Memory's answer
(``changes``, amendment MEM-ADR-020); an empty reconciliation, a repeated
snapshot and an answer without ``changes`` write no such event.
"""

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import (
    AuthContext,
    ResourceRef,
    authorize,
)
from control_plane.application.commands.workspaces import (
    require_active_workspace,
    workspace_ancestor_ids,
)
from control_plane.application.events import record_event
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    AuthorizationError,
    ConflictError,
    DependencyUnavailableError,
    UpstreamError,
    ValidationError,
)
from control_plane.infrastructure.context_provider import (
    ContextProviderError,
    KnowledgeProvider,
    workspace_namespace,
)

logger = logging.getLogger(__name__)

# Counters copied from the Memory answer into the journal event: numbers only,
# never snapshot content.
_MAX_COUNTERS = 32
# ``changes`` of Memory's reconcile answer (MEM-ADR-020): the lists in the
# order the event names them, and the core's own bound on what one event
# carries (Memory's limit is per list and configurable).
CHANGE_LISTS = ("opened", "changed", "closed")
MAX_EVENT_CHANGES = 200
_MAX_CHANGE_TEXT = 512


@dataclass(frozen=True)
class KnowledgeTarget:
    """Where a workspace's knowledge lives in Memory."""

    workspace_id: uuid.UUID
    root_workspace_id: uuid.UUID
    namespace: str
    scopes: list[str]


async def _workspace_target(
    session: AsyncSession, ctx: AuthContext, settings: Settings, workspace_id: uuid.UUID
) -> KnowledgeTarget:
    workspace = await require_active_workspace(session, ctx, workspace_id)
    # Nearest first, root last: the whole tree shares the root's namespace and a
    # sub-workspace is told apart by its visibility scope.
    root_id = (await workspace_ancestor_ids(session, ctx.tenant_id, workspace.id))[-1]
    return KnowledgeTarget(
        workspace_id=workspace.id,
        root_workspace_id=root_id,
        namespace=workspace_namespace(settings, ctx.tenant_id, root_id),
        scopes=[f"workspace:{workspace.id}"],
    )


def require_provider(provider: object | None) -> KnowledgeProvider:
    if provider is None:
        raise DependencyUnavailableError(
            "Context memory provider is not configured",
            code="memory_disabled",
        )
    return provider  # type: ignore[return-value]


def memory_failure(
    exc: ContextProviderError,
    *,
    invalid: dict[int, str] | None = None,
    conflict_code: str | None = None,
) -> Exception:
    """Map a Memory failure onto the API by meaning (CP-ADR-0060).

    ``invalid`` names the 422 code for the Memory statuses that mean "the
    caller's document is wrong" on this call; ``conflict_code`` gives Memory's
    409 its conflict code. Everything else -- a credential the core's identity
    lacks (401/403), an unexpected 4xx, 5xx, transport -- is the core's or
    Memory's problem, not the client's: 502. Memory's own text goes to the log
    only; the client gets our code and ``memoryStatus``.
    """
    logger.warning("memory call failed: %s", exc)
    status = exc.status
    if status is not None and invalid and status in invalid:
        return ValidationError(
            invalid[status],
            "Memory rejected the document",
            details={"memoryStatus": status},
        )
    if status == 409 and conflict_code is not None:
        return ConflictError(
            conflict_code,
            _CONFLICT_MESSAGES.get(conflict_code, "Memory reported a conflict"),
            details={"memoryStatus": 409},
        )
    # A 4xx is final for the same request: repeating it cannot succeed.
    retryable = exc.retryable and not (status is not None and 400 <= status < 500)
    return UpstreamError(
        "memory_unavailable",
        "Memory service failed to process the request",
        details={"memoryStatus": status, "retryable": retryable},
    )


_CONFLICT_MESSAGES = {
    "snapshot_stale": "Memory already holds a newer snapshot of this source",
    "pack_version_conflict": "This pack version is already registered with other content",
}


# --- snapshots -------------------------------------------------------------


async def prepare_snapshot(
    session: AsyncSession, ctx: AuthContext, settings: Settings, *, workspace_id: uuid.UUID
) -> KnowledgeTarget:
    await authorize(
        ctx, Permission.OBSERVATIONS_WRITE, resource=ResourceRef("workspace", str(workspace_id))
    )
    return await _workspace_target(session, ctx, settings, workspace_id)


def _counters(answer: dict[str, Any]) -> dict[str, int]:
    """Integer counters of a Memory answer, one level of nesting flattened."""
    counters: dict[str, int] = {}

    def take(key: str, value: object) -> None:
        if isinstance(value, int) and not isinstance(value, bool) and len(counters) < _MAX_COUNTERS:
            counters[key] = value

    for key, value in answer.items():
        if key == "changes":
            continue  # keys, not counters: knowledge.changed carries them
        if isinstance(value, dict):
            for inner, inner_value in value.items():
                take(f"{key}.{inner}", inner_value)
        else:
            take(key, value)
    return counters


async def reconcile_snapshot(
    provider: KnowledgeProvider,
    target: KnowledgeTarget,
    snapshot: dict[str, Any],
    *,
    trace_run_id: str | None = None,
) -> dict[str, Any]:
    try:
        return await provider.reconcile_snapshot(
            namespace=target.namespace,
            scopes=target.scopes,
            snapshot=snapshot,
            trace_run_id=trace_run_id,
        )
    except ContextProviderError as exc:
        raise memory_failure(
            exc, invalid={400: "snapshot_invalid"}, conflict_code="snapshot_stale"
        ) from exc


async def record_snapshot_reconciled(
    session: AsyncSession,
    ctx: AuthContext,
    target: KnowledgeTarget,
    snapshot: dict[str, Any],
    answer: dict[str, Any],
) -> uuid.UUID:
    """Journal the fact of reconciliation: identity and counters, no content."""
    event = await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="knowledge.snapshot_reconciled",
        entity_type="workspace",
        entity_id=target.workspace_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "snapshotId": snapshot.get("snapshotId"),
            "pack": snapshot.get("pack"),
            "source": snapshot.get("source"),
            "observedAt": snapshot.get("observedAt"),
            "workspaceId": str(target.workspace_id),
            "rootWorkspaceId": str(target.root_workspace_id),
            "namespace": target.namespace,
            "entityCount": len(snapshot.get("entities") or []),
            "relationCount": len(snapshot.get("relations") or []),
            "duplicate": bool(answer.get("duplicate", False)),
            "counters": _counters(answer),
        },
    )
    changes, truncated = changed_keys(answer)
    if changes:
        await record_event(
            session,
            tenant_id=ctx.tenant_id,
            event_type="knowledge.changed",
            entity_type="workspace",
            entity_id=target.workspace_id,
            actor_id=ctx.principal_id,
            request_id=ctx.request_id,
            correlation_id=ctx.correlation_id,
            causation_id=ctx.causation_id,
            trace_run_id=ctx.trace_run_id,
            payload={
                "snapshotId": snapshot.get("snapshotId"),
                "pack": snapshot.get("pack"),
                "source": snapshot.get("source"),
                "observedAt": snapshot.get("observedAt"),
                "workspaceId": str(target.workspace_id),
                "rootWorkspaceId": str(target.root_workspace_id),
                "namespace": target.namespace,
                "changes": changes,
                "truncated": truncated,
                "counters": _counters(answer),
            },
        )
    return event.id


def changed_keys(answer: dict[str, Any]) -> tuple[list[dict[str, str]], bool]:
    """``[{kind, key, change}]`` of the nodes a reconciliation touched, and whether cut.

    Read from ``answer.changes`` (MEM-ADR-020: ``{opened, changed, closed:
    [{kind, key}], limit, truncated}``): opened first, then changed, then
    closed, each in Memory's order. A duplicate snapshot touched nothing. A
    malformed entry is skipped; the list is cut at :data:`MAX_EVENT_CHANGES`,
    and ``truncated`` says Memory or the core cut it.
    """
    raw = answer.get("changes")
    if not isinstance(raw, dict) or answer.get("duplicate") is True:
        return [], False
    changes: list[dict[str, str]] = []
    truncated = raw.get("truncated") is True
    seen: set[tuple[str, str, str]] = set()
    for change in CHANGE_LISTS:
        entries = raw.get(change)
        for entry in entries if isinstance(entries, list) else ():
            if not isinstance(entry, dict):
                continue
            kind, key = entry.get("kind"), entry.get("key")
            if not isinstance(kind, str) or not isinstance(key, str) or not key:
                continue
            if len(kind) > _MAX_CHANGE_TEXT or len(key) > _MAX_CHANGE_TEXT:
                truncated = True  # a key the event cannot carry whole is not guessed at
                continue
            if (kind, key, change) in seen:
                continue
            if len(changes) >= MAX_EVENT_CHANGES:
                return changes, True
            seen.add((kind, key, change))
            changes.append({"kind": kind, "key": key, "change": change})
    return changes, truncated


# --- domain packs ----------------------------------------------------------

# Memory's pack name and version grammar (``core.kinds``): a namespace enables
# only pinned ``name@version`` references.
PACK_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}@[0-9A-Za-z][0-9A-Za-z.+-]{0,31}$")


def authorize_pack_registration(ctx: AuthContext, settings: Settings) -> None:
    """Only platform administrators register packs.

    Memory's pack registry is shared by every tenant and an unpinned reference
    resolves to the latest version, so a tenant administrator could break
    another tenant's strict checking by publishing a new version. The list is
    ``CP_KNOWLEDGE_PACK_ADMINS`` (Control Plane or IAM principal ids); empty
    closes the endpoint.
    """
    admins = {item.strip() for item in settings.knowledge_pack_admins if item.strip()}
    actor = {str(ctx.principal_id)}
    if ctx.iam_principal_id is not None:
        actor.add(str(ctx.iam_principal_id))
    if not admins & actor:
        raise AuthorizationError(
            "Registering knowledge packs is reserved to platform administrators",
            details={"required": "knowledge_pack_admin"},
        )


def require_pack_identity(package: dict[str, Any]) -> None:
    """A manifest names its pack and version, as Memory's ``PackIn`` requires.

    Checked here so that the client gets ``422 pack_invalid`` instead of a 502
    for Memory's own model 422; the grammar of both stays Memory's (400)."""
    name = package.get("name")
    version = package.get("version")
    if not isinstance(name, str) or not name.strip():
        raise ValidationError(
            "pack_invalid", "Pack manifest must have a name", details={"field": "name"}
        )
    # Memory reads ``version: 1`` as "1"; a bool is not a version.
    if (
        isinstance(version, bool)
        or not isinstance(version, str | int | float)
        or not str(version).strip()
    ):
        raise ValidationError(
            "pack_invalid", "Pack manifest must have a version", details={"field": "version"}
        )


async def register_pack(
    provider: KnowledgeProvider,
    package: dict[str, Any],
    *,
    trace_run_id: str | None = None,
) -> dict[str, Any]:
    try:
        return await provider.register_package(package=package, trace_run_id=trace_run_id)
    except ContextProviderError as exc:
        raise memory_failure(
            exc, invalid={400: "pack_invalid"}, conflict_code="pack_version_conflict"
        ) from exc


def _pack_identity(package: dict[str, Any], answer: dict[str, Any]) -> tuple[str, str]:
    """Name and version of a registered pack, preferring Memory's normalized
    answer (``{status, pack: {name, version, ...}}``; ``version: 1`` -> ``"1"``)."""
    registered = answer.get("pack")
    source = registered if isinstance(registered, dict) else package

    def pick(key: str) -> str:
        value = source.get(key, package.get(key))
        return "" if value is None or isinstance(value, dict | list) else str(value)

    return pick("name")[:64], pick("version")[:32]


async def record_pack_registered(
    session: AsyncSession,
    ctx: AuthContext,
    package: dict[str, Any],
    answer: dict[str, Any],
) -> uuid.UUID:
    """Journal who registered which pack version (manifest content stays out)."""
    name, version = _pack_identity(package, answer)
    status = answer.get("status")
    event = await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="knowledge.pack_registered",
        entity_type="knowledge_pack",
        # Packs live in Memory and have no row here: a stable id per version
        # groups the journal entries of one pack version.
        entity_id=uuid.uuid5(uuid.NAMESPACE_URL, f"knowledge-pack:{name}@{version}"),
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "name": name,
            "version": version,
            "status": status if isinstance(status, str) else None,
        },
    )
    return event.id


def require_pinned_packs(packs: list[str]) -> list[str]:
    """Deduplicated ``name@version`` references; an unpinned one is 422."""
    refs = list(dict.fromkeys(ref.strip() for ref in packs))
    unpinned = [ref for ref in refs if not PACK_REF_RE.match(ref)]
    if unpinned:
        raise ValidationError(
            "pack_version_required",
            "Knowledge packs are enabled by pinned name@version references",
            details={"packs": unpinned[:16]},
        )
    return refs


async def prepare_workspace_packs(
    session: AsyncSession, ctx: AuthContext, settings: Settings, *, workspace_id: uuid.UUID
) -> KnowledgeTarget:
    await authorize(
        ctx, Permission.WORKSPACES_MANAGE, resource=ResourceRef("workspace", str(workspace_id))
    )
    target = await _workspace_target(session, ctx, settings, workspace_id)
    # Packs and strict mode belong to the namespace, i.e. to the whole tree:
    # an administrator of a sub-workspace must not reshape the root's memory.
    if target.root_workspace_id != target.workspace_id:
        raise ValidationError(
            "workspace_not_root",
            "Knowledge packs are set on the root workspace of the tree",
            details={
                "workspaceId": str(workspace_id),
                "rootWorkspaceId": str(target.root_workspace_id),
            },
        )
    return target


async def set_workspace_packs(
    provider: KnowledgeProvider,
    target: KnowledgeTarget,
    *,
    packs: list[str],
    strict: bool,
    trace_run_id: str | None = None,
) -> dict[str, Any]:
    try:
        return await provider.set_namespace_kinds(
            namespace=target.namespace,
            packages=packs,
            strict=strict,
            trace_run_id=trace_run_id,
        )
    except ContextProviderError as exc:
        raise memory_failure(exc, invalid={400: "pack_invalid", 404: "pack_not_found"}) from exc


async def record_workspace_packs_set(
    session: AsyncSession,
    ctx: AuthContext,
    target: KnowledgeTarget,
    *,
    packs: list[str],
    strict: bool,
) -> uuid.UUID:
    """Journal who enabled which pinned packs (and strictness) for a namespace."""
    event = await record_event(
        session,
        tenant_id=ctx.tenant_id,
        event_type="knowledge.packs_configured",
        entity_type="workspace",
        entity_id=target.workspace_id,
        actor_id=ctx.principal_id,
        request_id=ctx.request_id,
        correlation_id=ctx.correlation_id,
        causation_id=ctx.causation_id,
        trace_run_id=ctx.trace_run_id,
        payload={
            "workspaceId": str(target.workspace_id),
            "namespace": target.namespace,
            "packs": packs,
            "strict": strict,
        },
    )
    return event.id
