"""Work declared by a task type for after a task of it is completed.

CP-ADR-0061, amendment 2026-09-25. A task type version may carry a
``completion_schema``: what core files once a task of that version has been
completed (moved into ``terminal_success``), by whoever completed it. The
vocabulary is the approval outcomes' one, narrowed to what makes sense after
the fact — ``ensureWork`` (with ``customFields``, ``relation`` and
``requestApproval``) and ``comment`` — and so is the expression grammar, minus
``$.approval`` and ``$.invocation``: there is no decision and no skill call
behind a completion.

Document shape::

    {"onComplete": {
        "when": ["$.task.artifact[commit].metadata.published",
                 "$.task.artifact[commit].metadata.branch"],
        "actions": [{"ensureWork": {...}}, {"comment": {...}}]
    }}

``when`` — expressions that must ALL resolve to something (not ``null``,
``""`` or ``false``) for the work to be filed; unmet, the completion simply
files nothing. Omitted, the work is filed on every completion.

Pure functions, no database and no I/O, like ``domain/approval_outcomes.py``.
"""

from dataclasses import dataclass
from typing import Any

from control_plane.domain.approval_outcomes import (
    COMMENT,
    ENSURE_WORK,
    Action,
    Path,
    check_actions,
    expressions_in,
    parse_path,
)
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import guard_json_document, reject_secret_material

INVALID_COMPLETION_SCHEMA = "invalid_completion_schema"

ON_COMPLETE = "onComplete"
COMPLETION_ACTIONS = frozenset({ENSURE_WORK, COMMENT})
# What a completion's expressions may read: the completed task and the task it
# was spawned by. No decision, no invocation.
COMPLETION_ROOTS = frozenset({"task", "spawnedBy"})
MAX_CONDITIONS = 8


@dataclass(frozen=True)
class CompletionSchema:
    when: tuple[Path, ...]
    actions: tuple[Action, ...]

    @property
    def empty(self) -> bool:
        return not self.actions


EMPTY_COMPLETION = CompletionSchema(when=(), actions=())


def _error(message: str, path: str) -> ValidationError:
    return ValidationError(INVALID_COMPLETION_SCHEMA, message, details={"path": path})


def _refuse_foreign_roots(action: Action, path: str) -> None:
    def strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for item in value.values() for s in strings(item)]
        return []

    for text in strings(action.inputs):
        for expression in expressions_in(text, where=path, invocation=True):
            if expression.root not in COMPLETION_ROOTS:
                raise _error(
                    f"{path}: {expression.text!r} — a completion reads only "
                    f"{sorted(COMPLETION_ROOTS)}",
                    path,
                )


def _check_when(raw: Any, path: str) -> tuple[Path, ...]:
    if not isinstance(raw, list) or not raw:
        raise _error(f"{path} must be a non-empty list of expressions", path)
    if len(raw) > MAX_CONDITIONS:
        raise _error(f"{path} allows at most {MAX_CONDITIONS} conditions", path)
    conditions: list[Path] = []
    for index, item in enumerate(raw):
        where = f"{path}[{index}]"
        if not isinstance(item, str):
            raise _error(f"{where} must be a $.path expression", where)
        try:
            condition = parse_path(item, where=where)
        except ValidationError as exc:
            raise _error(exc.message, where) from exc
        if condition.root not in COMPLETION_ROOTS or condition.truncate is not None:
            raise _error(
                f"{where}: a condition is one $.task/$.spawnedBy expression, no |truncate", where
            )
        conditions.append(condition)
    return tuple(conditions)


def parse_completion_schema(document: Any) -> CompletionSchema:
    """Validate a ``completion_schema`` document; ``{}`` means "nothing after completion".

    Errors carry ``invalid_completion_schema`` — whatever the shared action
    checker would call them — so a publisher knows which document to fix.
    """
    if document is None or document == {}:
        return EMPTY_COMPLETION
    if not isinstance(document, dict):
        raise _error("completionSchema must be an object", "completionSchema")
    guard_json_document(document, label="completionSchema")
    reject_secret_material(document, label="completionSchema")
    unknown = set(document) - {ON_COMPLETE}
    if unknown:
        raise _error(f"completionSchema: unknown keys {sorted(unknown)}", "completionSchema")
    section = document.get(ON_COMPLETE)
    if not isinstance(section, dict) or "actions" not in section:
        raise _error(f"{ON_COMPLETE} must be an object with 'actions'", ON_COMPLETE)
    unknown = set(section) - {"when", "actions"}
    if unknown:
        raise _error(f"{ON_COMPLETE}: unknown keys {sorted(unknown)}", ON_COMPLETE)
    when = _check_when(section["when"], f"{ON_COMPLETE}.when") if "when" in section else ()
    path = f"{ON_COMPLETE}.actions"
    raw = section["actions"]
    if not isinstance(raw, list) or not raw:
        raise _error(f"{path} must be a non-empty list of actions", path)
    try:
        actions = check_actions(raw, path, statuses=None, terminal=None)
    except ValidationError as exc:
        # The shared checker speaks of approval schemas; the publisher must
        # be told which of the version's documents to fix.
        raise ValidationError(INVALID_COMPLETION_SCHEMA, exc.message, details=exc.details) from exc
    for index, action in enumerate(actions):
        where = f"{path}[{index}]"
        if action.name not in COMPLETION_ACTIONS:
            raise _error(
                f"{where}: {action.name!r} is not available after completion; "
                f"expected {sorted(COMPLETION_ACTIONS)}",
                where,
            )
        _refuse_foreign_roots(action, where)
    return CompletionSchema(when=when, actions=actions)


def schema_of(document: Any) -> CompletionSchema:
    """The schema of an already-published (hence valid) document."""
    return parse_completion_schema(dict(document or {}))


def is_met(value: Any) -> bool:
    """A ``when`` condition holds: the expression resolved to something."""
    return value is not None and value != "" and value is not False
