"""Work item lifecycle rules, table-driven (ADR-0048).

The point of these tests is that a task type cannot be created in a shape that
would strand the tasks carrying it. Everything checkable at type-creation time
is checked THERE, so ``:complete`` never has to guess.
"""

from datetime import UTC, datetime
from typing import Any

import pytest

from control_plane.domain.errors import ValidationError
from control_plane.domain.project import (
    MAX_CUSTOM_FIELD_BYTES,
    MAX_JSON_DEPTH,
    MAX_STATUSES,
    parse_lifecycle,
)
from control_plane.domain.work_item import (
    LEGACY_STATUS_CATEGORIES,
    MAX_COMMENT_BODY_LENGTH,
    SYSTEM_TASK_LIFECYCLE,
    WORK_ITEM_CATEGORIES,
    TransitionRoute,
    WorkItemStatusCategory,
    normalize_planned_date,
    parse_work_item_lifecycle,
    transition_targets,
    validate_comment_body,
    validate_planned_dates,
    validate_task_custom_fields,
)

SIMPLE: dict[str, Any] = {
    "initialStatus": "open",
    "statuses": [
        {"key": "open", "category": "active"},
        {"key": "closed", "category": "terminal_success"},
    ],
    "transitions": [{"from": "open", "to": ["closed"]}],
}


def _with(**overrides: Any) -> dict[str, Any]:
    return {**SIMPLE, **overrides}


def _error_code(exc: pytest.ExceptionInfo[ValidationError]) -> str:
    return exc.value.code


# --- the system type ----------------------------------------------------------


def test_system_lifecycle_parses_and_keeps_the_pre_v08_vocabulary() -> None:
    parsed = parse_work_item_lifecycle(SYSTEM_TASK_LIFECYCLE)

    assert parsed.statuses == LEGACY_STATUS_CATEGORIES
    assert parsed.initial_status == "todo"
    assert parsed.claim_status == "in_progress"
    assert parsed.release_status == "todo"
    assert parsed.completion_status == "done"


@pytest.mark.parametrize("source", ["backlog", "todo", "in_progress", "blocked"])
@pytest.mark.parametrize(
    "target", ["backlog", "todo", "in_progress", "blocked", "done", "cancelled"]
)
def test_system_lifecycle_allows_every_pre_v08_transition(source: str, target: str) -> None:
    """Pre-v0.8 PATCH accepted any status from any status; only self is new."""
    parsed = parse_work_item_lifecycle(SYSTEM_TASK_LIFECYCLE)

    assert parsed.allows(source, target) is (source != target)


def test_terminal_statuses_are_terminal() -> None:
    parsed = parse_work_item_lifecycle(SYSTEM_TASK_LIFECYCLE)

    assert parsed.is_terminal("done") and parsed.is_terminal("cancelled")
    assert not parsed.is_terminal("blocked")


# --- the two vocabularies do not mix ------------------------------------------


def test_project_category_is_not_a_work_item_category() -> None:
    schema = _with(statuses=[{"key": "open", "category": "paused"}, *SIMPLE["statuses"][1:]])

    with pytest.raises(ValidationError) as exc:
        parse_work_item_lifecycle(schema)

    assert _error_code(exc) == "invalid_lifecycle_schema"


def test_work_item_category_is_not_a_project_category() -> None:
    schema = _with(statuses=[{"key": "open", "category": "blocked"}, *SIMPLE["statuses"][1:]])

    with pytest.raises(ValidationError):
        parse_lifecycle(schema)  # project vocabulary by default


def test_work_item_vocabulary_is_exactly_five_categories() -> None:
    assert {c.value for c in WorkItemStatusCategory} == WORK_ITEM_CATEGORIES
    assert len(WORK_ITEM_CATEGORIES) == 5


# --- claimStatus / releaseStatus ----------------------------------------------


@pytest.mark.parametrize("field", ["claimStatus", "releaseStatus"])
def test_service_status_must_be_declared(field: str) -> None:
    with pytest.raises(ValidationError) as exc:
        parse_work_item_lifecycle(_with(**{field: "nowhere"}))

    assert _error_code(exc) == "invalid_lifecycle_schema"


@pytest.mark.parametrize("field", ["claimStatus", "releaseStatus"])
def test_service_status_must_not_be_terminal(field: str) -> None:
    """Claiming does not finish work; releasing a claim does not cancel it."""
    with pytest.raises(ValidationError) as exc:
        parse_work_item_lifecycle(_with(**{field: "closed"}))

    assert _error_code(exc) == "invalid_lifecycle_schema"
    assert "terminal" in exc.value.message


