"""Domain event → Context Memory ``Observation`` translation (v0.4).

An explicit whitelist boundary: nothing crosses into durable memory unless a
rule here says so, and each rule names the payload fields it forwards —
events are never serialized blindly (data minimization; no secrets, no
internal payloads, no hidden reasoning).

Observation identity is stable and delivery-independent:
``(system=control-plane, stream=domain-events, external_id=event:<uuid>)`` —
the Memory Service deduplicates re-deliveries on it (at-least-once safe).

``MAPPING_VERSION`` is recorded in every observation's ``data`` so a future
rebuild can tell which translation semantics produced a fact.

Processes (CP-ADR-0076 §2-§3, mapping v6): the events that carry a case
projection or a published version become observations with assertions — the
nodes and edges of the projection. The edges of one stream (a case, or the
versions of a process) have a validity: an edge takes its ``valid_from``
from the event it first appeared in, and the event it disappears in closes
it (``valid_to``). The adapter hands the earlier events of the stream in;
the translation stays a pure function of the journal, so a re-delivery says
exactly what the first delivery said.
"""

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from control_plane.infrastructure.db.models import Event, EventArchive

# Archived events carry the same columns, so one mapping serves both tables.
JournalEvent = Event | EventArchive

MAPPING_VERSION = 6

SOURCE_SYSTEM = "control-plane"
SOURCE_STREAM = "domain-events"

