"""Project Model domain rules, table-driven.

These are the contracts the rest of v0.5 leans on: the lifecycle graph
(ADR-0031), the layered effective config (ADR-0032) and the governance lattice
(ADR-0033). They are pure functions on purpose, so the exact merge order and
the exact "stricter" relation can be pinned by a table instead of being
inferred from an integration test.
"""

import pytest

from control_plane.domain.errors import ValidationError
from control_plane.domain.project import (
    GOVERNANCE_FIELDS,
    MAX_JSON_DEPTH,
    ProjectConfigSources,
    SystemStatusCategory,
    compute_effective_config,
    deep_merge,
    governance_violations,
    guard_json_document,
    locked_setting_violations,
    parse_lifecycle,
    reject_secret_material,
    stricter,
    validate_against_schema,
    validate_config_document,
    validate_governance,
    validate_json_schema_document,
)

# --- lifecycle ----------------------------------------------------------------

VALID_LIFECYCLE = {
    "initialStatus": "discovery",
    "statuses": [
        {"key": "discovery", "displayName": "Discovery", "category": "planned"},
        {"key": "pilot", "category": "active"},
        {"key": "on_hold", "category": "paused"},
        {"key": "completed", "category": "terminal_success"},
        {"key": "cancelled", "category": "terminal_cancelled"},
    ],
    "transitions": [
        {"from": "discovery", "to": ["pilot", "cancelled"]},
        {"from": "pilot", "to": ["on_hold", "completed", "cancelled"]},
        {"from": "on_hold", "to": ["pilot", "cancelled"]},
    ],
}


def test_lifecycle_parses_statuses_categories_and_transitions() -> None:
    lifecycle = parse_lifecycle(VALID_LIFECYCLE)
    assert lifecycle.initial_status == "discovery"
    assert lifecycle.category_of("pilot") == SystemStatusCategory.ACTIVE
    assert lifecycle.category_of("completed") == SystemStatusCategory.TERMINAL_SUCCESS
    assert lifecycle.display_names["pilot"] == "pilot"  # defaults to the key
    assert lifecycle.allows("discovery", "pilot")
    assert not lifecycle.allows("discovery", "completed")
    # A status with no declared transitions is terminal in practice.
    assert not lifecycle.allows("completed", "pilot")


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        ({"statuses": []}, "/statuses"),
        (
            {
                "statuses": [
                    {"key": "a", "category": "planned"},
                    {"key": "a", "category": "active"},
                ],
                "initialStatus": "a",
                "transitions": [],
            },
            "/statuses/1/key",
        ),
        (
            {
                "statuses": [{"key": "a", "category": "nonsense"}],
                "initialStatus": "a",
                "transitions": [],
            },
            "/statuses/0/category",
        ),
        ({"initialStatus": "missing"}, "/initialStatus"),
        (
            {"transitions": [{"from": "discovery", "to": ["ghost"]}]},
            "/transitions/0/to/0",
        ),
        (
            {"transitions": [{"from": "ghost", "to": ["pilot"]}]},
            "/transitions/0/from",
        ),
        (
            {
                "transitions": [
                    {"from": "discovery", "to": ["pilot"]},
                    {"from": "discovery", "to": ["cancelled"]},
                ]
            },
            "/transitions/1/from",
        ),
    ],
)
def test_lifecycle_rejects_broken_schemas(mutation: dict, path: str) -> None:
    schema = {**VALID_LIFECYCLE, **mutation}
    with pytest.raises(ValidationError) as excinfo:
        parse_lifecycle(schema)
    assert excinfo.value.code == "invalid_lifecycle_schema"
    assert excinfo.value.details["path"] == path


def test_every_system_category_is_reachable_from_the_vocabulary() -> None:
    lifecycle = parse_lifecycle(VALID_LIFECYCLE)
    assert set(lifecycle.categories.values()) == {c.value for c in SystemStatusCategory}


# --- JSON Schema and payload guards -------------------------------------------


def test_json_schema_document_must_be_a_valid_schema() -> None:
    validate_json_schema_document({"type": "object"}, field_name="fieldSchema")
    with pytest.raises(ValidationError) as excinfo:
        validate_json_schema_document({"type": 12}, field_name="fieldSchema")
    assert excinfo.value.code == "invalid_json_schema"


