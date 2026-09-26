"""Scoped Tool Discovery: one decision, one projection, one revision (HRS-3).

Three consumers ask overlapping questions about the same tool:

* the Effective Harness Manifest asks "was it visible, and why" (HRS-2);
* discovery asks "what may this run see right now";
* action authorization asks "may this run execute it *now*".

They are answered by ``decide_visibility`` here and nowhere else. A second
implementation would eventually disagree with the first, and the disagreement
would be silent: search would offer a tool the invocation gate rejects, or —
far worse — the gate would accept one search was never allowed to reveal.

Everything in this module is a pure function over plain data. No database, no
clock, no HTTP: the security claims are the kind a table of unit cases can
settle.

See ``docs/specs/TASK-000005-scoped-tool-discovery.md``.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from control_plane.domain.canonical import canonical_bytes, content_hash
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import (
    guard_json_document,
    reject_secret_material,
)

# --- bounds -------------------------------------------------------------------

MAX_QUERY_CHARS = 200
DEFAULT_PAGE_LIMIT = 25
MAX_PAGE_LIMIT = 100
MAX_SUMMARY_CHARS = 200
MAX_DESCRIPTION_CHARS = 4_000
MAX_SCHEMA_BYTES = 32 * 1024

# --- decision reasons ---------------------------------------------------------

REASON_VISIBLE = "assigned_and_protocol_supported"
REASON_NOT_ASSIGNED = "not_assigned"
REASON_DISABLED = "skill_disabled"
REASON_GOVERNANCE = "protocol_not_allowed_by_governance"
REASON_HARNESS = "protocol_not_supported_by_harness"
REASON_CHILD_GRANT = "not_granted_by_child_handle"

SKILL_STATUS_DISABLED = "disabled"

#: The structural vocabulary of JSON Schema — and nothing else. Discovery
#: projects a schema so a model can build a valid call, not so it can read the
#: catalog's configuration: ``default``, ``examples`` and ``x-*`` extensions are
#: where real MCP manifests keep endpoints, account ids and pre-filled tokens.
#: A whitelist (rather than a blacklist of known-bad keys) means a keyword
#: nobody thought about defaults to invisible.
SCHEMA_KEYWORDS = frozenset(
    {
        "$ref",
        "$defs",
        "additionalProperties",
        "allOf",
        "anyOf",
        # Field descriptions are what let a model fill the schema correctly, and
        # they are author-written prose like `title` — no more likely to carry
        # connection material than the tool's own description.
        "description",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "format",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "nullable",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }
)

#: Keywords whose values are *maps of caller-chosen names to schemas*. Their
#: keys are data, not vocabulary, so they are kept verbatim while their values
#: are sanitized recursively.
_SCHEMA_MAP_KEYWORDS = frozenset({"properties", "$defs"})

#: Keywords whose values are lists of schemas.
_SCHEMA_LIST_KEYWORDS = frozenset({"allOf", "anyOf", "oneOf"})

#: Keywords whose values are a schema, a list of schemas, or a bare boolean.
_SCHEMA_OR_BOOL_KEYWORDS = frozenset({"additionalProperties", "items"})


# --- candidates and decisions -------------------------------------------------


@dataclass(frozen=True)
class ToolCandidate:
    """One registry entry, plus how it relates to the asking principal."""

    skill_id: str
    name: str
    version: str
    protocol: str
    status: str
    row_version: int
    description: str = ""
    input_schema: dict[str, Any] | None = None
    assigned: bool = False
    source: str = "principal_skill_assignment"


@dataclass(frozen=True)
class ToolDecision:
    """Why a tool is (or is not) visible, split along its two real dimensions.

    ``authorized`` is what the server grants; ``capable`` is what the client
    says it can run. They stay separate because only the first may ever gate
    execution — see the invocation section of the SPEC.
    """

    authorized: bool
    capable: bool
    reason: str
    allowed_by_governance: bool
    #: False only for a run launched by a parent whose handle withheld this
    #: skill (HRS-7). A root run has no handle and is never narrowed here.
    allowed_by_child_grant: bool = True

    @property
    def visible(self) -> bool:
        return self.authorized and self.capable


def normalize_harness_protocols(
    harness_capabilities: Sequence[str] | None,
) -> frozenset[str] | None:
    """Protocols the session declared, or ``None`` for protocol-agnostic.

    ``None`` (no session information) and an empty set (a session that declared
    capabilities but no skill protocols) are both treated as "filters nothing",
    preserving the v0.3 ``resolve_executable_skills`` contract: a legacy harness
    sees everything it is authorized for and filters locally.
    """
    if harness_capabilities is None:
        return None
    declared = {
        capability.removeprefix("skills.protocol.")
        for capability in harness_capabilities
        if capability.startswith("skills.protocol.")
    }
    return frozenset(declared) if declared else None


def decide_visibility(
    candidate: ToolCandidate,
    *,
    harness_protocols: frozenset[str] | None,
    allowed_protocols: Sequence[str] | None,
    granted_skills: frozenset[str] | None = None,
) -> ToolDecision:
    """The single authority on whether a tool may be seen or executed.

    Reason precedence is deliberate: authorization failures outrank capability
    ones, so the answer a caller gets never depends on what it declared about
    itself. Within authorization, "not assigned to you" comes first because it
    is the coarsest fact.

    ``granted_skills`` is the ceiling of a run launched by a parent (HRS-7),
    given as ``name@version`` refs; ``None`` means "no handle, no narrowing",
    which is not the same as an empty set. It belongs on the authorization
    side, not the capability side: it is the server's own restriction, and
    routing it through this one function is what keeps search, the manifest and
    the invocation gate from disagreeing about the same tool.
    """
    allowed_by_governance = (
        True if allowed_protocols is None else candidate.protocol in tuple(allowed_protocols)
    )
    allowed_by_child_grant = (
        True
        if granted_skills is None
        else f"{candidate.name}@{candidate.version}" in granted_skills
    )
    capable = harness_protocols is None or candidate.protocol in harness_protocols

    if not candidate.assigned:
        reason = REASON_NOT_ASSIGNED
    elif candidate.status == SKILL_STATUS_DISABLED:
        reason = REASON_DISABLED
    elif not allowed_by_governance:
        reason = REASON_GOVERNANCE
    elif not allowed_by_child_grant:
        reason = REASON_CHILD_GRANT
    elif not capable:
        reason = REASON_HARNESS
    else:
        reason = REASON_VISIBLE

    authorized = (
        candidate.assigned
        and candidate.status != SKILL_STATUS_DISABLED
        and allowed_by_governance
        and allowed_by_child_grant
    )
    return ToolDecision(
        authorized=authorized,
        capable=capable,
        reason=reason,
        allowed_by_governance=allowed_by_governance,
        allowed_by_child_grant=allowed_by_child_grant,
    )


# --- revisions ----------------------------------------------------------------


def catalog_revision(entries: Iterable[tuple[str, int, str]]) -> str:
    """Hash of ``(skill id, row version, status)`` over the tenant's catalog.

    Derived rather than stored on purpose. A counter column is one more place
    that can be forgotten on a write path; a hash over the rows themselves
    cannot go stale, and it covers deletions — which a monotonic counter
    bumped by writers does not.
    """
    rows = sorted(
        [str(skill_id), int(row_version), str(status)] for skill_id, row_version, status in entries
    )
    return content_hash(rows)


def policy_revision(
    *,
    principal_id: str,
    assigned_skill_ids: Iterable[str],
    allowed_protocols: Sequence[str] | None,
    project_revision: str | None,
    harness_protocols: frozenset[str] | None,
    granted_skills: frozenset[str] | None = None,
) -> str:
    """Hash of everything that narrows the catalog down to one run.

    The child-handle grant belongs here for the same reason governance does: it
    narrows what this run may see, so a view computed under it must not be
    mistaken for one computed without it.
    """
    return content_hash(
        {
            "principalId": principal_id,
            "assignedSkillIds": sorted(str(skill_id) for skill_id in assigned_skill_ids),
            "allowedProtocols": (
                None if allowed_protocols is None else sorted(str(p) for p in allowed_protocols)
            ),
            "projectRevision": project_revision,
            "harnessProtocols": (None if harness_protocols is None else sorted(harness_protocols)),
            "grantedSkills": (None if granted_skills is None else sorted(granted_skills)),
        }
    )


def view_hash(
    *,
    catalog: str,
    policy: str,
    query: str,
    limit: int,
    cursor: str | None,
) -> str:
    """Identity of one page of the projection — the ETag of a discovery view."""
    return content_hash(
        {
            "catalogRevision": catalog,
            "policyRevision": policy,
            "query": query,
            "limit": limit,
            "cursor": cursor,
        }
    )


# --- schema sanitization ------------------------------------------------------


def _sanitize_node(node: Any, *, path: str, redactions: list[str]) -> Any:
    """Keep the structural skeleton of a schema; drop everything else."""
    if isinstance(node, dict):
        kept: dict[str, Any] = {}
        for raw_key, value in node.items():
            key = str(raw_key)
            child_path = f"{path}.{key}" if path else key
            if key not in SCHEMA_KEYWORDS:
                redactions.append(child_path)
                continue
            if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
                kept[key] = {
                    str(name): _sanitize_node(
                        child, path=f"{child_path}.{name}", redactions=redactions
                    )
                    for name, child in value.items()
                }
            elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
                kept[key] = [
                    _sanitize_node(child, path=f"{child_path}[{index}]", redactions=redactions)
                    for index, child in enumerate(value)
                ]
            elif key in _SCHEMA_OR_BOOL_KEYWORDS and isinstance(value, (dict, list)):
                kept[key] = _sanitize_node(value, path=child_path, redactions=redactions)
            else:
                kept[key] = value
        return kept
    if isinstance(node, list):
        return [
            _sanitize_node(item, path=f"{path}[{index}]", redactions=redactions)
            for index, item in enumerate(node)
        ]
    return node


def sanitize_schema(schema: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """Bounded, secret-free projection of a tool's input schema.

    Returns ``(schema, redactions)``. Redactions are reported rather than
    silently applied: an operator debugging "why does the model not see that
    field" must be able to tell sanitization from an empty schema. If anything
    that looks like secret material survives the whitelist, the whole schema is
    withheld — fail closed — while the tool itself stays visible, because
    hiding the tool would let a secret in its description remove it from every
    operator's view.
    """
    if schema is None:
        return None, []
    if not isinstance(schema, dict):
        raise ValidationError(
            "invalid_tool_schema",
            "A tool input schema must be an object",
        )
    guard_json_document(schema, label="inputSchema", max_bytes=MAX_SCHEMA_BYTES)

    redactions: list[str] = []
    sanitized = _sanitize_node(schema, path="", redactions=redactions)
    try:
        reject_secret_material(sanitized, label="inputSchema")
    except ValidationError:
        return {"redacted": True, "reason": "secret_material"}, sorted(set(redactions))
    # Canonicalizing here rejects floats and exotic types *before* they reach a
    # hash or a client, so an unstable registry entry surfaces as an error on
    # its own tool rather than as a mismatching view hash later.
    canonical_bytes(sanitized)
    return sanitized, sorted(set(redactions))


# --- projections --------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"


def project_summary(candidate: ToolCandidate, decision: ToolDecision) -> dict[str, Any]:
    """Search-result projection: enough to choose, not enough to call."""
    return {
        "id": candidate.skill_id,
        "name": candidate.name,
        "version": candidate.version,
        "protocol": candidate.protocol,
        "status": candidate.status,
        "summary": _truncate(candidate.description, MAX_SUMMARY_CHARS),
        "visible": decision.visible,
        "reason": decision.reason,
    }


def project_detail(candidate: ToolCandidate, decision: ToolDecision) -> dict[str, Any]:
    """Describe projection: the schema needed to build one call.

    ``config`` and ``outputSchema`` are absent by construction — not filtered
    out downstream — so no future edit can leak connection material by adding a
    field to a serializer.
    """
    schema, redactions = sanitize_schema(candidate.input_schema)
    detail = project_summary(candidate, decision)
    detail["description"] = _truncate(candidate.description, MAX_DESCRIPTION_CHARS)
    detail["inputSchema"] = schema
    detail["schemaRedactions"] = redactions
    return detail


def validate_query(query: str | None) -> str:
    """Bounded free-text query; empty means eager mode."""
    text = (query or "").strip()
    if len(text) > MAX_QUERY_CHARS:
        raise ValidationError(
            "invalid_tool_query",
            f"query must be at most {MAX_QUERY_CHARS} characters",
            details={"maxChars": MAX_QUERY_CHARS},
        )
    return text


def clamp_tool_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ValidationError(
            "invalid_tool_query",
            f"limit must be between 1 and {MAX_PAGE_LIMIT}",
            details={"maxLimit": MAX_PAGE_LIMIT},
        )
    return limit
