"""Effective Harness Manifest: deterministic compilation and hashing (HRS-2).

Pure functions over plain data — no database, no HTTP, no clock. That is what
makes the reproducibility claim checkable: the same inputs must produce the
same bytes, and a table of unit cases can prove it without a running system.

The document has three structurally separate parts:

``base``
    Frozen effective configuration: identity, policy revisions, tool policy,
    budgets, model and redaction policy. Hashed.
``provenance``
    Where every base section came from, and why every tool was visible.
    Derived deterministically from the same inputs, hashed together with base.
``captured``
    Operational cursor/versions and a *reference* to the Memory Context Pack.
    NOT hashed: a monotonically growing cursor would make "same revisions →
    same hash" impossible, and eventually-consistent memory must never anchor
    a reproducibility claim.

See ``docs/effective-harness-manifest-spec.md``.
"""

from dataclasses import dataclass, field
from typing import Any

from control_plane.domain.canonical import canonicalize, content_hash
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import (
    guard_json_document,
    reject_secret_material,
)
from control_plane.domain.redaction import reject_unsafe_durable_payload
from control_plane.domain.tool_discovery import (
    ToolCandidate,
    decide_visibility,
    normalize_harness_protocols,
)

SCHEMA_VERSION = 1

#: Sections a harness may declare. Everything else is server-authoritative and
#: is rejected if it appears in a request body.
DECLARABLE_SECTIONS = ("workerProfile", "executionBackend", "model", "redaction")
SERVER_AUTHORITATIVE_SECTIONS = ("identity", "run", "projectPolicy", "toolPolicy", "budgets")

COMPILE_REASONS = ("run_started", "recompile", "provider_fallback")
EPHEMERAL_KINDS = ("steering", "warning", "budget_warning", "note")

MAX_DECLARED_BYTES = 8 * 1024
MAX_EPHEMERAL_BYTES = 4 * 1024
MAX_SUMMARY_CHARS = 500

_UNSAFE_CODE = "unsafe_manifest_payload"
_UNSAFE_SUBJECT = "Manifest declaration"

# --- canonical representation -------------------------------------------------
#
# The canonical form and the hash live in ``domain/canonical.py`` since HRS-3
# started hashing catalog and policy revisions with the same rules. They are
# re-exported here because "the manifest hash" is how the rest of the codebase
# and its tests refer to them.

_canonicalize = canonicalize
manifest_hash = content_hash


# --- inputs -------------------------------------------------------------------


@dataclass(frozen=True)
class IdentityInput:
    tenant_id: str
    principal_id: str
    principal_kind: str
    session_id: str
    control_level: str
    harness_type: str | None = None
    harness_version: str | None = None
    protocol_version: str | None = None
    harness_capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunInput:
    run_id: str
    task_id: str
    claim_id: str
    attempt: int
    fencing_token: int


@dataclass(frozen=True)
class ProjectPolicyInput:
    """Effective project configuration as of compilation (ADR-0032 layers)."""

    project_id: str | None = None
    template_key: str | None = None
    template_version: int | None = None
    active_revision: int | None = None
    governance: dict[str, Any] = field(default_factory=dict)
    governance_origins: dict[str, Any] = field(default_factory=dict)
    layers: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ToolInput:
    """One skill assigned to the principal, as resolved by the server.

    Visibility is deliberately NOT an input: it is decided here, by the same
    function discovery and invocation use (HRS-3), so the manifest cannot
    record a verdict the invocation gate would contradict.
    """

    skill_id: str
    name: str
    version: str
    protocol: str
    status: str
    source: str = "principal_skill_assignment"


@dataclass(frozen=True)
class BudgetInput:
    max_duration_seconds: int | None = None
    max_actions: int | None = None
    governance_ceiling: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapturedInput:
    """Moving state captured with the manifest but deliberately outside the hash."""

    event_cursor: str
    task_version: int
    claim_epoch: int
    run_attempt: int
    captured_at: str
    memory: dict[str, Any] | None = None


@dataclass(frozen=True)
class CompiledManifest:
    base: dict[str, Any]
    provenance: dict[str, Any]
    captured: dict[str, Any]
    base_hash: str
    snapshot_hash: str
    model_attempt: int


# --- declared sections --------------------------------------------------------