def test_instance_validation_reports_stable_paths() -> None:
    schema = {
        "type": "object",
        "properties": {"budget": {"type": "integer"}, "owner": {"type": "string"}},
        "required": ["owner"],
    }
    validate_against_schema(
        schema, {"owner": "team-a", "budget": 5}, code="custom_fields_invalid", field_name="cf"
    )
    with pytest.raises(ValidationError) as excinfo:
        validate_against_schema(
            schema, {"budget": "lots"}, code="custom_fields_invalid", field_name="cf"
        )
    assert excinfo.value.code == "custom_fields_invalid"
    paths = {error["path"] for error in excinfo.value.details["errors"]}
    assert "/budget" in paths  # the failing property, not just "invalid"


def test_payload_guards_reject_depth_and_size() -> None:
    deep: dict = {}
    node = deep
    for _ in range(MAX_JSON_DEPTH + 3):
        node["n"] = {}
        node = node["n"]
    with pytest.raises(ValidationError) as excinfo:
        guard_json_document(deep, label="config")
    assert excinfo.value.code == "payload_too_deep"

    with pytest.raises(ValidationError) as excinfo:
        guard_json_document({"blob": "x" * 200_000}, label="config")
    assert excinfo.value.code == "payload_too_large"

    with pytest.raises(ValidationError):
        guard_json_document(["not", "an", "object"], label="config")


@pytest.mark.parametrize(
    "document",
    [
        {"apiKey": "sk-live-123"},
        {"nested": {"password": "hunter2"}},
        {"list": [{"clientSecret": "x"}]},
        {"AUTHORIZATION": "Bearer x"},
    ],
)
def test_secret_material_is_rejected(document: dict) -> None:
    with pytest.raises(ValidationError) as excinfo:
        reject_secret_material(document, label="config")
    assert excinfo.value.code == "secret_material_rejected"
    assert excinfo.value.details["path"]


def test_secret_ref_is_the_sanctioned_escape_hatch() -> None:
    reject_secret_material({"connector": {"secretRef": "vault://x"}}, label="config")
    reject_secret_material({"connector": {"secret_ref": "vault://x"}}, label="config")


# --- config document ----------------------------------------------------------


def test_config_document_rejects_unknown_sections() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_config_document({"settings": {}, "connectors": {}})
    assert excinfo.value.code == "unknown_config_section"
    assert excinfo.value.details["unknown"] == ["connectors"]


def test_config_document_normalizes_missing_sections() -> None:
    config = validate_config_document({})
    assert config == {
        "settings": {},
        "views": [],
        "governance": {},
        "memory": {},
        "inheritance": {"inheritableSettings": ["*"], "lockedSettings": []},
    }


def test_deep_merge_replaces_arrays_and_scalars_but_merges_objects() -> None:
    base = {"a": {"x": 1, "y": 2}, "list": [1, 2], "scalar": "old"}
    overlay = {"a": {"y": 20, "z": 30}, "list": [9], "scalar": "new"}
    assert deep_merge(base, overlay) == {
        "a": {"x": 1, "y": 20, "z": 30},
        "list": [9],
        "scalar": "new",
    }


# --- governance ---------------------------------------------------------------


def test_governance_vocabulary_is_closed() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_governance({"maxSpend": 100})
    assert excinfo.value.code == "unknown_governance_field"
    assert set(excinfo.value.details["known"]) == set(GOVERNANCE_FIELDS)


def test_governance_normalizes_sets_order_independently() -> None:
    a = validate_governance({"allowedTaskPriorities": ["low", "high"]})
    b = validate_governance({"allowedTaskPriorities": ["high", "low", "high"]})
    assert a == b == {"allowedTaskPriorities": ["high", "low"]}


@pytest.mark.parametrize(
    ("field", "ancestor", "weaker", "stricter_value"),
    [
        ("maxAutonomyLevel", "assisted", "autonomous", "supervised"),
        ("requireApprovalForRun", True, False, True),
        ("requireApprovalForCompletion", True, False, True),
        (
            "allowedTaskPriorities",
            ["high", "medium"],
            ["critical", "high", "medium"],
            ["high"],
        ),
        ("allowedSkillProtocols", ["http", "mcp"], ["http", "local", "mcp"], ["http"]),
        ("maxRunDurationSeconds", 600, 1200, 300),
        ("maxRunDurationSeconds", 600, None, 300),
        ("maxRunActions", 50, 100, 10),
        ("maxConcurrentRuns", 2, 5, 1),
        ("memoryScopeSharing", "project", "ancestors", "none"),
    ],
)
def test_governance_weakening_is_detected_per_field(
    field: str, ancestor: object, weaker: object, stricter_value: object
) -> None:
    assert governance_violations({field: weaker}, {field: ancestor}) == [
        {"path": f"/governance/{field}", "ancestor": ancestor, "project": weaker}
    ]
    assert governance_violations({field: stricter_value}, {field: ancestor}) == []
    assert governance_violations({field: ancestor}, {field: ancestor}) == []


