"""HRS-3 spike: the visibility decision, the revisions and the projection.

No database and no HTTP — every claim the SPEC makes about *what may be seen*
is a claim about pure functions, and this is where it is settled. The threat
model's T4, T5, T6, T10 and T11 all land here.
"""

import pytest

from control_plane.domain.errors import ValidationError
from control_plane.domain.tool_discovery import (
    MAX_QUERY_CHARS,
    REASON_DISABLED,
    REASON_GOVERNANCE,
    REASON_HARNESS,
    REASON_NOT_ASSIGNED,
    REASON_VISIBLE,
    ToolCandidate,
    catalog_revision,
    clamp_tool_limit,
    decide_visibility,
    normalize_harness_protocols,
    policy_revision,
    project_detail,
    project_summary,
    sanitize_schema,
    validate_query,
    view_hash,
)


def candidate(**overrides: object) -> ToolCandidate:
    base = {
        "skill_id": "11111111-1111-1111-1111-111111111111",
        "name": "repo.search",
        "version": "1.0.0",
        "protocol": "mcp",
        "status": "active",
        "row_version": 1,
        "description": "Search files in the target repository",
        "assigned": True,
    }
    base.update(overrides)
    return ToolCandidate(**base)  # type: ignore[arg-type]


# --- the decision -------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "harness", "allowed", "authorized", "capable", "reason"),
    [
        ({}, None, None, True, True, REASON_VISIBLE),
        ({"assigned": False}, None, None, False, True, REASON_NOT_ASSIGNED),
        ({"status": "disabled"}, None, None, False, True, REASON_DISABLED),
        ({}, None, ["http"], False, True, REASON_GOVERNANCE),
        ({}, frozenset({"http"}), None, True, False, REASON_HARNESS),
        ({}, frozenset({"mcp"}), ["mcp"], True, True, REASON_VISIBLE),
        # Deprecated is not disabled: it stays usable and says so.
        ({"status": "deprecated"}, None, None, True, True, REASON_VISIBLE),
    ],
)
def test_decision_matrix(
    overrides: dict[str, object],
    harness: frozenset[str] | None,
    allowed: list[str] | None,
    authorized: bool,
    capable: bool,
    reason: str,
) -> None:
    decision = decide_visibility(
        candidate(**overrides), harness_protocols=harness, allowed_protocols=allowed
    )
    assert (decision.authorized, decision.capable, decision.reason) == (
        authorized,
        capable,
        reason,
    )
    assert decision.visible is (authorized and capable)


def test_authorization_failure_outranks_capability_failure() -> None:
    """A client must not learn a different answer by declaring less.

    Both dimensions fail here; the reason names the server's decision, not the
    client's self-description.
    """
    decision = decide_visibility(
        candidate(assigned=False), harness_protocols=frozenset({"http"}), allowed_protocols=["http"]
    )
    assert decision.reason == REASON_NOT_ASSIGNED


def test_undeclared_protocols_filter_nothing() -> None:
    assert normalize_harness_protocols(None) is None
    assert normalize_harness_protocols(["checkpoints", "resume"]) is None
    assert normalize_harness_protocols(["skills.protocol.mcp"]) == frozenset({"mcp"})


# --- revisions ----------------------------------------------------------------


def test_catalog_revision_ignores_row_order() -> None:
    rows = [("a", 1, "active"), ("b", 2, "deprecated")]
    assert catalog_revision(rows) == catalog_revision(reversed(rows))


@pytest.mark.parametrize(
    "changed",
    [
        [("a", 2, "active")],
        [("a", 1, "disabled")],
        [("a", 1, "active"), ("b", 1, "active")],
        [],
    ],
)
def test_catalog_revision_changes_with_the_catalog(changed: list[tuple[str, int, str]]) -> None:
    assert catalog_revision(changed) != catalog_revision([("a", 1, "active")])


def test_policy_revision_covers_every_narrowing_input() -> None:
    def revision(**overrides: object) -> str:
        base: dict[str, object] = {
            "principal_id": "p1",
            "assigned_skill_ids": ["s1"],
            "allowed_protocols": ["mcp"],
            "project_revision": "proj@3",
            "harness_protocols": frozenset({"mcp"}),
        }
        base.update(overrides)
        return policy_revision(**base)  # type: ignore[arg-type]

    baseline = revision()
    assert revision(assigned_skill_ids=["s1", "s2"]) != baseline
    assert revision(allowed_protocols=None) != baseline
    assert revision(project_revision="proj@4") != baseline
    assert revision(harness_protocols=None) != baseline
    assert revision(principal_id="p2") != baseline
    # Assignment order is not content.
    assert revision(assigned_skill_ids=["s1"]) == baseline


