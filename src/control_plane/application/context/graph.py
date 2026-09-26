"""Reading the knowledge graph through Memory's typed traversal (CP-ADR-0064).

One client and one wire format for the two readers of TAI-ADR-0042 p.6: the
task context pack (push, into the executor's prompt) and ``cp_recall`` (pull,
by the agent during its work). Both ask Memory's Context Compiler
``POST /api/memory/context/typed`` with the core's identity and the caller's
visibility, and both extract anchor candidates from text the same way — with
the ``idPatterns`` of the domain packs enabled for the namespace, read from
Memory's pack registry, never from core code.

Nothing here opens a database transaction: callers resolve and authorize in
one, then call Memory outside of it (ADR-0025).
"""

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import anyio
import regex

from control_plane.application.authorization import AuthContext
from control_plane.config import Settings
from control_plane.domain.context_schema import CHARS_PER_TOKEN
from control_plane.infrastructure.context_provider import (
    ContextProviderError,
    GraphProvider,
    tenant_namespace,
    workspace_namespace,
)

logger = logging.getLogger(__name__)

# Pack versions are immutable in Memory's registry, so their compiled patterns
# are cached for the life of the process; the bound only caps memory use.
_PACK_CACHE_SIZE = 64
_pack_patterns: "OrderedDict[str, KindPatterns]" = OrderedDict()

# Visibility fields of a Memory read request (MEM-ADR-019/021).
VISIBILITY_FIELDS = ("allowedNamespaces", "allowedScopes")


@dataclass
class GraphScope:
    """Where a graph read goes and what the caller may see there.

    ``namespace`` is the primary (tenant) namespace, ``namespaces`` the full
    read set (the workspace namespace of the focus, when the caller may read
    it) and ``visibility`` the ``allowedNamespaces``/``allowedScopes`` the core
    computed — the same values ``POST /context`` sends.
    """

    namespace: str
    namespaces: list[str]
    visibility: dict[str, Any] = field(default_factory=dict)


def graph_scope_of(memory_call: dict[str, Any]) -> GraphScope:
    """The graph scope of a ``POST /context`` memory call, visibility included."""
    request = memory_call["request"]
    return GraphScope(
        namespace=memory_call["namespace"],
        namespaces=list(memory_call.get("namespaces") or [memory_call["namespace"]]),
        visibility={k: request[k] for k in VISIBILITY_FIELDS if k in request},
    )


def workspace_read(
    settings: Settings,
    ctx: AuthContext,
    visible: tuple[list[str], list[str]] | None,
    root_workspace_id: uuid.UUID,
    workspace_scopes: list[str],
    principal_scopes: list[str],
) -> tuple[str | None, list[str] | None]:
    """Whether the workspace namespace is read, and the local-mode narrowing.

    CP-ADR-0059 p.3: in policy mode the PDP decides (a namespace outside the
    visible set is not asked for); in local mode the namespace is read and the
    whole request is narrowed to the focus workspace, its ancestors and the
    principal. Returns ``(namespace or None, allowedScopes or None)``.
    """
    ws_namespace = workspace_namespace(settings, ctx.tenant_id, root_workspace_id)
    if visible is None:
        return ws_namespace, list(dict.fromkeys([*workspace_scopes, *principal_scopes]))
    if ws_namespace in visible[0]:
        return ws_namespace, None
    return None, None


def base_scope(settings: Settings, ctx: AuthContext) -> GraphScope:
    namespace = tenant_namespace(settings, ctx.tenant_id)
    return GraphScope(namespace=namespace, namespaces=[namespace])


@dataclass
class KindPatterns:
    """``idPatterns`` of the kinds of a catalog, and the synonyms of kind names.

    ``aliases`` maps a ``kindAliases`` synonym to its canonical kind, so a
    profile naming ``route`` extracts with the patterns of ``endpoint``.
    """

    patterns: dict[str, tuple[regex.Pattern[str], ...]] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)


def _compile(patterns: Any) -> tuple[regex.Pattern[str], ...]:
    out: list[regex.Pattern[str]] = []
    for text in patterns if isinstance(patterns, list) else []:
        if not isinstance(text, str):
            continue
        try:
            out.append(regex.compile(text))
        except regex.error:
            # Memory validated the pattern at registration; one it accepts and
            # this Python does not is skipped rather than failing the pack.
            logger.warning("skipping an idPattern core cannot compile: %r", text[:100])
    return tuple(out)


async def _pack(provider: GraphProvider, ref: str, trace_run_id: str | None) -> KindPatterns:
    cached = _pack_patterns.get(ref)
    if cached is not None:
        _pack_patterns.move_to_end(ref)
        return cached
    name, _, version = ref.partition("@")
    payload = await provider.get_package(name=name, version=version, trace_run_id=trace_run_id)
    kinds = KindPatterns()
    for spec in payload.get("kinds") or []:
        if isinstance(spec, dict) and isinstance(spec.get("kind"), str):
            kinds.patterns[spec["kind"]] = _compile(spec.get("idPatterns"))
            for alias in spec.get("kindAliases") or []:
                if isinstance(alias, str):
                    kinds.aliases.setdefault(alias, spec["kind"])
    if version:
        _pack_patterns[ref] = kinds
        while len(_pack_patterns) > _PACK_CACHE_SIZE:
            _pack_patterns.popitem(last=False)
    return kinds