def test_governance_unbounded_ancestor_accepts_anything() -> None:
    # A null ceiling is the weakest value: a child may set any bound under it.
    assert governance_violations({"maxRunActions": 10}, {"maxRunActions": None}) == []
    assert governance_violations({"maxRunActions": None}, {"maxRunActions": None}) == []


def test_stricter_takes_the_tighter_side_field_by_field() -> None:
    folded = stricter(
        {"maxRunActions": 100, "requireApprovalForRun": False, "maxAutonomyLevel": "autonomous"},
        {"maxRunActions": 10, "requireApprovalForRun": True, "memoryScopeSharing": "none"},
    )
    assert folded == {
        "maxRunActions": 10,
        "requireApprovalForRun": True,
        "maxAutonomyLevel": "autonomous",
        "memoryScopeSharing": "none",
    }


def test_locked_setting_violations_are_sorted_and_exact() -> None:
    assert locked_setting_violations(["b", "a", "c"], frozenset({"a", "c"})) == ["a", "c"]
    assert locked_setting_violations(["x"], frozenset()) == []


# --- effective config ---------------------------------------------------------


def _sources(
    project_id: str,
    *,
    template_settings: dict | None = None,
    template_governance: dict | None = None,
    template_views: list | None = None,
    inheritance: dict | None = None,
    revision: int | None = None,
    revision_config: dict | None = None,
    profile_settings: dict | None = None,
) -> ProjectConfigSources:
    default_config: dict = {
        "settings": template_settings or {},
        "governance": template_governance or {},
        "memory": {},
    }
    if inheritance is not None:
        default_config["inheritance"] = inheritance
    return ProjectConfigSources(
        project_id=project_id,
        template_id=f"tpl-{project_id}",
        template_key="delivery",
        template_version=1,
        template_default_config=default_config,
        template_default_views=template_views or [],
        revision=revision,
        revision_config=revision_config,
        profile_settings=profile_settings or {},
    )


def test_layer_order_is_template_ancestor_revision_profile() -> None:
    """The documented precedence, pinned key by key (ADR-0032)."""
    parent = _sources(
        "parent",
        template_settings={"a": "template-parent", "b": "template-parent"},
        revision=1,
        revision_config={"settings": {"b": "parent-revision"}},
    )
    child = _sources(
        "child",
        template_settings={"a": "template-child", "c": "template-child", "d": "template-child"},
        revision=2,
        revision_config={"settings": {"c": "child-revision", "d": "child-revision"}},
        profile_settings={"d": "child-profile"},
    )
    effective = compute_effective_config([parent, child])

    settings = effective.config["settings"]
    # a/b come from the ancestor and override the child's TEMPLATE defaults.
    assert settings["a"] == "template-parent"
    assert settings["b"] == "parent-revision"
    # c is the child's own revision, d is overridden once more by the profile.
    assert settings["c"] == "child-revision"
    assert settings["d"] == "child-profile"

    provenance = effective.provenance["settings"]
    assert provenance["a"]["source"] == "ancestor"
    assert provenance["a"]["projectId"] == "parent"
    assert provenance["b"]["source"] == "ancestor"
    assert provenance["c"]["source"] == "revision"
    assert provenance["c"]["revision"] == 2
    assert provenance["d"]["source"] == "profile"
    assert [layer["projectId"] for layer in effective.provenance["layers"]] == ["parent", "child"]


def test_single_project_uses_template_then_revision_then_profile() -> None:
    effective = compute_effective_config(
        [
            _sources(
                "solo",
                template_settings={"a": 1, "b": 1, "c": 1},
                revision=3,
                revision_config={"settings": {"b": 2, "c": 2}},
                profile_settings={"c": 3},
            )
        ]
    )
    assert effective.config["settings"] == {"a": 1, "b": 2, "c": 3}
    assert effective.provenance["settings"]["a"]["source"] == "template"
    assert effective.provenance["settings"]["b"]["source"] == "revision"
    assert effective.provenance["settings"]["c"]["source"] == "profile"


