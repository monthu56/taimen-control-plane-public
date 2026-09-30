"""Work item domain rules: status categories and the task lifecycle.

Pure functions over plain dicts, no database and no I/O — the same shape as
``domain/project.py``, and deliberately reusing its lifecycle parser.

The one thing that must not be re-derived anywhere else: core decisions about a
task — claimability, dependency readiness, completion — read the SYSTEM
CATEGORY, never the tenant's status key (ADR-0048). A tenant may call a status
``done`` and give it category ``active``; core will treat it as active, and
that is the correct behaviour, not a bug.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from control_plane.domain.errors import ConflictError, ValidationError
from control_plane.domain.project import (
    Lifecycle,
    parse_lifecycle,
    reject_secret_material,
    validate_against_schema,
)
from control_plane.domain.redaction import reject_secret_text


class WorkItemStatusCategory(StrEnum):
    """The only status vocabulary core ever branches on for a work item."""

    BACKLOG = "backlog"
    ACTIVE = "active"
    BLOCKED = "blocked"
    TERMINAL_SUCCESS = "terminal_success"
    TERMINAL_CANCELLED = "terminal_cancelled"


WORK_ITEM_CATEGORIES = frozenset(c.value for c in WorkItemStatusCategory)
TERMINAL_CATEGORIES = frozenset(
    {WorkItemStatusCategory.TERMINAL_SUCCESS, WorkItemStatusCategory.TERMINAL_CANCELLED}
)

# The per-tenant fallback type. Unlike ``workspace_types`` there is no
# ``is_system`` column: the table is versioned, and neither a unique index on
# (tenant) nor on (tenant, version) expresses "at most one system KEY". The
# guarantee that matters comes from immutability instead — creating version 2
# of this key touches no existing task, because tasks pin a version by id.
SYSTEM_TASK_TYPE_KEY = "task"
SYSTEM_TASK_TYPE_DISPLAY_NAME = "Task"

# The six statuses that lived in the ``tasks.status`` CHECK constraint before
# ADR-0048, with the transition graph that keeps pre-v0.8 behaviour: complete
# among the four non-terminal statuses, plus an edge into each terminal one.
SYSTEM_TASK_LIFECYCLE: dict[str, Any] = {
    "initialStatus": "todo",
    "statuses": [
        {"key": "backlog", "displayName": "Backlog", "category": "backlog"},
        {"key": "todo", "displayName": "To do", "category": "active"},
        {"key": "in_progress", "displayName": "In progress", "category": "active"},
        {"key": "blocked", "displayName": "Blocked", "category": "blocked"},
        {"key": "done", "displayName": "Done", "category": "terminal_success"},
        {"key": "cancelled", "displayName": "Cancelled", "category": "terminal_cancelled"},
    ],
    "transitions": [
        {"from": "backlog", "to": ["todo", "in_progress", "blocked", "done", "cancelled"]},
        {"from": "todo", "to": ["backlog", "in_progress", "blocked", "done", "cancelled"]},
        {"from": "in_progress", "to": ["backlog", "todo", "blocked", "done", "cancelled"]},
        {"from": "blocked", "to": ["backlog", "todo", "in_progress", "done", "cancelled"]},
    ],
    "claimStatus": "in_progress",
    "releaseStatus": "todo",
    "completionStatus": "done",
}

# Pre-v0.8 status key -> system category. Used by the migration backfill and by
# nothing else: after the migration the mapping lives in a lifecycle document.
LEGACY_STATUS_CATEGORIES: dict[str, str] = {
    "backlog": WorkItemStatusCategory.BACKLOG.value,
    "todo": WorkItemStatusCategory.ACTIVE.value,
    "in_progress": WorkItemStatusCategory.ACTIVE.value,
    "blocked": WorkItemStatusCategory.BLOCKED.value,
    "done": WorkItemStatusCategory.TERMINAL_SUCCESS.value,
    "cancelled": WorkItemStatusCategory.TERMINAL_CANCELLED.value,
}

# The reverse direction, for downgrade only: a tenant-authored key has no place
# in the six-value CHECK, so it collapses onto the legacy key of its category.
# Lossy by construction — see the revision docstring.
LEGACY_CATEGORY_STATUSES: dict[str, str] = {
    WorkItemStatusCategory.BACKLOG.value: "backlog",
    WorkItemStatusCategory.ACTIVE.value: "todo",
    WorkItemStatusCategory.BLOCKED.value: "blocked",
    WorkItemStatusCategory.TERMINAL_SUCCESS.value: "done",
    WorkItemStatusCategory.TERMINAL_CANCELLED.value: "cancelled",
}


# --- custom fields and planned dates (ADR-0049) -------------------------------


def validate_task_custom_fields(
    field_schema: dict[str, Any], fields: dict[str, Any], *, field_name: str = "customFields"
) -> None:
    """Validate a task's ``custom_fields`` against the ``field_schema`` of its type.

    Two checks, in this order and not the other one: the schema pass runs
    ``guard_json_document`` first, so a pathological document is refused before
    anything walks it; the secret scan then refuses obviously-named credentials.
    Unlike a project profile, a work item gets the scan on its custom fields as
    well as on config — a task is edited by agents far more often than a project
    profile is, so it is the likelier place for a token to be pasted by mistake.
    """
    validate_against_schema(
        field_schema, fields, code="custom_fields_invalid", field_name=field_name
    )
    reject_secret_material(fields, label=field_name)


def normalize_planned_date(value: datetime | None) -> datetime | None:
    """A naive datetime is read as UTC, so ordering never depends on the client.

    The column is ``timestamptz``; letting a naive value through would make the
    stored instant depend on the server's session timezone and would make the
    ``start <= due`` comparison below compare aware with naive and raise.
    """
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def validate_planned_dates(start_date: datetime | None, due_date: datetime | None) -> None:
    """A planned interval must not run backwards (the database checks it too)."""
    if start_date is not None and due_date is not None and start_date > due_date:
        raise ValidationError(
            "invalid_planned_dates",
            "startDate must not be after dueDate",
            details={"startDate": start_date.isoformat(), "dueDate": due_date.isoformat()},
        )


# --- comments (ADR-0050) ------------------------------------------------------

# Long enough for a real hand-off note, short enough that pasting a transcript
# is not the path of least resistance. A comment is coordination; the work
# product belongs in an Artifact.
MAX_COMMENT_BODY_LENGTH = 10_000


def validate_comment_body(body: str, *, field_name: str = "body") -> str:
    """Normalize and check a comment body; returns the text that will be stored.

    Trailing whitespace is stripped so that an "edit" which only changes
    invisible characters is not recorded as a new version, and an empty comment
    is refused rather than stored as a blank line in the thread.
    """
    normalized = body.strip()
    if not normalized:
        raise ValidationError(
            "invalid_comment_body",
            f"{field_name} must not be empty",
            details={"field": field_name},
        )
    if len(normalized) > MAX_COMMENT_BODY_LENGTH:
        raise ValidationError(
            "payload_too_large",
            f"{field_name} exceeds the {MAX_COMMENT_BODY_LENGTH}-character limit",
            details={
                "field": field_name,
                "maxLength": MAX_COMMENT_BODY_LENGTH,
                "actualLength": len(normalized),
            },
        )
    reject_secret_text(normalized, code="secret_material_rejected", subject=field_name)
    return normalized


def _lifecycle_error(message: str, path: str) -> ValidationError:
    return ValidationError(
        "invalid_lifecycle_schema", message, details={"field": "lifecycleSchema", "path": path}
    )


@dataclass(frozen=True)
class WorkItemLifecycle:
    """A parsed task lifecycle plus the three statuses core moves through.

    ``claim_status`` and ``release_status`` are what today's hardcoded
    ``'in_progress'`` / ``'todo'`` become once the vocabulary is a tenant's to
    choose; ``completion_status`` is where ``:complete`` lands.
    """

    lifecycle: Lifecycle
    claim_status: str | None
    release_status: str | None
    completion_status: str

    @property
    def initial_status(self) -> str:
        return self.lifecycle.initial_status

    @property
    def statuses(self) -> dict[str, str]:
        return self.lifecycle.categories

    def category_of(self, status_key: str) -> str:
        return self.lifecycle.category_of(status_key)

    def declares(self, status_key: str) -> bool:
        return status_key in self.lifecycle.categories

    def allows(self, from_status: str, to_status: str) -> bool:
        return self.lifecycle.allows(from_status, to_status)

    def targets_from(self, status_key: str) -> list[str]:
        return sorted(self.lifecycle.transitions.get(status_key, frozenset()))

    def is_terminal(self, status_key: str) -> bool:
        return self.category_of(status_key) in TERMINAL_CATEGORIES

    def display_name_of(self, status_key: str) -> str:
        return self.lifecycle.display_names[status_key]


class TransitionRoute(StrEnum):
    """Which action performs a declared transition.

    Completion is not a status write: it releases the claim, settles a running
    run and stamps ``completed_at``, so a lifecycle edge into a
    ``terminal_success`` status is walked by ``:complete`` and refused by
    ``PATCH``. Readers need that distinction BEFORE they write, which is why it
    lives here and not inside the update path alone.
    """

    UPDATE = "update"
    COMPLETE = "complete"


@dataclass(frozen=True)
class TransitionTarget:
    """One status reachable from the current one, and how to get there."""

    status: str
    display_name: str
    category: str
    route: TransitionRoute


def transition_route(lifecycle: WorkItemLifecycle, target_status: str) -> TransitionRoute:
    if lifecycle.category_of(target_status) == WorkItemStatusCategory.TERMINAL_SUCCESS:
        return TransitionRoute.COMPLETE
    return TransitionRoute.UPDATE


def transition_targets(lifecycle: WorkItemLifecycle, status_key: str) -> list[TransitionTarget]:
    """The declared targets reachable from ``status_key``, each with its route.

    A self-edge is dropped: a lifecycle may declare one, but "already in this
    status" is refused by the update path, so offering it would advertise a
    move that cannot happen.
    """
    return [
        TransitionTarget(
            status=target,
            display_name=lifecycle.display_name_of(target),
            category=lifecycle.category_of(target),
            route=transition_route(lifecycle, target),
        )
        for target in lifecycle.targets_from(status_key)
        if target != status_key
    ]


# --- moving a task to another version of its type (ADR-0048, amendment 2026-09-30)


def validate_status_map(
    source: WorkItemLifecycle, target: WorkItemLifecycle, status_map: Any
) -> dict[str, str]:
    """``statusMap`` of a type migration: status of ``source`` -> status of ``target``.

    A key the source version does not declare is refused rather than ignored:
    a typo there would otherwise silently leave the tasks unmapped.

    Every value is checked, not only the one the task needs: a bulk migration
    applies one map to many tasks, and a map that would strand some of them in
    an undeclared status is wrong as a whole. A value must be non-terminal —
    a migration is not a way to close work.
    """
    if status_map is None:
        return {}
    if not isinstance(status_map, dict):
        raise ValidationError("invalid_status_map", "statusMap must be an object")
    result: dict[str, str] = {}
    for key, value in status_map.items():
        if not isinstance(key, str) or not key or not isinstance(value, str):
            raise ValidationError(
                "invalid_status_map",
                "statusMap maps status keys to status keys",
                details={"field": f"statusMap.{key}"},
            )
        if not source.declares(key):
            raise ValidationError(
                "invalid_status_map",
                f"statusMap: {key!r} is not declared by the source version",
                details={
                    "field": f"statusMap.{key}",
                    "statusKey": key,
                    "known": sorted(source.statuses),
                },
            )
        if not target.declares(value):
            raise ValidationError(
                "invalid_status_map",
                f"statusMap.{key}: {value!r} is not declared by the target version",
                details={
                    "field": f"statusMap.{key}",
                    "statusKey": value,
                    "known": sorted(target.statuses),
                },
            )
        if target.is_terminal(value):
            raise ValidationError(
                "invalid_status_map",
                f"statusMap.{key}: {value!r} is terminal; a migration does not close work",
                details={"field": f"statusMap.{key}", "statusKey": value},
            )
        result[key] = value
    return result


def migrated_status(target: WorkItemLifecycle, current: str, status_map: dict[str, str]) -> str:
    """The status a task in ``current`` gets under ``target``.

    The explicit mapping wins; otherwise the same key, if ``target`` declares
    it as a non-terminal status. Anything else is ``409 incompatible_status``:
    the caller has to say where the task goes, core does not guess.
    """
    status = status_map.get(current, current)
    if target.declares(status) and not target.is_terminal(status):
        return status
    raise ConflictError(
        "incompatible_status",
        f"Status {current!r} has no non-terminal counterpart in the target version; "
        "map it in statusMap",
        details={
            "statusKey": current,
            "known": sorted(k for k in target.statuses if not target.is_terminal(k)),
        },
    )


def parse_work_item_lifecycle(schema: Any) -> WorkItemLifecycle:
    """Validate and parse a task type's ``lifecycle_schema``.

    Everything that can make a lifecycle unusable is rejected HERE, at type
    creation, rather than later when a task tries to move: a type with no
    successful ending does not describe work, and discovering that at
    ``:complete`` time would strand whatever already carries the type.
    """
    lifecycle = parse_lifecycle(schema, valid_categories=WORK_ITEM_CATEGORIES)

    claim_status = _optional_status(schema, lifecycle, "claimStatus")
    release_status = _optional_status(schema, lifecycle, "releaseStatus")
    completion_status = _completion_status(schema, lifecycle)

    return WorkItemLifecycle(
        lifecycle=lifecycle,
        claim_status=claim_status,
        release_status=release_status,
        completion_status=completion_status,
    )


def _optional_status(schema: Any, lifecycle: Lifecycle, field: str) -> str | None:
    """``claimStatus`` / ``releaseStatus``: declared, and never terminal."""
    value = schema.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or value not in lifecycle.categories:
        raise _lifecycle_error(f"{field} must name one of the declared statuses", f"/{field}")
    if lifecycle.category_of(value) in TERMINAL_CATEGORIES:
        # Claiming a task does not finish it, and releasing a claim does not
        # cancel it. A terminal value here would let coordination close work.
        raise _lifecycle_error(f"{field} must not be a terminal status", f"/{field}")
    return value


def _completion_status(schema: Any, lifecycle: Lifecycle) -> str:
    successes = sorted(
        key
        for key, category in lifecycle.categories.items()
        if category == WorkItemStatusCategory.TERMINAL_SUCCESS
    )
    declared = schema.get("completionStatus")
    if declared is not None:
        if not isinstance(declared, str) or declared not in lifecycle.categories:
            raise _lifecycle_error(
                "completionStatus must name one of the declared statuses", "/completionStatus"
            )
        if lifecycle.category_of(declared) != WorkItemStatusCategory.TERMINAL_SUCCESS:
            raise _lifecycle_error(
                "completionStatus must have category 'terminal_success'", "/completionStatus"
            )
        return declared
    if not successes:
        raise _lifecycle_error(
            "a work item lifecycle must declare a status with category 'terminal_success'",
            "/statuses",
        )
    if len(successes) > 1:
        raise _lifecycle_error(
            "completionStatus must be declared when several statuses are 'terminal_success'",
            "/completionStatus",
        )
    return successes[0]
