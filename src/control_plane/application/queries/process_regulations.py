"""The regulations a process names, resolved through memory (CP-ADR-0076 §7).

``governedBy`` names documents of the knowledge base by natural key. Publishing
a version does not ask memory (the publish transaction never waits for it, and
memory being down must not stop a package); the check of a package in the core
does: :func:`governed_by_problems` resolves every named document with one
typed read per :data:`MAX_ANCHORS` documents — anchors only, nothing traversed,
nothing by similarity — and warns ``governed_by_unknown_document`` at each
reference memory does not resolve exactly. Memory that is not configured or
does not answer is one ``governed_by_unchecked`` warning, never a refusal.

The read goes where a ``recall`` of the process would: the tenant namespace and
the namespace of the tree of the process's workspace, with the visibility of
the caller (:func:`graph_scope`). Split like every memory call: the scope in a
transaction, the read outside it.
"""

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from control_plane.application.authorization import AuthContext
from control_plane.application.context.graph import (
    GraphScope,
    deadline_after,
    entities_of,
    typed,
)
from control_plane.application.queries.recall import graph_scope
from control_plane.config import Settings
from control_plane.domain.context_schema import MAX_ANCHORS
from control_plane.domain.process_definition import (
    GOVERNED_BY_UNCHECKED,
    Problem,
    governed_references,
    unknown_document_problems,
)
from control_plane.infrastructure.context_provider import ContextProviderError, GraphProvider

logger = logging.getLogger(__name__)

# How memory resolved an anchor (``matchedBy``): these name the document itself;
# a normalized or suffix match, or one by similarity, is another node.
EXACT_MATCHES = frozenset({"natural_key", "alias", "id_pattern"})


async def regulation_scope(
    session: AsyncSession, ctx: AuthContext, settings: Settings, spec: dict[str, Any]
) -> GraphScope:
    """Where the documents of ``spec`` are looked up (transactional half)."""
    raw = spec.get("workspaceId")
    workspace_id = uuid.UUID(str(raw)) if raw else None
    return await graph_scope(session, ctx, settings, workspace_id)


def _known(pack: dict[str, Any]) -> set[str]:
    known: set[str] = set()
    for anchor in pack.get("anchors") or []:
        if not isinstance(anchor, dict) or not anchor.get("resolved"):
            continue
        matched = anchor.get("matchedBy")
        if matched is not None and matched not in EXACT_MATCHES:
            continue
        value = (anchor.get("input") or {}).get("value")
        if isinstance(value, str):
            known.add(value)
    return known


async def unknown_documents(
    provider: GraphProvider,
    scope: GraphScope,
    documents: list[str],
    settings: Settings,
    *,
    trace_run_id: str | None = None,
) -> list[str]:
    """The documents memory does not resolve exactly, in the order given.

    Raises ``TimeoutError`` or ``ContextProviderError`` when memory does not
    answer; the caller decides what that means.
    """
    wanted = list(dict.fromkeys(documents))
    deadline = deadline_after(settings)
    known: set[str] = set()
    for start in range(0, len(wanted), MAX_ANCHORS):
        batch = wanted[start : start + MAX_ANCHORS]
        request = {
            "anchors": [{"value": document} for document in batch],
            "traverse": [],
            "allow_semantic": False,
        }
        pack = await typed(provider, scope, request, deadline=deadline, trace_run_id=trace_run_id)
        known |= _known(pack)
    return [document for document in wanted if document not in known]


async def governed_by_problems(
    provider: GraphProvider | None,
    scope: GraphScope,
    spec: dict[str, Any],
    settings: Settings,
    *,
    trace_run_id: str | None = None,
) -> list[Problem]:
    """Warnings for the ``governedBy`` references of ``spec`` memory cannot resolve."""
    documents = [document for document, _ in governed_references(spec)]
    if not documents:
        return []
    reason: str
    if provider is None:
        reason = "memory is not configured"
    else:
        try:
            unknown = await unknown_documents(
                provider, scope, documents, settings, trace_run_id=trace_run_id
            )
        except TimeoutError:
            reason = "memory did not answer in time"
        except ContextProviderError as exc:
            logger.warning("governedBy check: memory failed: %s", exc)
            reason = "memory failed to answer"
        else:
            return unknown_document_problems(spec, unknown)
    return [
        Problem(
            GOVERNED_BY_UNCHECKED,
            "warning",
            "/spec",
            f"the regulations the process names were not checked: {reason}",
            hint="check the package again when memory is available",
        )
    ]


# --- coverage of regulations (CP-ADR-0074 §11, FR-058) -------------------------------------

# The relation from a section of a document to the document: the sections of a
# regulation are the nodes that point at it with this relation.
SECTION_RELATION = "section_of"
# Sections read per document: the limit of one traversal step of memory.
MAX_SECTIONS = 200
_SECTION_SEPARATORS = ("#", "/", ":")
UNKNOWN_SECTION = "governed_by_unknown_section"


def section_name(document: str, entity: dict[str, Any]) -> str:
    """How ``governedBy[].section`` names a section node of ``document``.

    ``attributes.section`` of the node; else its natural key without the
    document's key and one separator (``doc#4.2`` → ``4.2``); else the key.
    """
    attributes = entity.get("attributes")
    named = attributes.get("section") if isinstance(attributes, dict) else None
    if isinstance(named, str) and named.strip():
        return named
    key = str(entity.get("natural_key") or "")
    for separator in _SECTION_SEPARATORS:
        if key.startswith(document + separator) and len(key) > len(document) + 1:
            return key[len(document) + 1 :]
    return key


async def document_sections(
    provider: GraphProvider,
    scope: GraphScope,
    documents: list[str],
    settings: Settings,
    *,
    trace_run_id: str | None = None,
) -> dict[str, list[str] | None]:
    """The sections memory holds of each document; ``None`` — memory has no such document.

    One typed read per document: the anchor and the nodes that point at it
    with :data:`SECTION_RELATION`, nothing by similarity. Raises
    ``TimeoutError`` or ``ContextProviderError`` when memory does not answer.
    """
    deadline = deadline_after(settings)
    found: dict[str, list[str] | None] = {}
    for document in dict.fromkeys(documents):
        request = {
            "anchors": [{"value": document}],
            "traverse": [
                {
                    "relation": SECTION_RELATION,
                    "direction": "in",
                    "depth": 1,
                    "limit": MAX_SECTIONS,
                }
            ],
            "allow_semantic": False,
        }
        pack = await typed(provider, scope, request, deadline=deadline, trace_run_id=trace_run_id)
        if document not in _known(pack):
            found[document] = None
            continue
        anchors = {
            str(item.get("natural_key"))
            for anchor in pack.get("anchors") or []
            if isinstance(anchor, dict)
            for item in anchor.get("resolved") or []
            if isinstance(item, dict)
        }
        members = {
            str(fact.get("subject"))
            for fact in pack.get("facts") or []
            if isinstance(fact, dict)
            and fact.get("relation") == SECTION_RELATION
            and str(fact.get("object")) in anchors
        }
        names = [
            section_name(document, entity)
            for entity in entities_of(pack)
            if str(entity.get("natural_key")) in members
        ]
        found[document] = sorted(dict.fromkeys(names))
    return found