# Events worth remembering, with the payload fields each may forward.
# Everything else (session lifecycle, claim churn, heartbeats, checkpoints,
# run actions, api keys) is operational noise for durable memory.
# KEYS MUST MATCH THE ACTUAL EMITTERS in application/commands/* — the unit
# suite cross-checks a representative sample so whitelist/payload drift is
# caught instead of silently emptying observations.
_RETAINED: dict[str, tuple[str, ...]] = {
    "tenant.bootstrapped": ("slug",),  # bootstrap.py (key ids/prefixes NOT forwarded)
    "principal.created": ("kind", "displayName"),  # principals.py
    # goalId / origin since mapping v5 (CP-ADR-0062): origin is a summary of
    # references (kind, ref, ruleId, evidence ids) — never evidence notes.
    "task.created": (
        "publicId",
        "title",
        "status",
        "priority",
        "workspaceId",
        "goalId",
        "origin",
    ),  # tasks.py
    "task.updated": ("publicId", "changes", "version"),  # tasks.py
    "task.claimed": ("publicId", "claimId", "sessionId", "holderId", "intent"),  # claims.py
    "task.completed": ("publicId", "version"),  # tasks.py
    "task.relation_added": ("relationId", "toTaskId", "type"),  # relations.py
    "task.relation_removed": ("relationId", "fromTaskId", "toTaskId", "type"),
    "run.started": ("taskId", "claimId", "attempt"),  # runs.py
    "run.succeeded": ("taskId", "attempt", "taskCompleted"),
    "run.failed": ("taskId", "attempt", "reason"),
    "run.cancelled": ("taskId", "attempt", "reason"),
    "run.suspended": ("taskId", "attempt", "reason", "waitingForApprovalId"),
    "artifact.created": ("taskId", "runId", "type", "name", "uri", "supersedesArtifactId"),
    "approval.requested": (  # approvals.py
        "taskId",
        "artifactId",
        "requiredRoleId",
        "assignedPrincipalId",
        "gate",
        "comment",
    ),
    "approval.approved": ("taskId", "artifactId", "comment"),
    "approval.rejected": ("taskId", "artifactId", "comment"),
    "approval.cancelled": ("taskId",),
    # M1.1 work graph (CP-ADR-0062). The desired state is prose and criteria
    # are tenant documents: only identity, status and the origin summary.
    "goal.created": (
        "title",
        "status",
        "workspaceId",
        "ownerId",
        "parentGoalId",
        "criteriaCount",
        "createdFrom",
    ),  # goals.py
    "goal.updated": ("changes", "fromStatus", "status", "version"),
    "workspace.created": ("slug", "name", "parentId"),  # workspaces.py
    "workspace.updated": ("changes", "version"),
    "workspace.moved": ("fromParentId", "toParentId"),
    "workspace.archived": ("slug",),
    "workspace.member_added": ("principalId",),
    "workspace.member_removed": ("principalId",),
    "role.created": ("slug", "name", "workspaceId"),  # org.py
    "role.updated": ("changes", "version"),
    "role.assigned": ("roleId", "workspaceId"),
    "role.revoked": ("roleId", "workspaceId"),
    "capability.created": ("name",),
    "capability.assigned": ("capabilityId",),
    "capability.revoked": ("capabilityId",),
    "skill.registered": ("name", "version", "protocol"),
    "skill.updated": ("changedFields", "rowVersion"),
    "skill.assigned": ("skillId",),
    "skill.revoked": ("skillId",),
    "delegation.created": ("humanPrincipalId", "agentPrincipalId", "permissions"),
    "delegation.revoked": (),
    # Explicit remember: the payload IS the observation (see observations.py).
    "observation.recorded": ("kind", "content", "data", "taskId", "runId", "workspaceId"),
    # plus _OBSERVATION_ORIGIN below (CP-ADR-0057).
    # v0.5 project model. Deliberately NARROW: identity and lifecycle only.
    # custom_fields and the config document are NEVER forwarded — they are
    # tenant-authored free-form JSON, and durable memory is not the place for
    # it (ADR-0032 keeps config auditable in the Control Plane instead).
    "project.created": (
        "workspaceId",
        "parentProjectId",
        "templateKey",
        "templateVersion",
        "statusKey",
        "systemStatusCategory",
    ),  # projects.py
    "project.status_changed": (
        "fromStatusKey",
        "statusKey",
        "systemStatusCategory",
        "comment",
    ),
    "project.archived": ("workspaceId",),
    "project.config_revision_activated": ("revision",),
    "project_template.created": ("key", "version", "displayName"),  # project_templates.py
    "workspace_type.created": ("key", "displayName"),  # workspace_types.py
    # Processes (CP-ADR-0076): identity only; what the case says is in the
    # assertions of its projection, never the instance data.
    "process.definition_published": (  # process_definitions.py
        "key",
        "version",
        "definitionHash",
        "previousVersion",
        "workspaceId",
        "displayName",
    ),
    "process.started": ("instanceId", "definitionKey", "version", "workspaceId"),  # engine
    "process.data_changed": ("instanceId", "definitionKey", "version", "workspaceId"),
    "process.completed": ("instanceId", "definitionKey", "version", "workspaceId", "outcome"),
}

# External-observation fields forwarded into ``data`` (CP-ADR-0057). They are
# server-validated, so they win over same-named keys of the client's data.
_OBSERVATION_ORIGIN = ("source", "dedupKey", "observedAt", "supersedes", "externalRef")

# Payload keys that contribute retrieval scopes when present.
_SCOPE_KEYS = {
    "taskId": "task",
    "runId": "run",
    "workspaceId": "workspace",
    "parentProjectId": "project",
    "fromTaskId": "task",
    "toTaskId": "task",
    "principalId": "principal",
    "artifactId": "artifact",
    "goalId": "goal",
    "parentGoalId": "goal",
}


def is_memory_worthy(event_type: str) -> bool:
    return event_type in _RETAINED


def _scopes(event: JournalEvent, payload: dict[str, Any]) -> list[dict[str, str]]:
    scopes: dict[tuple[str, str], dict[str, str]] = {}

    def add(scope_type: str, scope_id: object) -> None:
        if scope_id in (None, ""):
            return
        key = (scope_type, str(scope_id))
        scopes.setdefault(key, {"type": scope_type, "id": str(scope_id)})

    add(event.entity_type, event.entity_id)
    for payload_key, scope_type in _SCOPE_KEYS.items():
        add(scope_type, payload.get(payload_key))
    if event.actor_id is not None:
        add("principal", event.actor_id)
    return list(scopes.values())[:20]  # Memory contract: max 20 per observation