def validate_declared_sections(declared: dict[str, Any] | None) -> dict[str, Any]:
    """Validate what a harness claims about itself.

    A declaration is a *record of a statement*, never an authority: nothing
    here feeds an authorization decision. What it must be is bounded, free of
    secrets and canonically representable, because it is about to become
    immutable evidence.
    """
    if declared is None:
        return {}
    if not isinstance(declared, dict):
        raise ValidationError("invalid_declaration", "Declared sections must be an object")

    intruders = sorted(set(declared) & set(SERVER_AUTHORITATIVE_SECTIONS))
    if intruders:
        raise ValidationError(
            "server_authoritative_section",
            "These manifest sections are computed by the server and cannot be declared",
            details={"sections": intruders, "declarable": list(DECLARABLE_SECTIONS)},
        )
    unknown = sorted(set(declared) - set(DECLARABLE_SECTIONS))
    if unknown:
        raise ValidationError(
            "unknown_manifest_section",
            f"Unknown manifest section(s): {unknown}",
            details={"unknown": unknown, "declarable": list(DECLARABLE_SECTIONS)},
        )

    normalized: dict[str, Any] = {}
    for section in DECLARABLE_SECTIONS:
        if section not in declared:
            continue
        value = declared[section]
        guard_json_document(value, label=section, max_bytes=MAX_DECLARED_BYTES)
        reject_secret_material(value, label=section)
        reject_unsafe_durable_payload(value, code=_UNSAFE_CODE, subject=_UNSAFE_SUBJECT)
        # Canonicalizing now means an unstable value (float, exotic type) is
        # rejected at write time rather than producing an unhashable row later.
        normalized[section] = _canonicalize(value, path=section)
    return normalized


def validate_ephemeral(kind: str, summary: str, data: dict[str, Any] | None) -> dict[str, Any]:
    """Validate one ephemeral marker (steering, warning, ...)."""
    if kind not in EPHEMERAL_KINDS:
        raise ValidationError(
            "invalid_ephemeral_kind",
            f"kind must be one of {list(EPHEMERAL_KINDS)}",
            details={"kind": kind},
        )
    if not summary or len(summary) > MAX_SUMMARY_CHARS:
        raise ValidationError(
            "invalid_ephemeral_summary",
            f"summary must be non-empty and at most {MAX_SUMMARY_CHARS} characters",
        )
    payload = data or {}
    guard_json_document(payload, label="data", max_bytes=MAX_EPHEMERAL_BYTES)
    reject_secret_material(payload, label="data")
    reject_unsafe_durable_payload(payload, code=_UNSAFE_CODE, subject="Ephemeral marker")
    reject_unsafe_durable_payload(summary, code=_UNSAFE_CODE, subject="Ephemeral marker")
    canonical: dict[str, Any] = _canonicalize(payload, path="data")
    return canonical


# --- compilation --------------------------------------------------------------


