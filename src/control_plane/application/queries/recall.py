"""Pull side of the task context: ``cp_recall`` and pack replay (CP-ADR-0064, TAI-ADR-0042 p.6).

``POST /context/recall`` lets an agent ask the knowledge graph during its work:
from an anchor (an identifier it has in hand) or from a query (identifiers are
extracted from it with the domain packs' ``idPatterns``, exactly as from a
task description), along the relations it names, at a moment it names. It is
the same typed traversal, client and visibility as the task context pack —
the agent sees nothing its principal could not read through ``/context``.

``POST /context-packs/{id}:replay`` sends a recorded pack's request again with
the caller's visibility and says whether the answer still uses the same
entities and facts: how a reviewer sees what the executor saw.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, authorize
from control_plane.application.commands.knowledge import memory_failure
from control_plane.application.commands.relations import resolve_task
from control_plane.application.commands.workspaces import workspace_ancestor_ids
from control_plane.application.context.graph import (
    GraphScope,
    base_scope,
    deadline_after,
    drift,
    kind_patterns,
    off_loop,
    typed,
    used_of,
    within,
    within_budget,
    workspace_read,
)
from control_plane.application.queries.context import memory_visibility, principal_scopes
from control_plane.application.queries.task_context import get_pack_record, public_record
from control_plane.config import Settings
from control_plane.domain.context_schema import (
    DEFAULT_BUDGET_TOKENS,
    MAX_ANCHORS,
    MAX_CANDIDATE_CHARS,
    Candidate,
    extract_identifiers,
)
from control_plane.domain.enums import Permission
from control_plane.domain.errors import (
    DependencyUnavailableError,
    NotFoundError,
    ValidationError,
)
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider
from control_plane.infrastructure.db.models import Task, Workspace


@dataclass
class RecallCall:
    scope: GraphScope
    anchor: str = ""
    kind: str = ""
    query: str = ""
    kinds: list[str] = field(default_factory=list)
    traverse: list[dict[str, Any]] = field(default_factory=list)
    as_of: datetime | None = None
    budget_tokens: int = DEFAULT_BUDGET_TOKENS


def require_graph(provider: object | None) -> GraphProvider:
    if provider is None:
        raise DependencyUnavailableError(
            "Context memory provider is not configured", code="memory_disabled"
        )
    return provider  # type: ignore[return-value]


async def graph_scope(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    workspace_id: uuid.UUID | None,
) -> GraphScope:
    """Namespaces and visibility of a graph read, as ``POST /context`` computes them."""
    scope = base_scope(settings, ctx)
    visible = await memory_visibility(ctx, settings)
    if visible is not None:
        scope.visibility = {"allowedNamespaces": visible[0], "allowedScopes": visible[1]}
    if workspace_id is None:
        return scope
    workspace = await session.scalar(
        select(Workspace).where(Workspace.id == workspace_id, Workspace.tenant_id == ctx.tenant_id)
    )
    if workspace is None:
        raise NotFoundError("Workspace not found", details={"workspaceId": str(workspace_id)})
    ancestors = await workspace_ancestor_ids(session, ctx.tenant_id, workspace.id)
    ws_namespace, narrowed = workspace_read(
        settings,
        ctx,
        visible,
        ancestors[-1] if ancestors else workspace.id,
        [f"workspace:{w}" for w in ancestors],
        principal_scopes(ctx),
    )
    if ws_namespace is not None:
        scope.namespaces.append(ws_namespace)
    if narrowed is not None:
        scope.visibility["allowedScopes"] = narrowed
    return scope


async def prepare_recall(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    anchor: str | None,
    kind: str | None,
    query: str | None,
    kinds: list[str],
    relations: list[str],
    direction: str,
    depth: int,
    limit: int,
    as_of: datetime | None,
    task_ref: str | None,
    workspace_id: uuid.UUID | None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> RecallCall:
    """Authorize and resolve the scope of a recall (transactional half)."""
    # The graph is durable memory: the same right as recall through /context.
    await authorize(ctx, Permission.EVENTS_READ)
    if bool(anchor) == bool(query):
        raise ValidationError("invalid_recall_request", "Give exactly one of anchor or query")
    if task_ref is not None:
        await authorize(ctx, Permission.TASKS_READ)
        task: Task = await resolve_task(session, ctx, task_ref)
        if workspace_id is None:
            workspace_id = task.workspace_id
    return RecallCall(
        scope=await graph_scope(session, ctx, settings, workspace_id),
        anchor=(anchor or "").strip(),
        kind=kind or "",
        query=(query or "").strip(),
        kinds=list(dict.fromkeys(kinds)),
        traverse=[
            {"relation": r, "direction": direction, "depth": depth, "limit": limit}
            for r in dict.fromkeys(relations)
        ],
        as_of=as_of,
        budget_tokens=budget_tokens,
    )


async def fetch_recall(
    call: RecallCall,
    provider: GraphProvider,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> dict[str, Any]:
    """Non-transactional half: resolve the anchors and traverse."""
    deadline = deadline_after(settings)
    trace = trace_run_id or None
    warnings: list[str] = []
    semantic = False
    try:
        if call.anchor:
            anchors = [
                Candidate(value=call.anchor[:MAX_CANDIDATE_CHARS], kind=call.kind, source="anchor")
            ]
        else:
            catalog = await within(
                deadline,
                kind_patterns(
                    provider, call.scope.namespaces, trace_run_id=trace, warnings=warnings
                ),
            )
            found = await off_loop(
                deadline,
                extract_identifiers,
                call.query,
                catalog.patterns,
                call.kinds,
                aliases=catalog.aliases,
            )
            anchors = [
                Candidate(value=value, kind=found_kind, source="query")
                for found_kind, value in found
            ]
            if not anchors:
                # Nothing deterministic in the query: Memory may add semantic
                # hits, which it marks ``evidence: inferred`` (TAI-ADR-0042 p.4).
                anchors = [Candidate(value=call.query[:MAX_CANDIDATE_CHARS], source="query")]
                semantic = True
        request: dict[str, Any] = {
            "anchors": [c.to_request() for c in anchors[:MAX_ANCHORS]],
            "traverse": call.traverse,
            "allow_semantic": semantic,
        }
        if call.as_of is not None:
            request["as_of"] = call.as_of.isoformat()
        pack = await typed(provider, call.scope, request, deadline=deadline, trace_run_id=trace)
    except TimeoutError:
        raise DependencyUnavailableError(
            "Memory did not answer in time", code="memory_timeout"
        ) from None
    except ContextProviderError as exc:
        raise memory_failure(exc) from exc
    return {
        "anchors": [c.to_request() for c in anchors[:MAX_ANCHORS]],
        "semantic": semantic,
        "asOf": call.as_of.isoformat() if call.as_of else None,
        "namespaces": list(call.scope.namespaces),
        "warnings": warnings,
        "pack": within_budget(pack, call.budget_tokens),
    }


async def prepare_replay(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    pack_id: uuid.UUID,
) -> tuple[dict[str, Any], GraphScope]:
    """The recorded pack and the caller's visibility over its namespaces."""
    await authorize(ctx, Permission.EVENTS_READ)
    record = await get_pack_record(session, ctx, pack_id)
    task = await session.get(Task, uuid.UUID(record["taskId"]))
    assert task is not None
    current = await graph_scope(session, ctx, settings, task.workspace_id)
    scope = GraphScope(
        namespace=record["namespaces"][0],
        namespaces=list(record["namespaces"]),
        visibility=current.visibility,
    )
    return record, scope


async def fetch_replay(
    record: dict[str, Any],
    scope: GraphScope,
    provider: GraphProvider,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> dict[str, Any]:
    """Send the recorded request again; compare what the answer uses."""
    if not record["request"].get("anchors"):
        # Every anchor was redacted for this reader: nothing to send.
        return _replayed(record, {})
    try:
        pack = await typed(
            provider,
            scope,
            record["request"],
            deadline=deadline_after(settings),
            trace_run_id=trace_run_id or None,
        )
    except TimeoutError:
        raise DependencyUnavailableError(
            "Memory did not answer in time", code="memory_timeout"
        ) from None
    except ContextProviderError as exc:
        raise memory_failure(exc) from exc
    return _replayed(record, pack)


def _replayed(record: dict[str, Any], pack: dict[str, Any]) -> dict[str, Any]:
    difference = drift(record["used"], used_of(pack))
    return {
        "contextPack": public_record(record),
        "reproduced": not any(difference.values()),
        "drift": difference,
        "pack": within_budget(pack, record["budgetTokens"] or DEFAULT_BUDGET_TOKENS),
    }