def _content(event: JournalEvent, payload: dict[str, Any]) -> str:
    """Short human-readable summary — the lexical retrieval surface."""
    if event.event_type == "observation.recorded":
        return str(payload.get("content", ""))
    parts = [event.event_type]
    label = (
        payload.get("title")
        or payload.get("name")
        or payload.get("displayName")
        or payload.get("publicId")
        or payload.get("slug")
    )
    if label:
        parts.append(str(label))
    detail = payload.get("reason") or payload.get("comment") or payload.get("intent")
    if detail:
        parts.append(str(detail))
    return ": ".join(parts)


def map_event(
    event: JournalEvent,
    *,
    earlier: Sequence[JournalEvent] = (),
    document: "CaseDocument | None" = None,
) -> dict[str, Any] | None:
    """Translate one journal event, or return None for noise.

    ``earlier`` — the events of the same projection stream before this one,
    in journal order (:func:`projection_stream`); ``document`` — the case an
    ``artifact.created`` is a document of (CP-ADR-0076 §2). A projection
    larger than one observation holds continues in :func:`map_event_parts`.
    """
    parts = map_event_parts(event, earlier=earlier, document=document)
    return parts[0] if parts else None


def map_event_parts(
    event: JournalEvent,
    *,
    earlier: Sequence[JournalEvent] = (),
    document: "CaseDocument | None" = None,
) -> list[dict[str, Any]]:
    """All observations of one event: one, unless its assertions need more.

    Memory takes at most 200 assertions per observation. The first part is
    the event's observation; the others are ``event:<uuid>/<n>`` and carry
    only the rest of the assertions — each part is idempotent on its own.
    """
    observation = _map(event, earlier, document)
    if observation is None:
        return []
    assertions = observation.get("assertions") or []
    if len(assertions) <= _ASSERTION_LIMIT:
        return [observation]
    parts = []
    for number, start in enumerate(range(0, len(assertions), _ASSERTION_LIMIT), start=1):
        part = {**observation, "assertions": assertions[start : start + _ASSERTION_LIMIT]}
        if number > 1:
            part["source"] = {
                **observation["source"],
                "external_id": f"{observation['source']['external_id']}/{number}",
            }
            part["data"] = {**observation["data"], "part": number}
        parts.append(part)
    return parts


def _map(
    event: JournalEvent, earlier: Sequence[JournalEvent], document: "CaseDocument | None"
) -> dict[str, Any] | None:
    allowed = _RETAINED.get(event.event_type)
    if allowed is None:
        return None
    payload = event.payload or {}
    projected = _project(event, earlier, document)
    if projected is None and event.event_type in PROJECTION_EVENTS:
        return None  # a process without a memory section says nothing to memory

    if event.event_type == "observation.recorded":
        # Explicit remember: forward the recorded kind verbatim.
        kind = str(payload.get("kind") or "note")
        data = dict(payload.get("data") or {})
        data.update({key: payload[key] for key in _OBSERVATION_ORIGIN if key in payload})
    else:
        kind = event.event_type
        data = {key: payload[key] for key in allowed if key in payload}
    data["mappingVersion"] = MAPPING_VERSION
    data["eventSequence"] = event.sequence
    if event.trace_run_id:
        # The distributed trace of the request that caused this fact
        # (ADR-0039). Batches mix traces, so it belongs per observation.
        data["traceRunId"] = event.trace_run_id

    # The fact happened when it was seen in the external system, not when it
    # reached the journal; events before CP-ADR-0057 carry no observedAt.
    occurred_at = event.occurred_at.isoformat()
    if event.event_type == "observation.recorded" and payload.get("observedAt"):
        occurred_at = str(payload["observedAt"])

    scopes = _scopes(event, payload)
    content = _content(event, payload)
    if projected is not None:
        data.update(projected.data)
        if projected.workspace_id and len(scopes) < 20:
            scope = {"type": "workspace", "id": projected.workspace_id}
            if scope not in scopes:
                scopes.append(scope)
        if projected.label:
            content = f"{event.event_type}: {projected.label}"

    observation: dict[str, Any] = {
        "source": {
            "system": SOURCE_SYSTEM,
            "stream": SOURCE_STREAM,
            "external_id": f"event:{event.id}",
        },
        "kind": kind,
        "occurred_at": occurred_at,
        "subject": {"type": event.entity_type, "id": str(event.entity_id)},
        "scopes": scopes,
        "content": content,
        "data": data,
        "provenance": {"uri": f"control-plane://events/{event.id}"},
    }
    if event.event_type == "observation.recorded" and payload.get("assertions"):
        observation["assertions"] = payload["assertions"]
    if projected is not None and projected.assertions:
        observation["assertions"] = projected.assertions
    if event.actor_id is not None:
        observation["actor"] = {"type": "principal", "id": str(event.actor_id)}
    return observation