def test_view_hash_covers_page_identity() -> None:
    def page(**overrides: object) -> str:
        base: dict[str, object] = {
            "catalog": "sha256:a",
            "policy": "sha256:b",
            "query": "deploy",
            "limit": 25,
            "cursor": None,
        }
        base.update(overrides)
        return view_hash(**base)  # type: ignore[arg-type]

    baseline = page()
    assert page(catalog="sha256:c") != baseline
    assert page(policy="sha256:c") != baseline
    assert page(query="deployment") != baseline
    assert page(limit=50) != baseline
    assert page(cursor="abc") != baseline
    assert page() == baseline


# --- projection and sanitization ----------------------------------------------


def test_sanitize_keeps_structure_and_drops_everything_else() -> None:
    schema, redactions = sanitize_schema(
        {
            "type": "object",
            "title": "Deploy",
            "default": {"endpoint": "https://internal.example/deploy"},
            "x-connection": {"host": "10.0.0.4"},
            "$comment": "internal only",
            "properties": {
                "environment": {
                    "type": "string",
                    "description": "Where to deploy",
                    "enum": ["staging", "production"],
                    "examples": ["staging"],
                },
                "x-weird-name": {"type": "string", "default": "admin"},
            },
            "required": ["environment"],
        }
    )
    assert schema == {
        "type": "object",
        "title": "Deploy",
        "properties": {
            "environment": {
                "type": "string",
                "description": "Where to deploy",
                "enum": ["staging", "production"],
            },
            # A caller-chosen property name is data: it survives even when it
            # looks like a vendor extension.
            "x-weird-name": {"type": "string"},
        },
        "required": ["environment"],
    }
    assert redactions == [
        "$comment",
        "default",
        "properties.environment.examples",
        "properties.x-weird-name.default",
        "x-connection",
    ]


def test_sanitize_recurses_through_composition_keywords() -> None:
    schema, _ = sanitize_schema(
        {
            "oneOf": [{"type": "string", "default": "x"}, {"type": "integer"}],
            "items": {"type": "object", "x-secretish": 1},
            "additionalProperties": False,
        }
    )
    assert schema == {
        "oneOf": [{"type": "string"}, {"type": "integer"}],
        "items": {"type": "object"},
        "additionalProperties": False,
    }


def test_sanitize_fails_closed_on_secret_material() -> None:
    """The whitelist is the first line; a surviving secret withholds the schema."""
    schema, _ = sanitize_schema({"type": "object", "properties": {"apiKey": {"type": "string"}}})
    assert schema == {"redacted": True, "reason": "secret_material"}


def test_sanitize_rejects_noncanonical_values() -> None:
    with pytest.raises(ValidationError) as exc:
        sanitize_schema({"type": "number", "minimum": 0.5})
    assert exc.value.code == "non_canonical_value"


def test_sanitize_rejects_oversized_schema() -> None:
    with pytest.raises(ValidationError):
        sanitize_schema({"title": "x" * 40_000})


def test_projection_never_carries_config_or_output_schema() -> None:
    detail = project_detail(
        candidate(input_schema={"type": "object"}),
        decide_visibility(candidate(), harness_protocols=None, allowed_protocols=None),
    )
    assert set(detail) == {
        "id",
        "name",
        "version",
        "protocol",
        "status",
        "summary",
        "visible",
        "reason",
        "description",
        "inputSchema",
        "schemaRedactions",
    }


def test_summary_is_truncated() -> None:
    summary = project_summary(
        candidate(description="word " * 200),
        decide_visibility(candidate(), harness_protocols=None, allowed_protocols=None),
    )["summary"]
    assert len(summary) <= 200


# --- bounds -------------------------------------------------------------------


def test_query_and_limit_bounds() -> None:
    assert validate_query(None) == ""
    assert validate_query("  deploy  ") == "deploy"
    with pytest.raises(ValidationError) as exc:
        validate_query("x" * (MAX_QUERY_CHARS + 1))
    assert exc.value.code == "invalid_tool_query"

    assert clamp_tool_limit(None) == 25
    assert clamp_tool_limit(100) == 100
    for bad in (0, -1, 101):
        with pytest.raises(ValidationError):
            clamp_tool_limit(bad)