def _tool_policy(
    tools: tuple[ToolInput, ...],
    governance: dict[str, Any],
    harness_protocols: frozenset[str] | None,
    child_grant_skills: frozenset[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve tool visibility and say, per tool, why it is what it is.

    Three independent dimensions are reported separately rather than collapsed
    into one boolean: whether the harness can execute the protocol at all,
    whether project governance allows it, and — for a run launched by a parent
    (HRS-7) — whether the child handle granted it. ``visible`` is their
    conjunction. The decision itself is made by ``decide_visibility`` and
    nowhere else.
    """
    allowed_protocols = governance.get("allowedSkillProtocols")
    if not isinstance(allowed_protocols, list):
        allowed_protocols = None
    entries: list[dict[str, Any]] = []
    visibility: dict[str, Any] = {}
    for tool in sorted(tools, key=lambda t: (t.name, t.skill_id)):
        decision = decide_visibility(
            ToolCandidate(
                skill_id=tool.skill_id,
                name=tool.name,
                version=tool.version,
                protocol=tool.protocol,
                status=tool.status,
                row_version=0,
                assigned=True,
            ),
            harness_protocols=harness_protocols,
            allowed_protocols=allowed_protocols,
            granted_skills=child_grant_skills,
        )
        entries.append(
            {
                "id": tool.skill_id,
                "name": tool.name,
                "version": tool.version,
                "protocol": tool.protocol,
                "status": tool.status,
                "executableByHarness": decision.capable,
                "allowedByGovernance": decision.allowed_by_governance,
                "allowedByChildGrant": decision.allowed_by_child_grant,
                "visible": decision.visible,
            }
        )
        visibility[tool.skill_id] = {
            "visible": decision.visible,
            "reason": decision.reason,
            "source": tool.source,
        }
    return entries, visibility


def compile_manifest(
    *,
    identity: IdentityInput,
    run: RunInput,
    project_policy: ProjectPolicyInput,
    tools: tuple[ToolInput, ...],
    budgets: BudgetInput,
    captured: CapturedInput,
    declared: dict[str, Any] | None = None,
    redaction_default: dict[str, Any] | None = None,
    catalog_revision: str | None = None,
    policy_revision: str | None = None,
    child_grant_skills: frozenset[str] | None = None,
) -> CompiledManifest:
    """Compile one immutable snapshot of the effective runtime configuration."""
    sections = validate_declared_sections(declared)

    harness_protocols = normalize_harness_protocols(list(identity.harness_capabilities) or None)
    tool_entries, visibility = _tool_policy(
        tools, project_policy.governance, harness_protocols, child_grant_skills
    )
    tool_policy = {
        "tools": tool_entries,
        "policyHash": manifest_hash(tool_entries),
        # The revisions the discovery view was computed from (HRS-3). With
        # them, "why was this tool visible in this run" is answerable after the
        # fact: feed them back into discovery and the same projection comes out.
        "catalogRevision": catalog_revision,
        "policyRevision": policy_revision,
    }
    project_section = {
        "projectId": project_policy.project_id,
        "templateKey": project_policy.template_key,
        "templateVersion": project_policy.template_version,
        "activeRevision": project_policy.active_revision,
        "governance": dict(project_policy.governance),
        "layers": [dict(layer) for layer in project_policy.layers],
    }
    project_section["configHash"] = manifest_hash(
        {
            "governance": project_section["governance"],
            "layers": project_section["layers"],
            "activeRevision": project_section["activeRevision"],
            "templateKey": project_section["templateKey"],
            "templateVersion": project_section["templateVersion"],
        }
    )

    model_section = dict(sections.get("model") or {})
    model_attempt = model_section.get("attempt", 1)
    if not isinstance(model_attempt, int) or isinstance(model_attempt, bool) or model_attempt < 1:
        raise ValidationError(
            "invalid_model_attempt",
            "model.attempt must be a positive integer",
            details={"path": "model.attempt"},
        )
    model_section["attempt"] = model_attempt

    unavailable = {"status": "unavailable"}
    base = {
        "schemaVersion": SCHEMA_VERSION,
        "identity": {
            "tenantId": identity.tenant_id,
            "principalId": identity.principal_id,
            "principalKind": identity.principal_kind,
            "sessionId": identity.session_id,
            "controlLevel": identity.control_level,
            "harnessType": identity.harness_type,
            "harnessVersion": identity.harness_version,
            "protocolVersion": identity.protocol_version,
            # Sorted: capability order is a client detail, not content.
            "harnessCapabilities": sorted(identity.harness_capabilities),
        },
        "run": {
            "runId": run.run_id,
            "taskId": run.task_id,
            "claimId": run.claim_id,
            "attempt": run.attempt,
            "fencingToken": run.fencing_token,
        },
        "workerProfile": sections.get("workerProfile") or dict(unavailable),
        "projectPolicy": project_section,
        "toolPolicy": tool_policy,
        "executionBackend": sections.get("executionBackend") or dict(unavailable),
        "model": model_section or dict(unavailable),
        "budgets": {
            "maxDurationSeconds": budgets.max_duration_seconds,
            "maxActions": budgets.max_actions,
            "governanceCeiling": dict(budgets.governance_ceiling),
        },
        "redaction": sections.get("redaction")
        or dict(redaction_default or {"policy": "default", "version": SCHEMA_VERSION}),
    }

    declared_origin = {"source": "harness_declared"}
    absent_origin = {"source": "absent"}
    provenance = {
        "identity": {"source": "server_authoritative", "detail": "authenticated_principal_session"},
        "run": {"source": "server_authoritative", "detail": "run_claim_fencing"},
        "projectPolicy": {
            "source": "project_config" if project_policy.project_id else "absent",
            "layers": [dict(layer) for layer in project_policy.layers],
            "governanceOrigins": dict(project_policy.governance_origins),
        },
        "toolPolicy": {
            "source": "server_authoritative",
            "resolver": "principal_skill_assignment",
            "governanceFilter": "allowedSkillProtocols",
            "revisions": {
                "catalogRevision": catalog_revision,
                "policyRevision": policy_revision,
            },
            "visibility": visibility,
        },
        "workerProfile": declared_origin if "workerProfile" in sections else absent_origin,
        "executionBackend": declared_origin if "executionBackend" in sections else absent_origin,
        "model": declared_origin if "model" in sections else absent_origin,
        "budgets": {"source": "server_authoritative", "ceilingFrom": "project_governance"},
        "redaction": declared_origin if "redaction" in sections else {"source": "server_default"},
    }

    captured_document = {
        "operational": {
            "eventCursor": captured.event_cursor,
            "taskVersion": captured.task_version,
            "claimEpoch": captured.claim_epoch,
            "runAttempt": captured.run_attempt,
            "capturedAt": captured.captured_at,
        },
        # Memory is a *reference*, never content, and lives in its own branch:
        # an eventually-consistent pack must not be mistakable for authoritative
        # operational state (ADR-0028), including inside evidence.
        "memory": dict(captured.memory) if captured.memory else None,
    }

    hashed = {"schemaVersion": SCHEMA_VERSION, "base": base, "provenance": provenance}
    base_hash = manifest_hash(hashed)
    snapshot_hash = manifest_hash({**hashed, "captured": captured_document})
    return CompiledManifest(
        base=_canonicalize(base, path="base"),
        provenance=_canonicalize(provenance, path="provenance"),
        captured=_canonicalize(captured_document, path="captured"),
        base_hash=base_hash,
        snapshot_hash=snapshot_hash,
        model_attempt=model_attempt,
    )