# --- processes (CP-ADR-0076 §2-§3) --------------------------------------------------
#
# Kinds and relations of the projection. They are the names of the ontology
# package process-knowledge (TAI-ADR-0054 §9); the core writes them and does
# not check them against a catalog.

PROJECTION_EVENTS = ("process.started", "process.data_changed", "process.completed")
DEFINITION_EVENT = "process.definition_published"

KIND_PROCESS = "process"
KIND_STAGE = "process_stage"
KIND_STEP = "process_step"
KIND_CASE_FACT = "case_fact"
KIND_DOCUMENT = "document"
REL_INSTANCE_OF = "instance_of"
REL_INVOLVES = "involves"  # an entity of the case without its own rel
REL_HAS_FACT = "has_fact"
REL_HAS_DOCUMENT = "has_document"
REL_STAGE_OF = "stage_of"  # stage -> process
REL_STEP_OF = "step_of"  # step, milestone, decision table -> stage or process
REL_REGULATES = "regulates"  # regulation document -> element of the process

# The shapes Memory takes a natural key and a relation in (application/context/assertions.py).
_KEY = re.compile(r"^[a-z][a-z0-9._-]*:[^\s]+$")
_RELATION = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")
_KEY_LIMIT = 256
_TITLE_LIMIT = 2000
_ASSERTION_LIMIT = 200  # per observation, as Memory accepts them

Edge = tuple[str, str, str]  # (subject, relation, object)


@dataclass(frozen=True)
class CaseDocument:
    """An artifact of a case that went to memory as a document (CP-ADR-0076 §2)."""

    key: str  # natural key of the document node
    case: str  # natural key of the case node
    workspace_id: str | None


@dataclass(frozen=True)
class _Projected:
    assertions: list[dict[str, Any]]
    data: dict[str, Any]
    label: str | None
    workspace_id: str | None


@dataclass(frozen=True)
class _Snapshot:
    """What one event says the graph of its stream is now."""

    nodes: list[dict[str, Any]]
    edges: list[Edge]


def node_key(kind: str, key: str) -> str:
    """``<kind>:<key>`` — the key memory resolves an anchor ``{kind, key}`` by."""
    return f"{kind}:{key}"


def process_key(definition_key: str, element: str | None = None) -> str:
    """``process:<key>`` and ``process:<key>/<element id>`` (CP-ADR-0076 §3)."""
    base = node_key(KIND_PROCESS, definition_key)
    return base if element is None else f"{base}/{element}"


def projection_stream(event: JournalEvent) -> tuple[str, str] | None:
    """The stream whose edges the event states: a case, or a process's versions."""
    if event.event_type in PROJECTION_EVENTS:
        return ("case", str(event.entity_id))
    if event.event_type == DEFINITION_EVENT:
        key = (event.payload or {}).get("key")
        return ("process", str(key)) if key else None
    return None


