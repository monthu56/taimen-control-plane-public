"""The entities of a workspace's knowledge, listed through the core (CP-ADR-0060, K031).

``POST /knowledge/entities:query`` lists every entity of the named kinds valid
at a moment whose attributes satisfy ``where`` -- "all licenses valid until the
end of the year" -- page by page, with no anchor to start from. Memory computes
the list (``POST /api/memory/entities:query``, MEM-ADR-020); the core decides
where it reads and what the caller sees there, exactly as for ``cp_recall``:
the namespace is the one a snapshot of the workspace lands in (the root of its
tree), the visibility is the caller's (``graph_scope``). The client names
neither.

As everywhere memory is read, the transaction closes before Memory is called.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext, ResourceRef, authorize
from control_plane.application.commands.knowledge import memory_failure
from control_plane.application.context.graph import GraphScope, deadline_after, within
from control_plane.application.queries.recall import graph_scope
from control_plane.config import Settings
from control_plane.domain.enums import Permission
from control_plane.domain.errors import AuthorizationError, DependencyUnavailableError
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider


@dataclass
class EntitiesQueryCall:
    scope: GraphScope
    kinds: list[str]
    limit: int
    as_of: datetime | None = None
    cursor: str | None = None
    # Literal conditions as the caller gave them: memory applies them.
    where: list[dict[str, Any]] = field(default_factory=list)


async def prepare_entities_query(
    session: AsyncSession,
    ctx: AuthContext,
    settings: Settings,
    *,
    workspace_id: uuid.UUID,
    kinds: list[str],
    where: list[dict[str, Any]],
    as_of: datetime | None,
    limit: int,
    cursor: str | None,
) -> EntitiesQueryCall:
    """Authorize on the workspace and resolve where memory is read (transactional half)."""
    # The right to read the workspace's context, as recall through /context/recall.
    await authorize(
        ctx, Permission.EVENTS_READ, resource=ResourceRef("workspace", str(workspace_id))
    )
    scope = await graph_scope(session, ctx, settings, workspace_id)
    # graph_scope adds the namespace of the workspace tree root after the
    # tenant's when the caller may read it; the tenant namespace holds no
    # snapshot, so the list reads the root's alone.
    if len(scope.namespaces) < 2:
        raise AuthorizationError(
            "The caller may not read this workspace's knowledge",
            details={"workspaceId": str(workspace_id)},
        )
    namespace = scope.namespaces[-1]
    return EntitiesQueryCall(
        scope=GraphScope(namespace=namespace, namespaces=[namespace], visibility=scope.visibility),
        kinds=list(dict.fromkeys(kinds)),
        limit=limit,
        as_of=as_of,
        cursor=cursor,
        where=list(where),
    )


def entities_request(call: EntitiesQueryCall) -> dict[str, Any]:
    """Memory's ``EntitiesQueryIn`` without ``namespaces`` (the provider adds them)."""
    request: dict[str, Any] = {"kinds": call.kinds, "limit": call.limit}
    if call.where:
        request["where"] = call.where
    if call.as_of is not None:
        request["asOf"] = call.as_of.isoformat()
    if call.cursor is not None:
        request["cursor"] = call.cursor
    return {**request, **call.scope.visibility}


async def fetch_entities(
    call: EntitiesQueryCall,
    provider: GraphProvider,
    settings: Settings,
    *,
    trace_run_id: str = "",
) -> dict[str, Any]:
    """Non-transactional half: one page, ``{items, nextCursor, asOf}``."""
    try:
        page = await within(
            deadline_after(settings),
            provider.query_entities(
                namespaces=call.scope.namespaces,
                request=entities_request(call),
                trace_run_id=trace_run_id or None,
            ),
        )
    except TimeoutError:
        raise DependencyUnavailableError(
            "Memory did not answer in time", code="memory_timeout"
        ) from None
    except ContextProviderError as exc:
        # Memory's 400 is a request it cannot read: a cursor of another list.
        raise memory_failure(exc, invalid={400: "entities_query_invalid"}) from exc
    return {
        "items": page.get("items") or [],
        "nextCursor": page.get("nextCursor"),
        "asOf": call.as_of.isoformat() if call.as_of else None,
    }
