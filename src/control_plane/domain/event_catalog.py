"""Catalog of the event types the core writes to its journal (CP-ADR-0068).

Every event carries an envelope (``id``, ``type``, ``sequence``, ``entityType``,
``entityId``, ``workspaceId``, ``occurredAt``, ``actorId``, ``schemaVersion``,
``payload``...) and a ``payload`` whose shape is declared here: event type ->
version -> JSON Schema of the payload. The writer stamps the current version of
the type onto the event (``schema_version``), so a consumer reading an old event
knows which schema it follows.

Evolution rule: a version only ADDS payload fields. A consumer written against
version N reads version N+1 unchanged; a field that changes its meaning or goes
away is a new event type, not a new version. Older versions stay in the catalog
for as long as the journal may hold events written under them.

The catalog is neutral (constitution, art. II): it names the core's own
entities — tasks, runs, approvals, workspaces — never a domain of a package.

``python -m control_plane.domain.event_catalog <dir>`` renders the catalog into
``<dir>/catalog.md`` and ``<dir>/catalog.json`` (``make event-catalog`` writes
``docs/events/``); a test keeps the committed files in step with this module.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

JsonSchema = dict[str, Any]

# --- schema vocabulary -------------------------------------------------------

ANY: JsonSchema = {}
STR: JsonSchema = {"type": "string"}
INT: JsonSchema = {"type": "integer"}
BOOL: JsonSchema = {"type": "boolean"}
OBJ: JsonSchema = {"type": "object"}
ARR: JsonSchema = {"type": "array"}
UUID: JsonSchema = {"type": "string", "format": "uuid"}
TIME: JsonSchema = {"type": "string", "format": "date-time"}


def nullable(schema: JsonSchema) -> JsonSchema:
    """``schema`` or ``null``."""
    kind = schema.get("type")
    if kind is None:
        return schema
    return {**schema, "type": [kind, "null"]}


STR_N = nullable(STR)
UUID_N = nullable(UUID)
INT_N = nullable(INT)


def described(schema: JsonSchema, description: str) -> JsonSchema:
    return {**schema, "description": description}


def data(
    required: Mapping[str, JsonSchema] | None = None,
    optional: Mapping[str, JsonSchema] | None = None,
) -> JsonSchema:
    """An object schema: ``required`` keys are always present (possibly null),
    ``optional`` ones only on some paths. Unknown keys are allowed — a newer
    version may add them, and a consumer must ignore what it does not know."""
    properties = {**(required or {}), **(optional or {})}
    schema: JsonSchema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties,
        "additionalProperties": True,
    }
    if required:
        schema["required"] = sorted(required)
    return schema


# --- catalog entries ---------------------------------------------------------


@dataclass(frozen=True)
class EventVersion:
    version: int
    schema: JsonSchema
    # What this version added relative to the previous one (empty for v1).
    changes: str = ""


@dataclass(frozen=True)
class EventType:
    type: str
    entity_type: str
    description: str
    versions: tuple[EventVersion, ...] = field(default_factory=tuple)

    @property
    def current(self) -> EventVersion:
        return self.versions[-1]

    def version(self, number: int) -> EventVersion | None:
        return next((v for v in self.versions if v.version == number), None)


class UnknownEventType(ValueError):
    """An event type missing from the catalog: a programming error in the core."""


_TYPES: dict[str, EventType] = {}


def _register(
    event_type: str,
    entity_type: str,
    description: str,
    *versions: JsonSchema | tuple[JsonSchema, str],
) -> None:
    if event_type in _TYPES:
        raise ValueError(f"event type {event_type!r} registered twice")
    built: list[EventVersion] = []
    for number, item in enumerate(versions, start=1):
        schema, changes = item if isinstance(item, tuple) else (item, "")
        built.append(EventVersion(version=number, schema=schema, changes=changes))
    _TYPES[event_type] = EventType(event_type, entity_type, description, tuple(built))


def event_types() -> list[EventType]:
    """Every registered type, ordered by name."""
    return [_TYPES[name] for name in sorted(_TYPES)]


def get_event_type(event_type: str) -> EventType:
    try:
        return _TYPES[event_type]
    except KeyError:
        raise UnknownEventType(
            f"event type {event_type!r} is not in the event catalog"
            " (control_plane/domain/event_catalog.py)"
        ) from None


def current_version(event_type: str) -> int:
    """The version a new event of this type is written with."""
    return get_event_type(event_type).current.version


def schema_for(event_type: str, version: int) -> JsonSchema | None:
    entry = _TYPES.get(event_type)
    found = entry.version(version) if entry else None
    return found.schema if found else None


# Limit of free text copied into an event payload (a decision comment...): the
# event says what happened, the full text stays with its entity.
PAYLOAD_TEXT_LIMIT = 1000

# --- approvals ---------------------------------------------------------------

_APPROVAL_REQUESTED_V1 = {
    "taskId": UUID_N,
    "artifactId": UUID_N,
    "requiredRoleId": described(UUID_N, "Set when any holder of the role may decide"),
    "assignedPrincipalId": described(UUID_N, "Set when one principal decides"),
    "gate": described(BOOL, "A gate holds the task's claim and completion until decided"),
}
_register(
    "approval.requested",
    "approval",
    "A decision was requested from a principal or from the holders of a role.",
    data(_APPROVAL_REQUESTED_V1),
    (
        data(
            {
                **_APPROVAL_REQUESTED_V1,
                "workspaceId": described(
                    UUID_N, "The approval's workspace, else its task's workspace"
                ),
                "taskPublicId": described(STR_N, "Public id of the task, e.g. TASK-000123"),
                "taskTitle": STR_N,
                "requestedBy": described(UUID, "Principal who requested the decision"),
                "comment": described(
                    {"type": "string", "maxLength": PAYLOAD_TEXT_LIMIT},
                    "Request comment; credential-shaped material redacted, cut to the limit",
                ),
            }
        ),
        "workspaceId, taskPublicId, taskTitle, requestedBy, comment",
    ),
)

_APPROVAL_DECIDED_V1 = {
    "taskId": UUID_N,
    "artifactId": UUID_N,
    "outcomeStatus": described(
        STR_N, "pending when the task type declares outcomes for this decision (CP-ADR-0061)"
    ),
}
_APPROVAL_DECIDED_V2 = {
    **_APPROVAL_DECIDED_V1,
    "decisionBy": described(UUID, "Principal who decided"),
    "comment": described(
        {"type": ["string", "null"], "maxLength": PAYLOAD_TEXT_LIMIT},
        "Decision comment; credential-shaped material redacted, cut to the limit",
    ),
    "channel": described(
        STR_N,
        "Channel the decision came through when it was not a direct API call"
        " (e.g. telegram); null for a direct call",
    ),
}
for _decision in ("approved", "rejected"):
    _register(
        f"approval.{_decision}",
        "approval",
        f"The approval was {_decision} by an eligible principal.",
        data(_APPROVAL_DECIDED_V1),
        (data(_APPROVAL_DECIDED_V2), "decisionBy, comment, channel"),
    )

_register(
    "approval.cancelled",
    "approval",
    "The pending approval was cancelled; nobody decides it any more.",
    data({"taskId": UUID_N}),
    (
        data({"taskId": UUID_N, "cancelledBy": described(UUID, "Principal who cancelled")}),
        "cancelledBy",
    ),
)
_register(
    "approval.outcome_executed",
    "approval",
    "The outcome actions the task type declares for the decision were executed.",
    data({"taskId": UUID, "outcome": STR, "actions": ARR}),
)
_register(
    "approval.outcome_deferred",
    "approval",
    "An outcome action waits for something (e.g. a skill invocation) before continuing.",
    data({"taskId": UUID, "outcome": STR, "waitingAction": OBJ, "reason": STR, "details": OBJ}),
)
_register(
    "approval.outcome_failed",
    "approval",
    "An outcome action failed; the remaining actions stay for replay.",
    data(
        {
            "taskId": UUID,
            "outcome": STR,
            "failedAction": OBJ,
            "actions": ARR,
            "failureWorkTaskId": UUID_N,
        }
    ),
)

# --- credentials and identities ---------------------------------------------

_register(
    "api_key.created",
    "api_key",
    "An API key was issued to a principal.",
    data({"principalId": UUID, "keyPrefix": STR, "permissions": ARR}),
)
_register(
    "api_key.revoked",
    "api_key",
    "An API key was revoked.",
    data({"keyPrefix": STR}, {"breakGlass": BOOL, "issuedBy": ANY}),
)
_register(
    "api_key.break_glass_issued",
    "api_key",
    "A short-lived break-glass key was issued from the host shell (CP-ADR-0065).",
    data(
        {
            "principalId": UUID,
            "keyPrefix": STR,
            "permissions": ARR,
            "expiresAt": TIME,
            "ttlSeconds": INT,
            "reason": STR,
            "issuedBy": ANY,
        }
    ),
)
_register(
    "iam_binding.created",
    "iam_binding",
    "An IAM identity was bound to a local principal (CP-ADR-0053).",
    data(
        {
            "principalId": UUID,
            "issuer": STR,
            "iamTenantId": ANY,
            "iamPrincipalId": ANY,
            "permissions": ARR,
        }
    ),
)
_register(
    "iam_binding.updated",
    "iam_binding",
    "The permissions of an IAM binding changed.",
    data(
        {
            "principalId": UUID,
            "issuer": STR,
            "iamTenantId": ANY,
            "iamPrincipalId": ANY,
            "permissions": ARR,
        }
    ),
)
_register(
    "iam_binding.revoked",
    "iam_binding",
    "An IAM binding was revoked; the identity no longer enters.",
    data({"principalId": UUID, "issuer": STR, "iamPrincipalId": ANY}),
)
_register(
    "delegation.created",
    "delegation",
    "A human delegated permissions to an agent.",
    data({"humanPrincipalId": UUID, "agentPrincipalId": UUID, "permissions": ARR}),
)
_register("delegation.revoked", "delegation", "A delegation was revoked.", data())
_register(
    "principal.created",
    "principal",
    "A principal (human, agent or service) was created.",
    data({"kind": STR, "displayName": STR}),
)
_register(
    "tenant.bootstrapped",
    "tenant",
    "The tenant was created with its first administrator.",
    data(
        {
            "slug": STR,
            "adminPrincipalId": UUID,
            "apiKeyId": UUID,
            "apiKeyPrefix": STR,
            "iamBindingId": UUID_N,
            "iamPrincipalId": ANY,
        }
    ),
)

# --- organization ------------------------------------------------------------

_register(
    "role.created",
    "role",
    "A role was created, tenant-wide or in a workspace.",
    data({"slug": STR, "name": STR, "workspaceId": UUID_N}),
)
_register(
    "role.updated",
    "role",
    "A role was renamed or redescribed.",
    data({"changes": ANY, "version": INT}),
)
_register(
    "role.assigned",
    "principal",
    "A role was assigned to the principal, tenant-wide or in a workspace subtree.",
    data({"roleId": UUID, "workspaceId": UUID_N}),
)
_register(
    "role.revoked",
    "principal",
    "A role assignment was revoked.",
    data({"roleId": UUID, "workspaceId": UUID_N}),
)
_register("capability.created", "capability", "A capability was created.", data({"name": STR}))
_register(
    "capability.assigned",
    "principal",
    "A capability was assigned to the principal.",
    data({"capabilityId": UUID}),
)
_register(
    "capability.revoked",
    "principal",
    "A capability was revoked from the principal.",
    data({"capabilityId": UUID}),
)
_register(
    "skill.registered",
    "skill",
    "A skill version was registered.",
    data(
        {
            "name": STR,
            "version": ANY,
            "protocol": ANY,
            "invocable": BOOL,
            "sideEffects": ANY,
            "riskLevel": ANY,
        }
    ),
)
_register(
    "skill.updated",
    "skill",
    "The description or status of a skill version changed.",
    data({"changedFields": ARR, "rowVersion": INT}),
)
_register("skill.assigned", "principal", "A skill was assigned.", data({"skillId": UUID}))
_register("skill.revoked", "principal", "A skill was revoked.", data({"skillId": UUID}))

_INVOCATION_BASE = {"skillId": UUID}
_register(
    "skill.invocation_requested",
    "skill_invocation",
    "The core was asked to invoke a skill (CP-ADR-0056).",
    data(
        {
            **_INVOCATION_BASE,
            "skill": STR,
            "version": ANY,
            "sideEffects": ANY,
            "riskLevel": ANY,
            "requestedBy": OBJ,
            "taskId": UUID_N,
            "runId": UUID_N,
            "authorizationBasis": ANY,
        }
    ),
)
_register(
    "skill.invocation_claimed",
    "skill_invocation",
    "An executor took the invocation under a lease.",
    data({**_INVOCATION_BASE, "attempt": INT, "fencingToken": INT, "leaseExpiresAt": TIME}),
)
_register(
    "skill.invocation_succeeded",
    "skill_invocation",
    "The invocation finished; its result is an artifact.",
    data(
        {
            **_INVOCATION_BASE,
            "skill": STR,
            "version": ANY,
            "attempt": INT,
            "taskId": UUID_N,
            "runId": UUID_N,
            "artifactId": UUID_N,
            "cost": ANY,
        }
    ),
)
_register(
    "skill.invocation_retry_scheduled",
    "skill_invocation",
    "The attempt failed with a retryable error; another one is scheduled.",
    data(
        {
            **_INVOCATION_BASE,
            "attempt": INT,
            "maxAttempts": INT,
            "availableAt": TIME,
            "error": OBJ,
        }
    ),
)
_register(
    "skill.invocation_failed",
    "skill_invocation",
    "The invocation failed for good.",
    data(
        {
            **_INVOCATION_BASE,
            "skill": STR,
            "version": ANY,
            "attempt": INT,
            "maxAttempts": INT,
            "taskId": UUID_N,
            "runId": UUID_N,
            "error": OBJ,
        }
    ),
)
_register(
    "skill.invocation_cancelled",
    "skill_invocation",
    "The invocation was cancelled.",
    data(
        {
            **_INVOCATION_BASE,
            "skill": STR,
            "version": ANY,
            "attempt": INT,
            "taskId": UUID_N,
            "runId": UUID_N,
            "code": ANY,
            "reason": STR,
            "wasRunning": BOOL,
            "cancelledBy": UUID_N,
            "initiator": ANY,
        }
    ),
)

# --- workspaces and projects -------------------------------------------------

_register(
    "workspace.created",
    "workspace",
    "A workspace was created.",
    data({"slug": STR, "name": STR, "parentId": UUID_N, "typeKey": ANY}),
)
_register(
    "workspace.updated",
    "workspace",
    "Workspace attributes changed.",
    data({"changes": ANY, "version": INT}),
)
_register("workspace.archived", "workspace", "A workspace was archived.", data({"slug": STR}))
_register(
    "workspace.moved",
    "workspace",
    "A workspace moved under another parent.",
    data({"fromParentId": UUID_N, "toParentId": UUID_N}),
)
_register(
    "workspace.member_added",
    "workspace",
    "A principal became a member of the workspace.",
    data({"principalId": UUID}),
)
_register(
    "workspace.member_removed",
    "workspace",
    "A principal stopped being a member of the workspace.",
    data({"principalId": UUID}),
)
_register(
    "workspace_type.created",
    "workspace_type",
    "A workspace type was created.",
    data({"key": STR, "displayName": STR, "allowedChildTypes": ANY}),
)
_register(
    "workspace_type.updated",
    "workspace_type",
    "A workspace type changed.",
    data({"changes": ANY, "version": INT}),
)
_register(
    "workspace_type.archived",
    "workspace_type",
    "A workspace type was archived.",
    data({"key": STR}),
)
_register(
    "knowledge.snapshot_reconciled",
    "workspace",
    "A knowledge snapshot was reconciled into the memory service (CP-ADR-0060).",
    data(
        {
            "snapshotId": ANY,
            "pack": ANY,
            "source": ANY,
            "observedAt": ANY,
            "workspaceId": UUID,
            "rootWorkspaceId": UUID,
            "namespace": STR,
            "entityCount": INT,
            "relationCount": INT,
            "duplicate": BOOL,
            "counters": OBJ,
        }
    ),
)
_register(
    "knowledge.pack_registered",
    "knowledge_pack",
    "A domain knowledge pack version was registered.",
    data({"name": STR, "version": ANY, "status": STR_N}),
)
_register(
    "knowledge.packs_configured",
    "workspace",
    "The knowledge packs of a workspace tree were configured.",
    data({"workspaceId": UUID, "namespace": STR, "packs": ARR, "strict": BOOL}),
)
_register(
    "project.created",
    "project",
    "A project was created on a workspace (ADR-0031).",
    data(
        {
            "workspaceId": UUID,
            "parentProjectId": UUID_N,
            "templateKey": STR,
            "templateVersion": INT,
            "statusKey": STR,
            "systemStatusCategory": STR,
        }
    ),
)
_register(
    "project.updated",
    "project",
    "Project attributes changed.",
    data({"changes": ARR, "version": INT}),
)
_register(
    "project.status_changed",
    "project",
    "The project moved to another status.",
    data(
        {
            "fromStatusKey": STR,
            "fromSystemStatusCategory": STR,
            "statusKey": STR,
            "systemStatusCategory": STR,
            "comment": ANY,
            "version": INT,
        }
    ),
)
_register(
    "project.archived",
    "project",
    "The project was archived.",
    data({"workspaceId": UUID, "version": INT}),
)
_register(
    "project.config_revision_created",
    "project",
    "A new configuration revision of the project was drafted.",
    data({"revision": INT, "revisionId": UUID, "comment": ANY}),
)
_register(
    "project.config_revision_activated",
    "project",
    "A configuration revision became the active one.",
    data({"revision": INT, "revisionId": UUID, "version": INT}),
)
_register(
    "project_template.created",
    "project_template",
    "A project template version was created.",
    data({"key": STR, "version": INT, "displayName": STR, "initialStatus": ANY}),
)
_register(
    "project_template.deprecated",
    "project_template",
    "A project template version was deprecated.",
    data({"key": STR, "version": INT}),
)

_EXTERNAL_REFERENCE = data(
    {
        "externalReferenceId": UUID,
        "externalSystem": STR,
        "externalType": STR,
        "externalId": STR,
    }
)
for _entity in ("project", "task"):
    _register(
        f"{_entity}.external_reference_added",
        _entity,
        f"A reference to an external system was attached to the {_entity} (ADR-0047).",
        _EXTERNAL_REFERENCE,
    )
    _register(
        f"{_entity}.external_reference_updated",
        _entity,
        f"An external reference of the {_entity} changed.",
        _EXTERNAL_REFERENCE,
    )

# --- tasks -------------------------------------------------------------------

_register(
    "task.created",
    "task",
    "A task was created.",
    data(
        {
            "publicId": STR,
            "title": STR,
            "status": STR,
            "systemStatusCategory": STR,
            "typeKey": STR,
            "typeVersion": INT,
            "priority": ANY,
            "workspaceId": UUID_N,
            "startDate": STR_N,
            "dueDate": STR_N,
            "customFields": BOOL,
            "goalId": UUID_N,
            "origin": ANY,
            "acceptanceChecks": INT,
        }
    ),
)
_register(
    "task.updated",
    "task",
    "Task attributes or its status changed.",
    data(
        {"publicId": STR, "changes": ANY, "version": INT},
        {"fromStatus": STR, "status": STR, "systemStatusCategory": STR},
    ),
)
_register(
    "task.claimed",
    "task",
    "An executor claimed the task under a lease.",
    data(
        {
            "publicId": STR,
            "claimId": UUID,
            "sessionId": UUID_N,
            "holderId": UUID,
            "fencingToken": INT,
            "expiresAt": TIME,
            "status": STR,
            "systemStatusCategory": STR,
            "version": INT,
        }
    ),
)
_register(
    "task.completed",
    "task",
    "The task reached its completion status.",
    data(
        {"publicId": STR, "status": STR, "systemStatusCategory": STR, "version": INT},
        {"verificationId": UUID, "attempt": INT},
    ),
)
_register(
    "task.relation_added",
    "task",
    "A relation to another task was added.",
    data({"relationId": UUID, "toTaskId": UUID, "type": STR}),
)
_register(
    "task.relation_removed",
    "task",
    "A relation between tasks was removed.",
    data({"relationId": UUID, "fromTaskId": UUID, "toTaskId": UUID, "type": STR}),
)
_COMMENT = data(
    {
        "commentId": UUID,
        "authorPrincipalId": UUID,
        "version": INT,
        "bodyLength": INT,
        "runId": UUID_N,
        "artifactId": UUID_N,
    }
)
_register("task.comment_added", "task", "A comment was added to the task.", _COMMENT)
_register("task.comment_edited", "task", "A task comment was edited.", _COMMENT)
_register(
    "task.context_pack_recorded",
    "task",
    "The context pack assembled for the task on claim was recorded (CP-ADR-0064).",
    data(
        {
            "publicId": STR,
            "contextPackId": UUID,
            "claimId": UUID_N,
            "asOf": STR,
            "asOfMode": STR,
            "entities": INT,
            "facts": INT,
            "snapshots": INT,
        }
    ),
)
_register(
    "task.completion_work_executed",
    "task",
    "The completion work the task type declares was executed (ADR-0061).",
    data({"publicId": STR, "taskTypeId": UUID, "actions": ARR}),
)
_register(
    "task.completion_work_failed",
    "task",
    "An action of the completion work failed; the completion stands.",
    data({"publicId": STR, "taskTypeId": UUID, "failedAction": OBJ, "actions": ARR}),
)
_VERIFICATION = {
    "publicId": STR,
    "taskId": UUID,
    "verificationId": UUID,
    "attempt": INT,
    "trigger": ANY,
    "checks": INT,
}
_register(
    "task.verification_started",
    "task",
    "A verification attempt of the task's acceptance checks opened (CP-ADR-0067).",
    data(_VERIFICATION, {"triggerRef": ANY}),
)
_register(
    "task.verified",
    "task",
    "Every acceptance check passed; the task is complete.",
    data({**_VERIFICATION, "results": ARR, "artifactId": UUID}),
)
_register(
    "task.verification_failed",
    "task",
    "An acceptance check failed; the task went back to its executor or got blocked.",
    data(
        {
            **_VERIFICATION,
            "results": ARR,
            "failedCheck": ANY,
            "reason": ANY,
            "consecutiveFailures": INT,
            "blocked": BOOL,
            "fromStatus": STR,
            "status": STR,
            "systemStatusCategory": STR,
        }
    ),
)
_TASK_TYPE_CREATED_V1: dict[str, JsonSchema] = {
    "key": STR,
    "version": INT,
    "displayName": STR,
    "initialStatus": ANY,
    "completionStatus": ANY,
    "execution": ANY,
    "declaresApprovalOutcomes": BOOL,
    "declaresContextProfile": BOOL,
    "declaresInstructions": BOOL,
    "declaresCompletionWork": BOOL,
}
_register(
    "task_type.created",
    "task_type",
    "A task type version was created (ADR-0048).",
    data(_TASK_TYPE_CREATED_V1),
    (
        data(
            {
                **_TASK_TYPE_CREATED_V1,
                "declaresArtifactSchema": BOOL,
                "inputs": described(INT, "Number of declared artifact inputs"),
                "outputs": described(INT, "Number of declared artifact outputs"),
            }
        ),
        "declaresArtifactSchema, inputs, outputs (CP-ADR-0072)",
    ),
)
_register(
    "task_type.deprecated",
    "task_type",
    "A task type version was deprecated.",
    data({"key": STR, "version": INT}),
)

# --- claims, sessions, runs --------------------------------------------------

_register(
    "claim.released",
    "claim",
    "The claim on a task ended: completed, released, cancelled or superseded.",
    data({"taskId": UUID, "reason": ANY}, {"taskStatus": STR, "taskSystemStatusCategory": STR}),
)
_register(
    "claim.expired",
    "claim",
    "The lease of a claim ran out.",
    data({"taskId": UUID}, {"reason": ANY, "taskStatus": STR, "taskSystemStatusCategory": STR}),
)
_register(
    "session.opened",
    "session",
    "A harness opened a work session.",
    data(
        {
            "clientName": ANY,
            "harnessType": ANY,
            "controlLevel": ANY,
            "protocolVersion": ANY,
            "onBehalfOf": UUID_N,
            "expiresAt": TIME,
        }
    ),
)
_register(
    "session.expired",
    "session",
    "A work session expired; its claims were released.",
    data({"expiresAt": ANY}, {"releasedClaims": ARR}),
)
_register("session.closed", "session", "A work session was closed.", data({"releasedClaims": ARR}))

_RUN = {"taskId": UUID}
_RUN_STARTED_V1 = {
    **_RUN,
    "claimId": UUID,
    "attempt": INT,
    "fencingToken": INT,
    "instructionsHash": ANY,
    "instructionsRefs": ANY,
}
_register(
    "run.started",
    "run",
    "An execution attempt started under a claim.",
    data(_RUN_STARTED_V1),
    (
        data(
            {
                **_RUN_STARTED_V1,
                "agentRevisionId": described(
                    UUID_N,
                    "Agent revision the run goes by (CP-ADR-0073 §7); "
                    "null for executors that are not registered agents",
                ),
            }
        ),
        "agentRevisionId",
    ),
)
_register(
    "run.succeeded",
    "run",
    "The run finished successfully.",
    data({**_RUN, "attempt": INT, "taskCompleted": BOOL}),
)
_register(
    "run.failed",
    "run",
    "The run failed.",
    data({**_RUN, "reason": ANY, "attempt": INT}),
)
_register(
    "run.suspended",
    "run",
    "The run was suspended, e.g. to wait for a decision.",
    data({**_RUN, "reason": ANY, "attempt": INT}, {"waitingForApprovalId": UUID_N}),
)
_register(
    "run.checkpointed",
    "run",
    "The run left a checkpoint.",
    data({**_RUN, "checkpointId": UUID, "seq": INT, "kind": STR}),
)
_register(
    "run.handoff_prepared",
    "run",
    "The run prepared a handoff to another executor.",
    data({**_RUN, "claimId": UUID, "checkpointId": UUID, "fencingToken": INT, "reason": ANY}),
)
_register(
    "run.cancel_requested",
    "run",
    "Cancellation of the run was requested.",
    data({**_RUN, "attempt": INT}, {"reason": ANY, "controlMessageId": UUID}),
)
_register(
    "run.cancelled",
    "run",
    "The run was cancelled.",
    data({**_RUN, "reason": ANY, "attempt": INT}, {"controlMessageId": UUID}),
)
_register(
    "run.manifest_compiled",
    "run",
    "The effective harness manifest of the run was compiled (ADR-0043). No longer "
    "written since ADR-0073; kept for events already in the journal.",
    data(
        {
            **_RUN,
            "manifestId": UUID,
            "version": INT,
            "baseHash": ANY,
            "reason": ANY,
            "modelAttempt": ANY,
            "supersedesVersion": ANY,
        }
    ),
)
_register(
    "run.manifest_ephemeral_recorded",
    "run",
    "An ephemeral manifest change was recorded (ADR-0043). No longer written since "
    "ADR-0073; kept for events already in the journal.",
    data({**_RUN, "manifestId": UUID, "version": INT, "seq": INT, "kind": ANY}),
)
_CONTROL_MESSAGE = {
    **_RUN,
    "controlMessageId": UUID,
    "seq": INT,
    "operation": ANY,
    "status": STR,
    "causalPosition": ANY,
}
_register(
    "run.control_message.accepted",
    "run",
    "A control message for the active turn was accepted (ADR-0044).",
    data(_CONTROL_MESSAGE),
)
for _status in ("applied", "rejected", "superseded"):
    _register(
        f"run.control_message.{_status}",
        "run",
        f"A control message was {_status}.",
        data({**_CONTROL_MESSAGE, "safeBoundary": ANY}),
    )
_register(
    "run.child.launched",
    "run",
    "The run launched a child task under a handle (ADR-0046).",
    data(
        {
            **_RUN,
            "childHandleId": UUID,
            "childTaskId": UUID,
            "childTaskPublicId": STR,
            "correlationId": ANY,
            "cancellationPolicy": ANY,
            "depth": INT,
            "grantSizes": OBJ,
        }
    ),
)
_CHILD = {"childHandleId": UUID, "correlationId": ANY}
_register(
    "run.child.started",
    "run",
    "A run of the child task started.",
    data({**_CHILD, "childTaskId": UUID, "childRunId": UUID, "attempt": INT}),
)
_register(
    "run.child.resolved",
    "run",
    "The child handle was resolved with the child's outcome.",
    data(
        {
            **_CHILD,
            "childRunId": UUID_N,
            "outcome": ANY,
            "resultHash": ANY,
            "artifactRefs": ARR,
        }
    ),
)
_register(
    "run.child.revoked",
    "run",
    "The child handle was revoked.",
    data({**_CHILD, "childTaskId": UUID, "reason": ANY}),
)
_register(
    "run.child.cancel_requested",
    "run",
    "Cancellation of the child run was requested.",
    data({**_CHILD, "childRunId": UUID_N, "controlMessageId": UUID, "reason": ANY}),
)

# --- artifacts, observations, goals -----------------------------------------

_ARTIFACT_CREATED_V1 = {
    "type": STR,
    "name": STR,
    "taskId": UUID_N,
    "runId": UUID_N,
    "uri": ANY,
    "supersedesArtifactId": UUID_N,
}
_ARTIFACT_CREATED_OPTIONAL = {
    "skillInvocationId": UUID,
    "ruleEvaluationId": UUID,
    "verificationId": UUID,
}
_CONTENT_STATE: JsonSchema = {"type": "string", "enum": ["none", "stored", "purged"]}
_SHA256: JsonSchema = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_register(
    "artifact.created",
    "artifact",
    "An artifact was recorded.",
    data(_ARTIFACT_CREATED_V1, _ARTIFACT_CREATED_OPTIONAL),
    (
        data(
            {
                **_ARTIFACT_CREATED_V1,
                "sizeBytes": described(INT_N, "Size of the stored content; null without one"),
                "mediaType": STR_N,
                "sha256": nullable(_SHA256),
                "contentState": _CONTENT_STATE,
                "typeVersion": described(
                    INT_N, "Version of the registered artifact type it was checked against"
                ),
            },
            _ARTIFACT_CREATED_OPTIONAL,
        ),
        "sizeBytes, mediaType, sha256, contentState, typeVersion (CP-ADR-0072)",
    ),
)
_register(
    "artifact.content_read",
    "artifact",
    "The bytes of an artifact were handed out (CP-ADR-0072 §5).",
    data(
        {
            "artifactId": UUID,
            "taskId": UUID_N,
            "forTaskId": described(UUID_N, "Receiving task when read as its input"),
            "runId": described(UUID_N, "The reader's running run on that task, if any"),
            "sha256": _SHA256,
            "sizeBytes": INT,
        }
    ),
)
_register(
    "artifact.content_purged",
    "artifact",
    "The bytes of an artifact were removed by an administrator; the record stays.",
    data(
        {
            "artifactId": UUID,
            "taskId": UUID_N,
            "sha256": _SHA256,
            "sizeBytes": INT,
            "reason": described(
                {"type": "string", "maxLength": PAYLOAD_TEXT_LIMIT},
                "Reason given; credential-shaped material redacted, cut to the limit",
            ),
            "objectDeleted": described(
                BOOL, "False when other artifacts or uploads still need the object"
            ),
        }
    ),
)
_register(
    "artifact_type.created",
    "artifact_type",
    "An artifact type version was created (CP-ADR-0072).",
    data(
        {
            "key": STR,
            "version": INT,
            "mediaTypes": ARR,
            "maxBytes": INT,
            "declaresMetadataSchema": BOOL,
        }
    ),
)
# --- agent registry (CP-ADR-0073) ---------------------------------------------

_AGENT_HASH: JsonSchema = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
_AGENT_STATE: JsonSchema = {"type": "string", "enum": ["running", "stopped"]}
_AGENT_PHASE: JsonSchema = {
    "type": "string",
    "enum": [
        "pending",
        "running",
        "waiting_for_node",
        "crash_looping",
        "node_unavailable",
        "stopped",
    ],
}
_register(
    "agent.revision_published",
    "agent",
    "A new immutable revision of an agent spec was published (CP-ADR-0073 §2).",
    data(
        {
            "key": STR,
            "revision": INT,
            "specHash": _AGENT_HASH,
            "previousRevision": described(INT_N, "Null for the first revision of the key"),
            "executorKind": described(STR_N, "Null for an identity without placement"),
            "placed": described(BOOL, "False for placement none"),
            "permissionsChanged": described(
                BOOL, "Identity (roles, permissions, capabilities) differs from the previous one"
            ),
        }
    ),
)
_register(
    "agent.state_changed",
    "agent",
    "The desired state or replica count of an agent changed; no new revision.",
    data(
        {
            "key": STR,
            "state": _AGENT_STATE,
            "replicas": INT,
            "previousState": described(
                {"type": ["string", "null"], "enum": [*_AGENT_STATE["enum"], None]},
                "Null when the agent is first published",
            ),
            "previousReplicas": INT_N,
        }
    ),
)
_register(
    "agent.status_changed",
    "agent",
    "The observed state of an agent changed: phase, reason, node or revision.",
    data(
        {
            "key": STR,
            "phase": _AGENT_PHASE,
            "previousPhase": described(
                {"type": ["string", "null"], "enum": [*_AGENT_PHASE["enum"], None]},
                "Null on the first report",
            ),
            "reasonCode": described(STR_N, "Why it is not running, e.g. no_matching_node"),
            "node": STR_N,
            "observedRevision": INT_N,
            "observedAt": TIME,
        }
    ),
)
_register(
    "agent.retired",
    "agent",
    "An agent was retired: stopped, binding revoked, history kept.",
    data(
        {
            "key": STR,
            "revision": described(INT, "The last revision of the agent"),
            "principalId": UUID_N,
            "reason": described(
                {"type": "string", "maxLength": PAYLOAD_TEXT_LIMIT},
                "Reason given; credential-shaped material redacted, cut to the limit",
            ),
            "releasedClaims": described(INT, "Active claims of the agent released to the queue"),
        }
    ),
)
_register(
    "observation.recorded",
    "observation",
    "An observation was recorded (ADR-0057).",
    data(
        {"kind": STR, "content": ANY, "observedAt": TIME},
        {
            "source": STR,
            "dedupKey": STR,
            "externalRef": OBJ,
            "assertions": ARR,
            "data": OBJ,
            "taskId": UUID,
            "runId": UUID,
            "workspaceId": UUID,
            "supersedes": UUID,
        },
    ),
)
_register(
    "goal.created",
    "goal",
    "A goal was created (CP-ADR-0062).",
    data(
        {
            "goalId": UUID,
            "title": STR,
            "status": STR,
            "workspaceId": UUID_N,
            "ownerId": UUID_N,
            "parentGoalId": UUID_N,
            "criteriaCount": INT,
            "createdFrom": ANY,
        }
    ),
)
_register(
    "goal.updated",
    "goal",
    "Goal attributes or its status changed.",
    data({"goalId": UUID, "changes": ANY, "version": INT}, {"fromStatus": STR, "status": STR}),
)

# --- work derivation rules ---------------------------------------------------

_RULE_SUMMARY = {
    "ruleId": UUID,
    "key": STR,
    "version": INT,
    "status": STR,
    "workspaceId": UUID_N,
    "goalId": UUID_N,
    "trigger": OBJ,
    "skill": ANY,
    "action": OBJ,
}
_register("rule.created", "rule", "A work rule was created (CP-ADR-0063).", data(_RULE_SUMMARY))
_register(
    "rule.updated",
    "rule",
    "A work rule changed.",
    data({**_RULE_SUMMARY, "changes": ARR}),
)
_register("rule.enabled", "rule", "A work rule was enabled.", data(_RULE_SUMMARY))
_register("rule.disabled", "rule", "A work rule was disabled.", data(_RULE_SUMMARY))
_register("rule.archived", "rule", "A work rule was archived.", data(_RULE_SUMMARY))
_register(
    "rule.evaluated",
    "rule",
    "A work rule was evaluated against a trigger.",
    data(
        {
            "ruleId": UUID,
            "ruleKey": STR,
            "ruleVersion": INT,
            "evaluationId": UUID,
            "triggerRef": ANY,
            "trigger": OBJ,
            "result": STR,
            "conditionMatched": ANY,
            "evidence": ARR,
            "skillInvocationId": UUID_N,
            "work": ANY,
            "error": nullable(OBJ),
        }
    ),
)
_WORK = {
    "ruleId": UUID,
    "ruleKey": STR,
    "ruleVersion": INT,
    "evaluationId": UUID,
    "taskId": UUID,
    "publicId": STR,
    "evidence": ARR,
    "action": STR,
    "dedupKey": STR,
}
_register(
    "work.derived",
    "task",
    "A rule derived new work.",
    data({**_WORK, "created": BOOL}, {"approvalId": UUID}),
)
_register(
    "work.reconciled",
    "task",
    "A rule updated, cancelled or completed the work it derived earlier.",
    data({**_WORK, "changes": ARR}, {"verificationId": UUID, "check": ANY}),
)

# --- attention ---------------------------------------------------------------

_register(
    "attention.feedback_recorded",
    "attention_feedback",
    "A principal judged an item of its attention list (CP-ADR-0071).",
    data(
        {
            "principalId": described(UUID, "Whose attention list the item was on"),
            "itemKey": described(STR, "Stable key of the item: <ruleKey>:<entityId>"),
            "rule": described(STR, "The rule that raised the item, as ruleKey@version"),
            "ruleKey": STR,
            "ruleVersion": INT,
            "kind": STR,
            "reasonCode": STR,
            "entityType": described(STR, "approval or task"),
            "entityId": UUID,
            "score": INT,
            "verdict": described(STR, "useful or not_needed"),
            "created": described(BOOL, "false when the verdict replaced an earlier one"),
            "hasComment": described(BOOL, "The comment itself stays with the feedback row"),
        }
    ),
)

# --- operations --------------------------------------------------------------

_register(
    "event_journal.archived",
    "event_journal",
    "Journal events were moved to the archive (ADR-0038).",
    data({"archived": INT, "throughCursor": STR, "minAgeSeconds": INT}),
)
_register(
    "event_journal.pruned",
    "event_journal",
    "Archived journal events were deleted.",
    data({"pruned": INT, "throughCursor": STR, "minAgeSeconds": INT}),
)
_register(
    "context_adapter.redriven",
    "event_consumer",
    "The memory context adapter was redriven past a parked event.",
    data(
        {
            "consumer": STR,
            "wasParked": BOOL,
            "parkedReason": ANY,
            "parkedEventId": UUID_N,
            "cursor": STR,
            "reason": STR,
        }
    ),
)
_register(
    "context_adapter.rebuilt",
    "event_consumer",
    "The memory context adapter was rewound to rebuild its projection.",
    data({"consumer": STR, "fromCursor": ANY, "toCursor": STR, "reason": STR}),
)


# --- publication -------------------------------------------------------------

ENVELOPE_FIELDS: tuple[tuple[str, str], ...] = (
    ("id", "Event identifier (uuid); the key for deduplication"),
    ("type", "Event type, see below"),
    ("schemaVersion", "Version of the payload schema of this type"),
    ("sequence", "Journal sequence number; an identifier, not the replay order"),
    ("cursor", "Opaque replay cursor of the event"),
    ("tenantId", "Tenant"),
    ("entityType", "Entity the event is about"),
    ("entityId", "Its identifier"),
    ("workspaceId", "Workspace of the entity; null for tenant-level events"),
    ("actorId", "Principal who acted; null for the core itself"),
    ("iamActorId", "IAM identity of the actor, when there is one"),
    ("occurredAt", "When it happened"),
    ("correlationId", "Correlation of the request chain"),
    ("causationId", "What caused it, when known"),
    ("requestId", "Request that wrote it"),
    ("sessionId", "Work session, when there is one"),
    ("traceRunId", "Distributed trace id (X-Run-Id)"),
    ("payload", "Data of the event, by the schema of (type, schemaVersion)"),
)


def catalog_document() -> dict[str, Any]:
    """The catalog as one JSON document (``docs/events/catalog.json``)."""
    return {
        "envelope": {name: description for name, description in ENVELOPE_FIELDS},
        "types": {
            entry.type: {
                "entityType": entry.entity_type,
                "description": entry.description,
                "currentVersion": entry.current.version,
                "versions": {
                    str(v.version): {
                        **({"changes": v.changes} if v.changes else {}),
                        "schema": v.schema,
                    }
                    for v in entry.versions
                },
            }
            for entry in event_types()
        },
    }


def _schema_rows(schema: JsonSchema) -> list[str]:
    required = set(schema.get("required", ()))
    rows = []
    for name, prop in schema.get("properties", {}).items():
        kind = prop.get("type", "any")
        kind = " \\| ".join(kind) if isinstance(kind, list) else kind
        if "format" in prop:
            kind = f"{kind} ({prop['format']})"
        presence = "да" if name in required else "нет"
        rows.append(f"| `{name}` | {kind} | {presence} | {prop.get('description', '')} |")
    return rows


def render_markdown() -> str:
    """The human-readable catalog (``docs/events/catalog.md``)."""
    lines = [
        "# Каталог событий ядра",
        "",
        "<!-- Сгенерировано из src/control_plane/domain/event_catalog.py:"
        " make event-catalog. Не править руками. -->",
        "",
        "Контракт событий — [CP-ADR-0068](../adr/0068-event-filters-catalog-versions.md).",
        "Версия схемы данных только добавляет поля: потребитель версии N читает",
        "N+1 без изменений и игнорирует незнакомые поля. Машиночитаемый каталог",
        "с JSON Schema — [catalog.json](catalog.json).",
        "",
        "## Конверт",
        "",
        "| Поле | Смысл |",
        "|---|---|",
        *(f"| `{name}` | {description} |" for name, description in ENVELOPE_FIELDS),
        "",
        "## Типы",
        "",
        "| Тип | Сущность | Версия | Описание |",
        "|---|---|---|---|",
        *(
            f"| [`{e.type}`](#{e.type.replace('.', '')}) | `{e.entity_type}` |"
            f" {e.current.version} | {e.description} |"
            for e in event_types()
        ),
    ]
    for entry in event_types():
        lines += ["", f"### {entry.type}", "", entry.description, ""]
        lines.append(f"Сущность: `{entry.entity_type}`.")
        for version in reversed(entry.versions):
            lines += ["", f"Версия {version.version}"]
            if version.changes:
                lines[-1] += f" (добавлено: {version.changes})"
            lines[-1] += ":"
            rows = _schema_rows(version.schema)
            if rows:
                lines += ["", "| Поле | Тип | Всегда | Описание |", "|---|---|---|---|", *rows]
            else:
                lines += ["", "Данных нет."]
    return "\n".join(lines) + "\n"


def write_catalog(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "catalog.md").write_text(render_markdown(), encoding="utf-8")
    (directory / "catalog.json").write_text(
        json.dumps(catalog_document(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":  # pragma: no cover - thin CLI
    write_catalog(Path(sys.argv[1] if len(sys.argv) > 1 else "docs/events"))