def case_of(event: JournalEvent) -> tuple[str, str] | None:
    """(kind, natural key) of the case node an event's projection names."""
    memory = (event.payload or {}).get("memory")
    if not isinstance(memory, dict):
        return None
    case = memory.get("case") or {}
    if not isinstance(case, dict) or case.get("key") in (None, ""):
        return None
    kind = str(case.get("kind") or "case")
    key = node_key(kind, str(case["key"]))
    return (kind, key) if _valid_key(key) else None


def _valid_key(key: str) -> bool:
    return len(key) <= _KEY_LIMIT and _KEY.match(key) is not None


def _valid_edge(edge: Edge) -> bool:
    subject, relation, obj = edge
    return _valid_key(subject) and _valid_key(obj) and _RELATION.match(relation) is not None


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _entity(key: str, kind: str, title: str | None, properties: dict[str, Any]) -> dict[str, Any]:
    entity: dict[str, Any] = {"key": key, "type": kind}
    if title:
        entity["title"] = title[:_TITLE_LIMIT]
    if properties:
        entity["properties"] = properties
    return {"assert": "entity", "entity": entity}


def _case_snapshot(payload: dict[str, Any], case: tuple[str, str] | None) -> _Snapshot:
    """The case node, its facts and entities, and the process it is an instance of."""
    if case is None:
        return _Snapshot([], [])
    kind, key = case
    memory = payload["memory"]
    facts = {
        str(name): value for name, value in (memory.get("facts") or {}).items() if value is not None
    }
    title = (memory.get("case") or {}).get("title")
    nodes = [_entity(key, kind, _text(title) if title is not None else None, facts)]
    edges: list[Edge] = []
    if payload.get("definitionKey"):
        edges.append((key, REL_INSTANCE_OF, process_key(str(payload["definitionKey"]))))
    # A fact is an edge to a node of its value: a new value is a new edge,
    # and the edge of the old value closes (MEM-ADR-016).
    for name, value in facts.items():
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(f"{name}\x00{encoded}".encode()).hexdigest()[:16]
        fact = node_key(KIND_CASE_FACT, f"{key}/{digest}")
        if not _valid_key(fact):
            continue
        nodes.append(
            _entity(fact, KIND_CASE_FACT, f"{name}: {_text(value)}", {"name": name, "value": value})
        )
        edges.append((key, REL_HAS_FACT, fact))
    for entity in memory.get("entities") or ():
        if not isinstance(entity, dict) or not entity.get("kind"):
            continue
        if entity.get("key") in (None, ""):
            continue
        target = node_key(str(entity["kind"]), str(entity["key"]))
        if not _valid_key(target):
            continue
        if entity.get("name") is not None:
            nodes.append(_entity(target, str(entity["kind"]), _text(entity["name"]), {}))
        edges.append((key, str(entity.get("rel") or REL_INVOLVES), target))
    return _Snapshot(nodes, edges)


def _version_snapshot(payload: dict[str, Any]) -> _Snapshot:
    """The process, its stages and steps, and the regulations of each (CP-ADR-0076 §3)."""
    definition = str(payload.get("key") or "")
    process = process_key(definition)
    if not _valid_key(process):
        return _Snapshot([], [])
    nodes = [
        _entity(
            process,
            KIND_PROCESS,
            payload.get("displayName") or definition,
            {
                "version": payload.get("version"),
                "definitionHash": payload.get("definitionHash"),
                "regulatedBy": list(payload.get("governedBy") or ()),
            },
        )
    ]
    edges: list[Edge] = [
        (str(item["document"]), REL_REGULATES, process)
        for item in payload.get("governedBy") or ()
        if isinstance(item, dict) and item.get("document")
    ]
    elements = {
        str(e["id"]): e
        for e in payload.get("elements") or ()
        if isinstance(e, dict) and e.get("id")
    }

    def holder(element: dict[str, Any]) -> str:
        # The nearest stage around the element, else the process itself.
        seen: set[str] = set()
        parent = element.get("parent")
        while parent is not None and parent in elements and parent not in seen:
            seen.add(parent)
            if elements[parent].get("kind") == "stage":
                return process_key(definition, parent)
            parent = elements[parent].get("parent")
        return process

    for element_id, element in elements.items():
        key = process_key(definition, element_id)
        if not _valid_key(key):
            continue
        stage = element.get("kind") == "stage"
        governed = [dict(i) for i in element.get("governedBy") or () if isinstance(i, dict)]
        nodes.append(
            _entity(
                key,
                KIND_STAGE if stage else KIND_STEP,
                element.get("displayName") or element_id,
                {
                    "process": definition,
                    "element": element_id,
                    "elementKind": element.get("kind"),
                    "version": payload.get("version"),
                    # A section of a regulation is a property: an edge of an
                    # observation carries no attributes.
                    "regulatedBy": governed,
                },
            )
        )
        edges.append((key, REL_STAGE_OF, process) if stage else (key, REL_STEP_OF, holder(element)))
        edges += [(str(i["document"]), REL_REGULATES, key) for i in governed if i.get("document")]
    return _Snapshot(nodes, edges)