async def kind_patterns(
    provider: GraphProvider,
    namespaces: list[str],
    *,
    trace_run_id: str | None = None,
    warnings: list[str] | None = None,
) -> KindPatterns:
    """``idPatterns`` of every kind enabled in ``namespaces``, by kind.

    Precedence is Memory's: the first pack (and the first namespace) that
    declares a kind (or a synonym) wins. A namespace whose catalog cannot be
    read contributes nothing and is reported in ``warnings`` — values of
    fields still anchor.
    """
    result = KindPatterns()
    for namespace in namespaces:
        try:
            body = await provider.namespace_kinds(namespace=namespace, trace_run_id=trace_run_id)
            refs = (body.get("catalog") or {}).get("packages") or []
            for ref in refs:
                if not isinstance(ref, str) or not ref:
                    continue
                pack = await _pack(provider, ref, trace_run_id)
                for kind, patterns in pack.patterns.items():
                    result.patterns.setdefault(kind, patterns)
                for alias, kind in pack.aliases.items():
                    result.aliases.setdefault(alias, kind)
        except ContextProviderError as exc:
            logger.info("kind catalog of %s unavailable: %s", namespace, exc)
            if warnings is not None:
                warnings.append(f"kind catalog unavailable for a namespace ({exc.status})")
    result.patterns = {kind: patterns for kind, patterns in result.patterns.items() if patterns}
    return result


def deadline_after(settings: Settings) -> float:
    """One deadline for a whole graph read, as for ``POST /context`` (CP-ADR-0059 p.4)."""
    return time.monotonic() + settings.context_timeout_seconds + 1.0


async def within(deadline: float, call: Any) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return await asyncio.wait_for(call, timeout=remaining)


async def off_loop[T](deadline: float, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run pack patterns over tenant text in a worker thread, within the deadline.

    ``regex`` releases the GIL while matching and stops on its own timeout
    (``EXTRACTION_SECONDS``), so a backtracking pattern neither blocks the
    event loop nor keeps the thread past the request.
    """
    result: T = await within(
        deadline, anyio.to_thread.run_sync(partial(fn, *args, **kwargs), abandon_on_cancel=True)
    )
    return result


async def typed(
    provider: GraphProvider,
    scope: GraphScope,
    request: dict[str, Any],
    *,
    deadline: float,
    trace_run_id: str | None = None,
) -> dict[str, Any]:
    """One typed traversal with the caller's visibility."""
    pack: dict[str, Any] = await within(
        deadline,
        provider.typed_context(
            namespace=scope.namespace,
            namespaces=scope.namespaces,
            request={**request, **scope.visibility},
            trace_run_id=trace_run_id,
        ),
    )
    return pack


# One rendered line of the prompt is at most this long (control_plane_agent's
# MAX_ITEM_CHARS); the estimate below measures an item the way it is rendered.
_ITEM_CHARS = 600
_ITEM_FIELDS = ("natural_key", "title", "subject", "relation", "object", "source_path")


def _item_chars(item: Any) -> int:
    if not isinstance(item, dict):
        return 0
    size = sum(len(str(item[k])) for k in _ITEM_FIELDS if item.get(k))
    attributes = item.get("attributes")
    if isinstance(attributes, dict):
        size += len(str(attributes))
    return min(size + 16, _ITEM_CHARS)


def within_budget(pack: dict[str, Any], budget_tokens: int) -> dict[str, Any]:
    """The pack cut to ``budget_tokens``, as the API returns it.

    Entities in section order, then relations, while their rendered size fits
    ``budget_tokens * CHARS_PER_TOKEN``; the rest is counted in ``omitted``.
    ``used`` and ``unresolved`` stay whole: they describe the compilation, and
    the record and ``:replay`` compare the whole of it.
    """
    room = budget_tokens * CHARS_PER_TOKEN
    omitted = {"entities": 0, "facts": 0}

    def fits(item: Any, counter: str) -> bool:
        nonlocal room
        size = _item_chars(item)
        if size > room:
            room = 0
            omitted[counter] += 1
            return False
        room -= size
        return True

    sections = []
    for section in pack.get("sections") or []:
        if not isinstance(section, dict):
            continue
        items = [i for i in section.get("items") or [] if fits(i, "entities")]
        if items:
            sections.append({**section, "items": items})
    facts = [f for f in pack.get("facts") or [] if fits(f, "facts")]
    out = {**pack, "sections": sections, "facts": facts}
    if any(omitted.values()):
        out["omitted"] = omitted
    return out


def entities_of(pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Entities of a typed pack, in section order."""
    return [
        item
        for section in pack.get("sections") or []
        if isinstance(section, dict)
        for item in section.get("items") or []
        if isinstance(item, dict)
    ]


def used_of(pack: dict[str, Any]) -> dict[str, Any]:
    """The ``used`` block of a typed pack: what a reproduction must match."""
    raw = pack.get("used")
    used: dict[str, Any] = raw if isinstance(raw, dict) else {}
    return {
        "entities": [e for e in used.get("entities") or [] if isinstance(e, dict)],
        "facts": [str(f) for f in used.get("facts") or []],
        "snapshots": [s for s in used.get("snapshots") or [] if isinstance(s, dict)],
    }


def drift(recorded: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """How a re-compiled pack differs from the recorded one."""

    def entity_keys(used: dict[str, Any]) -> set[tuple[str, str]]:
        return {
            (str(e.get("namespace", "")), str(e.get("natural_key", "")))
            for e in used.get("entities") or []
        }

    before, after = entity_keys(recorded), entity_keys(current)
    facts_before = set(recorded.get("facts") or [])
    facts_after = set(current.get("facts") or [])

    def as_entities(keys: set[tuple[str, str]]) -> list[dict[str, str]]:
        return [{"namespace": n, "natural_key": k} for n, k in sorted(keys)]

    return {
        "missingEntities": as_entities(before - after),
        "extraEntities": as_entities(after - before),
        "missingFacts": sorted(facts_before - facts_after),
        "extraFacts": sorted(facts_after - facts_before),
    }