def test_service_statuses_are_optional() -> None:
    parsed = parse_work_item_lifecycle(SIMPLE)

    assert parsed.claim_status is None and parsed.release_status is None


# --- completionStatus ---------------------------------------------------------


def test_completion_status_is_inferred_when_unambiguous() -> None:
    assert parse_work_item_lifecycle(SIMPLE).completion_status == "closed"


def test_ambiguous_completion_status_is_refused() -> None:
    schema = _with(
        statuses=[
            {"key": "open", "category": "active"},
            {"key": "closed", "category": "terminal_success"},
            {"key": "shipped", "category": "terminal_success"},
        ]
    )

    with pytest.raises(ValidationError) as exc:
        parse_work_item_lifecycle(schema)

    assert "completionStatus" in exc.value.message


def test_ambiguous_completion_status_is_accepted_once_declared() -> None:
    schema = _with(
        statuses=[
            {"key": "open", "category": "active"},
            {"key": "closed", "category": "terminal_success"},
            {"key": "shipped", "category": "terminal_success"},
        ],
        completionStatus="shipped",
    )

    assert parse_work_item_lifecycle(schema).completion_status == "shipped"


def test_lifecycle_without_success_is_refused() -> None:
    schema = {
        "initialStatus": "open",
        "statuses": [
            {"key": "open", "category": "active"},
            {"key": "dropped", "category": "terminal_cancelled"},
        ],
        "transitions": [{"from": "open", "to": ["dropped"]}],
    }

    with pytest.raises(ValidationError) as exc:
        parse_work_item_lifecycle(schema)

    assert "terminal_success" in exc.value.message


def test_completion_status_must_be_a_success_status() -> None:
    schema = _with(
        statuses=[
            {"key": "open", "category": "active"},
            {"key": "closed", "category": "terminal_success"},
            {"key": "dropped", "category": "terminal_cancelled"},
        ],
        completionStatus="dropped",
    )

    with pytest.raises(ValidationError) as exc:
        parse_work_item_lifecycle(schema)

    assert "terminal_success" in exc.value.message


# --- transition projection ----------------------------------------------------


def test_targets_route_completion_through_the_complete_action() -> None:
    """What a reader is offered must match what the update path accepts."""
    parsed = parse_work_item_lifecycle(SYSTEM_TASK_LIFECYCLE)

    routes = {t.status: t.route for t in transition_targets(parsed, "in_progress")}

    assert routes == {
        "backlog": TransitionRoute.UPDATE,
        "todo": TransitionRoute.UPDATE,
        "blocked": TransitionRoute.UPDATE,
        "cancelled": TransitionRoute.UPDATE,
        "done": TransitionRoute.COMPLETE,
    }


def test_targets_carry_the_display_name_and_category_of_the_type() -> None:
    parsed = parse_work_item_lifecycle(SYSTEM_TASK_LIFECYCLE)

    blocked = next(t for t in transition_targets(parsed, "todo") if t.status == "blocked")

    assert blocked.display_name == "Blocked"
    assert blocked.category == WorkItemStatusCategory.BLOCKED


def test_a_terminal_status_offers_no_targets() -> None:
    parsed = parse_work_item_lifecycle(SYSTEM_TASK_LIFECYCLE)

    assert transition_targets(parsed, "done") == []


def test_a_declared_self_edge_is_not_offered() -> None:
    """The update path refuses "already in this status"; do not advertise it."""
    parsed = parse_work_item_lifecycle(
        _with(transitions=[{"from": "open", "to": ["open", "closed"]}])
    )

    assert [t.status for t in transition_targets(parsed, "open")] == ["closed"]


# --- payload guards apply before anything is parsed ---------------------------


def test_pathological_nesting_is_refused() -> None:
    deep: Any = {"key": "open"}
    for _ in range(MAX_JSON_DEPTH + 2):
        deep = {"nested": deep}

    with pytest.raises(ValidationError) as exc:
        parse_work_item_lifecycle(deep)

    assert _error_code(exc) == "payload_too_deep"


def test_too_many_statuses_are_refused() -> None:
    schema = _with(
        statuses=[{"key": f"s{i}", "category": "active"} for i in range(MAX_STATUSES + 1)]
    )

    with pytest.raises(ValidationError) as exc:
        parse_work_item_lifecycle(schema)

    assert _error_code(exc) == "invalid_lifecycle_schema"


# --- custom fields and planned dates (ADR-0049) -------------------------------

FIELD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "component": {"type": "string"},
        "blastRadius": {"type": "string", "enum": ["one_service", "platform"]},
    },
    "required": ["component"],
    "additionalProperties": False,
}


