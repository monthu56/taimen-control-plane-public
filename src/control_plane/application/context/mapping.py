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
"""

from typing import Any

from control_plane.infrastructure.db.models import Event, EventArchive

# Archived events carry the same columns, so one mapping serves both tables.
JournalEvent = Event | EventArchive

MAPPING_VERSION = 5

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


def map_event(event: JournalEvent) -> dict[str, Any] | None:
    """Translate one journal event, or return None for noise."""
    allowed = _RETAINED.get(event.event_type)
    if allowed is None:
        return None
    payload = event.payload or {}

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

    observation: dict[str, Any] = {
        "source": {
            "system": SOURCE_SYSTEM,
            "stream": SOURCE_STREAM,
            "external_id": f"event:{event.id}",
        },
        "kind": kind,
        "occurred_at": occurred_at,
        "subject": {"type": event.entity_type, "id": str(event.entity_id)},
        "scopes": _scopes(event, payload),
        "content": _content(event, payload),
        "data": data,
        "provenance": {"uri": f"control-plane://events/{event.id}"},
    }
    if event.event_type == "observation.recorded" and payload.get("assertions"):
        observation["assertions"] = payload["assertions"]
    if event.actor_id is not None:
        observation["actor"] = {"type": "principal", "id": str(event.actor_id)}
    return observation
