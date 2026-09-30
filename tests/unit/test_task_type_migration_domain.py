"""Status mapping of a type migration (ADR-0048, amendment 2026-09-30)."""

from typing import Any

import pytest

from control_plane.domain.errors import ConflictError, ValidationError
from control_plane.domain.work_item import (
    migrated_status,
    parse_work_item_lifecycle,
    validate_status_map,
)

SOURCE = parse_work_item_lifecycle(
    {
        "initialStatus": "asked",
        "statuses": [
            {"key": "asked", "category": "backlog"},
            {"key": "waiting", "category": "blocked"},
            {"key": "answered", "category": "terminal_success"},
        ],
        "transitions": [{"from": "asked", "to": ["waiting", "answered"]}],
    }
)

TARGET = parse_work_item_lifecycle(
    {
        "initialStatus": "todo",
        "statuses": [
            {"key": "todo", "category": "active"},
            {"key": "review", "category": "active"},
            {"key": "parked", "category": "blocked"},
            {"key": "done", "category": "terminal_success"},
            {"key": "dropped", "category": "terminal_cancelled"},
        ],
        "transitions": [{"from": "todo", "to": ["review", "parked", "done", "dropped"]}],
    }
)


def test_no_map_is_an_empty_map() -> None:
    assert validate_status_map(SOURCE, TARGET, None) == {}
    assert validate_status_map(SOURCE, TARGET, {}) == {}


@pytest.mark.parametrize(
    "status_map",
    [
        [],
        "todo",
        {"asked": 1},
        {"asked": None},
        {"": "todo"},
    ],
)
def test_malformed_map_is_refused(status_map: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        validate_status_map(SOURCE, TARGET, status_map)
    assert caught.value.code == "invalid_status_map"


def test_map_value_must_be_declared_by_the_target() -> None:
    with pytest.raises(ValidationError) as caught:
        validate_status_map(SOURCE, TARGET, {"asked": "todo", "waiting": "blocked"})
    assert caught.value.code == "invalid_status_map"
    assert caught.value.details["field"] == "statusMap.waiting"
    assert "parked" in caught.value.details["known"]


@pytest.mark.parametrize("unknown", ["investigating", "ASKED", " asked"])
def test_map_key_must_be_declared_by_the_source(unknown: str) -> None:
    # A typo in a key would otherwise leave the tasks it meant silently unmapped.
    with pytest.raises(ValidationError) as caught:
        validate_status_map(SOURCE, TARGET, {"asked": "todo", unknown: "review"})
    assert caught.value.code == "invalid_status_map"
    assert caught.value.details["field"] == f"statusMap.{unknown}"
    assert caught.value.details["known"] == ["answered", "asked", "waiting"]


@pytest.mark.parametrize("terminal", ["done", "dropped"])
def test_map_value_must_not_be_terminal(terminal: str) -> None:
    # Checked for every entry, not only the one a task needs: a bulk migration
    # applies one map to many tasks.
    with pytest.raises(ValidationError) as caught:
        validate_status_map(SOURCE, TARGET, {"waiting": terminal})
    assert caught.value.code == "invalid_status_map"


def test_same_key_is_kept_without_a_map() -> None:
    assert migrated_status(TARGET, "review", {}) == "review"


def test_the_map_wins_over_the_same_key() -> None:
    assert migrated_status(TARGET, "review", {"review": "parked"}) == "parked"


def test_a_map_entry_for_another_status_does_not_apply() -> None:
    assert migrated_status(TARGET, "todo", {"asked": "review"}) == "todo"


def test_status_missing_in_the_target_is_a_conflict() -> None:
    with pytest.raises(ConflictError) as caught:
        migrated_status(TARGET, "investigating", {})
    assert caught.value.code == "incompatible_status"
    assert caught.value.details["statusKey"] == "investigating"
    # Only where the task may go: terminal statuses are not offered.
    assert caught.value.details["known"] == ["parked", "review", "todo"]


def test_same_key_that_is_terminal_in_the_target_is_a_conflict() -> None:
    # An open task whose status key is terminal in the new version would be
    # closed by the migration; the caller maps it instead.
    with pytest.raises(ConflictError) as caught:
        migrated_status(TARGET, "done", {})
    assert caught.value.code == "incompatible_status"