def test_custom_fields_matching_the_type_schema_are_accepted() -> None:
    validate_task_custom_fields(FIELD_SCHEMA, {"component": "iam", "blastRadius": "platform"})


def test_an_empty_field_schema_accepts_anything() -> None:
    # Every type created before WI-3 carries {} — those tasks must keep working.
    validate_task_custom_fields({}, {"whatever": [1, 2, 3]})


@pytest.mark.parametrize(
    "fields",
    [
        {},  # 'component' is required
        {"component": 42},  # wrong type
        {"component": "iam", "blastRadius": "galaxy"},  # not in the enum
        {"component": "iam", "extra": True},  # additionalProperties: false
    ],
)
def test_custom_fields_violating_the_schema_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_task_custom_fields(FIELD_SCHEMA, fields)

    assert _error_code(exc) == "custom_fields_invalid"


def test_secret_material_in_custom_fields_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_task_custom_fields({}, {"deploy": {"apiKey": "sk-live-1234"}})

    assert _error_code(exc) == "secret_material_rejected"


def test_an_opaque_secret_ref_is_still_allowed() -> None:
    validate_task_custom_fields({}, {"deploy": {"secretRef": "vault://kv/deploy"}})


def test_a_remote_ref_in_the_stored_schema_is_a_validation_error_not_a_fetch() -> None:
    # jsonschema would RESOLVE an absolute $ref by fetching it. A type cannot be
    # created with one (validate_json_schema_document), and if one ever reached
    # storage the instance check must fail closed rather than make a request.
    with pytest.raises(ValidationError) as exc:
        validate_task_custom_fields({"$ref": "https://example.test/schema.json"}, {"a": 1})

    assert _error_code(exc) == "invalid_json_schema"


def test_an_oversized_custom_fields_document_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_task_custom_fields({}, {"blob": "x" * (MAX_CUSTOM_FIELD_BYTES + 1)})

    assert _error_code(exc) == "payload_too_large"


def test_a_naive_planned_date_is_read_as_utc() -> None:
    assert normalize_planned_date(datetime(2026, 8, 14, 12, 0)) == datetime(
        2026, 8, 14, 12, 0, tzinfo=UTC
    )


def test_an_aware_planned_date_is_left_alone() -> None:
    moment = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    assert normalize_planned_date(moment) is moment
    assert normalize_planned_date(None) is None


def test_a_planned_interval_may_be_open_ended_or_a_single_instant() -> None:
    moment = datetime(2026, 8, 14, tzinfo=UTC)

    validate_planned_dates(None, None)
    validate_planned_dates(moment, None)
    validate_planned_dates(None, moment)
    validate_planned_dates(moment, moment)


def test_a_planned_interval_must_not_run_backwards() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_planned_dates(datetime(2026, 8, 20, tzinfo=UTC), datetime(2026, 8, 14, tzinfo=UTC))

    assert _error_code(exc) == "invalid_planned_dates"


# --- comment bodies (ADR-0050) ------------------------------------------------


def test_a_comment_body_is_stored_stripped() -> None:
    """Whitespace-only differences must not read as a new version of a reply."""
    assert validate_comment_body("  Ship it on Thursday \n") == "Ship it on Thursday"


@pytest.mark.parametrize("body", ["", "   ", "\n\t "])
def test_an_empty_comment_is_refused(body: str) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_comment_body(body)

    assert _error_code(exc) == "invalid_comment_body"


def test_a_comment_longer_than_the_cap_is_refused() -> None:
    with pytest.raises(ValidationError) as exc:
        validate_comment_body("x" * (MAX_COMMENT_BODY_LENGTH + 1))

    assert _error_code(exc) == "payload_too_large"


@pytest.mark.parametrize(
    "body",
    [
        "key is sk-0123456789abcdefghij",
        "ghp_0123456789abcdefghij0123456789",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.dBjftJeZ4CVPmB92K27uhbUJU1p1r",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-0123456789-abcdefgh",
        "password = correcthorsebatterystaple",
        'api_key: "0123456789abcdef0123"',
    ],
)
def test_credential_shaped_material_is_refused(body: str) -> None:
    with pytest.raises(ValidationError) as exc:
        validate_comment_body(body)

    assert _error_code(exc) == "secret_material_rejected"


@pytest.mark.parametrize(
    "body",
    [
        "Rotate the API key before Friday.",
        "The password policy needs a decision from security.",
        "Ask Ops for the token; do not paste it here.",
        "secret: ask Dana",
        "We store credentials in Vault, referenced by secretRef.",
    ],
)
def test_talking_about_credentials_is_not_refused(body: str) -> None:
    """A guard that silences the discussion of secrets is worse than useless."""
    assert validate_comment_body(body) == body.strip()