def test_effective_config_is_independent_of_dict_ordering() -> None:
    """Same layers, different key insertion order -> identical result."""
    forward = compute_effective_config(
        [_sources("p", template_settings={"a": 1, "b": 2}, profile_settings={"b": 3, "a": 4})]
    )
    backward = compute_effective_config(
        [_sources("p", template_settings={"b": 2, "a": 1}, profile_settings={"a": 4, "b": 3})]
    )
    assert forward.config == backward.config


def test_governance_folds_stricter_down_the_chain() -> None:
    parent = _sources(
        "parent",
        revision=1,
        revision_config={"governance": {"maxRunActions": 50, "requireApprovalForRun": True}},
    )
    child = _sources(
        "child",
        revision=1,
        revision_config={"governance": {"maxRunActions": 10}},
    )
    effective = compute_effective_config([parent, child])
    assert effective.config["governance"] == {
        "maxRunActions": 10,
        "requireApprovalForRun": True,
    }
    # The ceiling the child had to fit under is reported separately.
    assert effective.inherited_governance == {
        "maxRunActions": 50,
        "requireApprovalForRun": True,
    }
    assert effective.provenance["governance"]["requireApprovalForRun"]["source"] == "ancestor"
    assert effective.provenance["governance"]["maxRunActions"]["source"] == "revision"


def test_a_lax_template_under_a_strict_ancestor_is_clamped_not_rejected() -> None:
    parent = _sources("parent", template_governance={"maxRunActions": 5})
    child = _sources("child", template_governance={"maxRunActions": 500})
    effective = compute_effective_config([parent, child])
    assert effective.config["governance"]["maxRunActions"] == 5


def test_inheritable_settings_restrict_what_flows_down() -> None:
    parent = _sources(
        "parent",
        template_settings={"shared": "yes", "private": "no"},
        inheritance={"inheritableSettings": ["shared"], "lockedSettings": []},
    )
    child = _sources("child")
    effective = compute_effective_config([parent, child])
    assert effective.config["settings"] == {"shared": "yes"}


def test_locked_settings_accumulate_from_every_ancestor() -> None:
    root = _sources("root", inheritance={"inheritableSettings": ["*"], "lockedSettings": ["tone"]})
    middle = _sources(
        "middle", inheritance={"inheritableSettings": ["*"], "lockedSettings": ["locale"]}
    )
    leaf = _sources("leaf")
    effective = compute_effective_config([root, middle, leaf])
    assert effective.locked_settings == frozenset({"tone", "locale"})
    # The project's own lock does not apply to itself, only to descendants.
    solo = compute_effective_config(
        [_sources("solo", inheritance={"inheritableSettings": ["*"], "lockedSettings": ["tone"]})]
    )
    assert solo.locked_settings == frozenset()


def test_views_are_replaced_wholesale_by_the_nearest_declaring_layer() -> None:
    parent = _sources("parent", template_views=[{"key": "overview"}])
    child_inherits = compute_effective_config([parent, _sources("child")])
    assert child_inherits.config["views"] == [{"key": "overview"}]

    child_overrides = compute_effective_config(
        [
            parent,
            _sources(
                "child", revision=1, revision_config={"views": [{"key": "work"}, {"key": "runs"}]}
            ),
        ]
    )
    assert child_overrides.config["views"] == [{"key": "work"}, {"key": "runs"}]


def test_empty_chain_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        compute_effective_config([])


# --- regressions from the v0.5 adversarial review -----------------------------


def test_governance_fold_is_a_true_meet_for_sets() -> None:
    """Incomparable sets must intersect, not pick a side (review finding).

    ``["http","mcp"]`` and ``["mcp","local"]`` have no stricter side. Keeping
    either one would let the result allow a value the other layer forbade.
    """
    folded = stricter(
        {"allowedSkillProtocols": ["http", "mcp"]},
        {"allowedSkillProtocols": ["mcp", "local"]},
    )
    assert folded == {"allowedSkillProtocols": ["mcp"]}

    effective = compute_effective_config(
        [
            _sources("parent", template_governance={"allowedSkillProtocols": ["http", "mcp"]}),
            _sources("child", template_governance={"allowedSkillProtocols": ["mcp", "local"]}),
        ]
    )
    assert effective.config["governance"]["allowedSkillProtocols"] == ["mcp"]