def _snapshot(event: JournalEvent) -> _Snapshot:
    payload = event.payload or {}
    if event.event_type == DEFINITION_EVENT:
        return _version_snapshot(payload)
    return _case_snapshot(payload, case_of(event))


def _timeline(
    moments: Iterable[tuple[str, list[Edge]]],
) -> tuple[dict[Edge, str], dict[Edge, str]]:
    """Edges open after the last moment with their start, and those it closed.

    An edge starts at the first moment of an unbroken run of moments that
    state it; the first moment without it closes it.
    """
    state: dict[Edge, str] = {}
    closed: dict[Edge, str] = {}
    for at, edges in moments:
        present = dict.fromkeys(edges)
        closed = {edge: since for edge, since in state.items() if edge not in present}
        state = {edge: state.get(edge, at) for edge in present}
    return state, closed


def _fact(edge: Edge, since: str, until: str | None = None) -> dict[str, Any]:
    subject, relation, obj = edge
    fact: dict[str, Any] = {
        "subject": subject,
        "predicate": relation,
        "object": obj,
        "valid_from": since,
    }
    if until is not None:
        fact["valid_to"] = until
    return {"assert": "fact", "fact": fact}


def _project(
    event: JournalEvent, earlier: Sequence[JournalEvent], document: CaseDocument | None
) -> _Projected | None:
    if event.event_type == "artifact.created":
        if document is None:
            return None
        edge = (document.case, REL_HAS_DOCUMENT, document.key)
        return _Projected(
            [_fact(edge, event.occurred_at.isoformat())] if _valid_edge(edge) else [],
            {"case": document.case, "document": document.key},
            None,
            document.workspace_id,
        )
    if event.event_type not in PROJECTION_EVENTS and event.event_type != DEFINITION_EVENT:
        return None
    payload = event.payload or {}
    now = event.occurred_at.isoformat()
    snapshot = _snapshot(event)
    moments = [(e.occurred_at.isoformat(), _snapshot(e).edges) for e in earlier]
    moments.append((now, snapshot.edges))
    opened, closed = _timeline(moments)
    if not snapshot.nodes and not closed:
        return None
    assertions = [
        *snapshot.nodes,
        *(_fact(edge, since) for edge, since in opened.items() if _valid_edge(edge)),
        *(_fact(edge, since, now) for edge, since in closed.items() if _valid_edge(edge)),
    ]
    data: dict[str, Any] = {"closedEdges": len(closed)}
    label: str | None = None
    if event.event_type == DEFINITION_EVENT:
        label = str(payload.get("displayName") or payload.get("key") or "")
    else:
        case = case_of(event)
        if case is not None:
            data["case"] = {"kind": case[0], "key": case[1]}
            title = ((payload.get("memory") or {}).get("case") or {}).get("title")
            label = _text(title) if title is not None else case[1]
    workspace = payload.get("workspaceId")
    return _Projected(assertions, data, label, str(workspace) if workspace else None)