@pytest.mark.parametrize(
    ("field", "left", "right", "expected"),
    [
        ("maxAutonomyLevel", "assisted", "autonomous", "assisted"),
        ("requireApprovalForRun", False, True, True),
        ("allowedTaskPriorities", ["high", "low"], ["low", "medium"], ["low"]),
        ("maxRunActions", 10, 50, 10),
        ("maxRunActions", None, 50, 50),
        ("maxRunActions", 50, None, 50),
        ("memoryScopeSharing", "ancestors", "project", "project"),
    ],
)
def test_meet_is_commutative_and_picks_the_lower_bound(
    field: str, left: object, right: object, expected: object
) -> None:
    forward = stricter({field: left}, {field: right})
    backward = stricter({field: right}, {field: left})
    assert forward == backward == {field: expected}


def test_a_revision_cannot_silently_unlock_what_the_template_locked() -> None:
    """Locks accumulate within a project, not just down the chain (review)."""
    effective = compute_effective_config(
        [
            _sources(
                "parent",
                inheritance={"inheritableSettings": ["*"], "lockedSettings": ["tone"]},
                revision=1,
                revision_config={
                    "settings": {"a": 1},
                    # The config document always materializes this section, so
                    # "not mentioned" is indistinguishable from "empty".
                    "inheritance": {"inheritableSettings": ["*"], "lockedSettings": []},
                },
            ),
            _sources("child"),
        ]
    )
    assert effective.locked_settings == frozenset({"tone"})


def test_an_ancestor_without_views_does_not_blank_the_descendant() -> None:
    """No views declared is not the same as views declared empty (review)."""
    effective = compute_effective_config(
        [_sources("parent"), _sources("child", template_views=[{"key": "work"}])]
    )
    assert effective.config["views"] == [{"key": "work"}]


def test_provenance_keeps_the_originating_project_through_a_pass_through() -> None:
    """A value from the root must not be attributed to the middle ancestor."""
    effective = compute_effective_config(
        [
            _sources("root", template_settings={"tone": "formal"}),
            _sources("middle"),
            _sources("leaf"),
        ]
    )
    origin = effective.provenance["settings"]["tone"]
    assert origin["source"] == "ancestor"
    assert origin["projectId"] == "root"


def test_governance_is_rejected_inside_settings() -> None:
    """It belongs in a versioned revision, not the profile overlay (ADR-0033)."""
    with pytest.raises(ValidationError) as excinfo:
        validate_config_document({"settings": {"governance": {"maxRunActions": 1}}})
    assert excinfo.value.code == "governance_not_in_settings"


def test_a_stored_schema_may_not_point_outside_itself() -> None:
    """A remote $ref would make the validator fetch a URL (review finding)."""
    for schema in (
        {"$ref": "https://evil.example/schema.json"},
        {"properties": {"a": {"$ref": "http://169.254.169.254/latest/meta-data"}}},
        {"$defs": {"x": {"$dynamicRef": "https://evil.example/#x"}}},
    ):
        with pytest.raises(ValidationError) as excinfo:
            validate_json_schema_document(schema, field_name="fieldSchema")
        assert excinfo.value.code == "invalid_json_schema"

    # A same-document ref is the supported way to factor a schema.
    validate_json_schema_document(
        {"$defs": {"name": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/name"}}},
        field_name="fieldSchema",
    )


def test_an_unevaluatable_schema_is_a_422_not_a_500() -> None:
    """A stored schema with a dangling local $ref must not crash writes."""
    with pytest.raises(ValidationError) as excinfo:
        validate_against_schema(
            {"$ref": "#/$defs/missing"},
            {"a": 1},
            code="custom_fields_invalid",
            field_name="customFields",
        )
    assert excinfo.value.code == "invalid_json_schema"


def test_views_are_scanned_for_secrets_like_settings() -> None:
    with pytest.raises(ValidationError) as excinfo:
        validate_config_document({"views": [{"key": "board", "apiKey": "sk-live"}]})
    assert excinfo.value.code == "secret_material_rejected"
    validate_config_document({"views": [{"key": "board", "secretRef": "vault://x"}]})
