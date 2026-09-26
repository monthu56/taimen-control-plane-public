"""Approval outcomes declared by a task type (TAI-ADR-0041, CP-ADR-0061).

A task type version may carry an ``approval_schema``: what happens after an
approval on a task of that type is decided. Core does not know what the
actions *mean* for a domain (merging a branch, publishing a release); it only
executes a closed vocabulary of generic actions over its own entities, in the
declared order, with the authority of the principal who decided.

Document shape::

    {"gates": {"default": {"outcomes": {
        "approved": [{"completeTask": {}}],
        "rejected": [{"ensureWork": {...}}, {"completeTask": {}}]
    }}}}

Each action is a single-key object: the key names the action, the value holds
its inputs. String inputs may reference the decision context through a
restricted JSON-path (``$.task.publicId``, ``$.approval.comment``,
``$.spawnedBy.artifact[commit].metadata.branch``, ``$.task.customFields.branch``):
a string that is exactly one expression takes the raw value, any other string
is a template in which every expression is replaced by its text. An expression
followed by ``!`` is required: if it resolves to nothing (``null`` or an empty
string) the action fails with ``unresolved_expression`` instead of running with
a blank. ``|truncate:N`` caps a text value at N characters. There is no code
and there are no calls — the whole grammar is the whitelist below, and a
document that steps outside it is refused when the version is published, not
when an approval is decided.

``ensureWork`` may also set the new work item's ``customFields`` (each value a
string of the same grammar, checked against the target type's
``field_schema`` when the action runs) and ``requestApproval`` a gate on it
(CP-ADR-0061, amendment 2026-09-25) — which is also what a type's work after
completion (``domain/completion_work.py``) is made of.

``invokeSkill`` queues a skill invocation and counts as executed once it is
queued. Its ``onSuccess`` and ``onFailure`` actions (the same vocabulary,
minus ``invokeSkill``) react to how the invocation ended: they run after the
outcome's own actions, once every invocation of the outcome has finished —
``onSuccess`` if it succeeded as ``expect`` says, ``onFailure`` otherwise —
and may read the invocation as ``$.invocation.…``. Closing the gated task
belongs there, not next to the ``invokeSkill``: the approval is the basis of
an external write only while its task is open.

A gate may also declare ``preconditions`` of the ``approved`` decision
(TAI-ADR-0041 p.7): each names the newest external observation (CP-ADR-0057)
of a kind about the task, the task it was spawned by or an external object,
and a condition over it in the language of the work rules (CP-ADR-0063)::

    {"gates": {"default": {
        "preconditions": {"approved": [
            {"observation": {"kind": "ci.status", "task": "$.spawnedBy.id"},
             "condition": {"eq": [{"var": "observation.data.conclusion"}, "success"]},
             "reason": "CI of $.spawnedBy.publicId is not green"}
        ]},
        "outcomes": {...}
    }}}

While one does not hold, ``approve`` is refused with
``approval_precondition_failed`` — the decision is not recorded at all.

Pure functions, no database and no I/O, like ``domain/work_item.py``.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from control_plane.domain.enums import TaskPriority, TaskRelationType
from control_plane.domain.errors import ValidationError
from control_plane.domain.project import guard_json_document, reject_secret_material

DEFAULT_GATE = "default"
OUTCOMES = ("approved", "rejected")
MAX_GATES = 16
MAX_ACTIONS_PER_OUTCOME = 20
MAX_INPUT_LENGTH = 10_000
MAX_TRUNCATE = 100_000

ENSURE_WORK = "ensureWork"
COMPLETE_TASK = "completeTask"
COMMENT = "comment"
TRANSITION = "transition"
INVOKE_SKILL = "invokeSkill"

# A required (``!``) expression resolved to nothing when the action ran.
UNRESOLVED_EXPRESSION = "unresolved_expression"
# ``ensureWork.customFields`` rendered into fields the target type's
# ``field_schema`` refuses: the same code the API answers a create with.
CUSTOM_FIELDS_INVALID = "custom_fields_invalid"

# ``approve`` refused: a precondition of the decision does not hold (409).
APPROVAL_PRECONDITION_FAILED = "approval_precondition_failed"

# Preconditions (TAI-ADR-0041 p.7): only a decision that sets something in
# motion may wait for the world; ``reject`` stays always possible.
PRECONDITION_OUTCOMES = ("approved",)
MAX_PRECONDITIONS = 10
MAX_REASON_LENGTH = 1_000
# The single root a precondition's ``condition`` reads.
OBSERVATION_ROOT = "observation"
_OBSERVATION_KIND = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_OBSERVATION_SOURCE = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")

# ``ensureWork`` inputs (CP-ADR-0061, amendment 2026-09-25).
CUSTOM_FIELDS = "customFields"
REQUEST_APPROVAL = "requestApproval"
MAX_CUSTOM_FIELDS = 32

_GATE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,62}$")
_TYPE_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
# ``name`` or ``name@version``; the version is pinned by a published type
# version, which is what makes an outcome's skill as immutable as the type.
_SKILL_REF = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,126}(?:@[A-Za-z0-9][A-Za-z0-9_.+-]{0,63})?$")
_OUTPUT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_WORK_KEY = re.compile(r"^[A-Za-z0-9_.:/$\[\]! -]{1,200}$")
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# --- expressions --------------------------------------------------------------

# One expression inside a string: ``$.`` + root + segments + an optional ``!``
# (required) + an optional ``|truncate:N``. Segments are either ``.name`` or
# ``[name]``; what they may spell is decided by ``parse_path``.
_EXPRESSION = re.compile(
    r"\$\.(?P<root>[A-Za-z]+)(?P<rest>(?:\.[A-Za-z_][A-Za-z0-9_]*|\[[A-Za-z0-9_.:-]+\])*)"
    r"(?P<required>!)?(?:\|truncate:(?P<truncate>[0-9]+))?"
)
_SEGMENT = re.compile(r"\.(?P<field>[A-Za-z_][A-Za-z0-9_]*)|\[(?P<index>[A-Za-z0-9_.:-]+)\]")

TASK_FIELDS = frozenset(
    {"id", "publicId", "title", "description", "assigneeId", "workspaceId", "status", "priority"}
)
APPROVAL_FIELDS = frozenset({"id", "comment", "decidedBy", "decidedAt", "outcome"})
# ``$.invocation`` — the skill invocation a reaction (onSuccess/onFailure) reacts to:
# these fields, ``output.<key>`` and ``error.<code|message>``.
INVOCATION_FIELDS = frozenset({"id", "skill", "status"})
INVOCATION_ERROR_FIELDS = frozenset({"code", "message"})
# Roots that denote a task, and so may be followed by ``artifact[<type>]`` or
# ``customFields.<key>``.
TASK_ROOTS = frozenset({"task", "spawnedBy"})
INVOCATION_ROOT = "invocation"
# Reactions of an ``invokeSkill`` to how its invocation ended.
ON_SUCCESS = "onSuccess"
ON_FAILURE = "onFailure"
REACTIONS = (ON_SUCCESS, ON_FAILURE)
ROOTS = TASK_ROOTS | {"approval", INVOCATION_ROOT}


@dataclass(frozen=True)
class Path:
    """A parsed expression.

    ``root`` + ``field`` (+ ``key`` for a two-level field such as
    ``customFields.<key>`` or ``output.<key>``), or
    ``root`` + ``artifact[type].metadata.<metadata_key>``.
    """

    text: str
    root: str
    field: str | None = None
    artifact_type: str | None = None
    metadata_key: str | None = None
    required: bool = False
    key: str | None = None
    truncate: int | None = None


def _expression_error(expression: str, where: str, reason: str) -> ValidationError:
    return ValidationError(
        "invalid_approval_schema",
        f"Unsupported expression {expression!r} at {where}: {reason}",
        details={"path": where, "expression": expression},
    )


def parse_path(expression: str, *, where: str = "", invocation: bool = False) -> Path:
    """Parse one ``$.…`` expression against the closed grammar.

    ``invocation`` — whether ``$.invocation`` is in scope (only inside the
    ``onSuccess``/``onFailure`` actions of an ``invokeSkill``).
    """
    match = _EXPRESSION.fullmatch(expression)
    if match is None:
        raise _expression_error(expression, where, "not a $.path expression")
    root = match.group("root")
    required = match.group("required") is not None
    truncate: int | None = None
    if match.group("truncate") is not None:
        truncate = int(match.group("truncate"))
        if not 1 <= truncate <= MAX_TRUNCATE:
            raise _expression_error(expression, where, f"truncate must be 1..{MAX_TRUNCATE}")
    roots = ROOTS if invocation else ROOTS - {INVOCATION_ROOT}
    if root not in roots:
        raise _expression_error(expression, where, f"unknown root; expected {sorted(roots)}")
    segments: list[tuple[str | None, str | None]] = [
        (seg.group("field"), seg.group("index")) for seg in _SEGMENT.finditer(match.group("rest"))
    ]
    names = [name for name, _ in segments]

    def path(**parts: Any) -> Path:
        return Path(text=expression, root=root, required=required, truncate=truncate, **parts)

    if root == INVOCATION_ROOT:
        if len(segments) == 1 and names[0] in INVOCATION_FIELDS:
            return path(field=names[0])
        if len(segments) == 2 and names[0] == "output" and names[1] is not None:
            return path(field="output", key=names[1])
        if len(segments) == 2 and names[0] == "error" and names[1] in INVOCATION_ERROR_FIELDS:
            return path(field="error", key=names[1])
        raise _expression_error(
            expression,
            where,
            f"expected $.invocation.<{'|'.join(sorted(INVOCATION_FIELDS))}>, "
            "$.invocation.output.<field> or $.invocation.error.<code|message>",
        )
    fields = APPROVAL_FIELDS if root == "approval" else TASK_FIELDS
    if len(segments) == 1 and names[0] in fields:
        return path(field=names[0])
    if root in TASK_ROOTS and len(segments) == 2 and names[0] == "customFields" and names[1]:
        return path(field="customFields", key=names[1])
    if (
        root in TASK_ROOTS
        and len(segments) == 4
        and segments[0] == ("artifact", None)
        and segments[1][1] is not None
        and segments[2] == ("metadata", None)
        and segments[3][0] is not None
    ):
        return path(artifact_type=segments[1][1], metadata_key=segments[3][0])
    raise _expression_error(
        expression,
        where,
        f"expected $.{root}.<{'|'.join(sorted(fields))}>"
        + (
            f", $.{root}.customFields.<key> or $.{root}.artifact[<type>].metadata.<field>"
            if root in TASK_ROOTS
            else ""
        ),
    )


def expressions_in(value: str, *, where: str = "", invocation: bool = False) -> list[Path]:
    """Every expression a string input references, validated."""
    return [
        parse_path(m.group(0), where=where, invocation=invocation)
        for m in _EXPRESSION.finditer(value)
    ]


def _truncated(value: Any, limit: int | None) -> Any:
    if limit is None or not isinstance(value, str) or len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def render(value: Any, resolve: Callable[[Path], Any]) -> Any:
    """Substitute expressions in one input value.

    A string that is exactly one expression yields the raw resolved value
    (possibly ``None``); a template yields a string with each expression
    replaced by its text (``None`` becomes the empty string). Objects are
    rendered member-wise; other JSON values pass through untouched. A required
    (``!``) expression that resolves to ``None`` or ``""`` raises
    ``unresolved_expression`` in either form; ``|truncate:N`` then caps a text
    value at N characters (the last one an ellipsis).
    """
    if isinstance(value, dict):
        return {k: render(v, resolve) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, resolve) for v in value]
    if not isinstance(value, str):
        return value

    def resolved(path: Path) -> Any:
        result = resolve(path)
        if path.required and (result is None or result == ""):
            raise ValidationError(
                UNRESOLVED_EXPRESSION,
                f"Required expression {path.text!r} resolved to nothing",
                details={"expression": path.text},
            )
        return _truncated(result, path.truncate)

    # Published documents only: the grammar was checked, $.invocation included.
    whole = _EXPRESSION.fullmatch(value)
    if whole is not None:
        return resolved(parse_path(value, invocation=True))

    def substitute(match: re.Match[str]) -> str:
        text = resolved(parse_path(match.group(0), invocation=True))
        return "" if text is None else str(text)

    return _EXPRESSION.sub(substitute, value)


# --- actions ------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSpec:
    """Inputs an action accepts: ``required`` must be present, the rest may be."""

    required: frozenset[str]
    optional: frozenset[str]


ACTION_SPECS: dict[str, ActionSpec] = {
    ENSURE_WORK: ActionSpec(
        required=frozenset({"type", "key", "title"}),
        optional=frozenset(
            {
                "description",
                "assignee",
                "relation",
                "priority",
                "workspace",
                CUSTOM_FIELDS,
                REQUEST_APPROVAL,
            }
        ),
    ),
    COMPLETE_TASK: ActionSpec(required=frozenset(), optional=frozenset({"task"})),
    COMMENT: ActionSpec(required=frozenset({"body"}), optional=frozenset({"task"})),
    TRANSITION: ActionSpec(required=frozenset({"status"}), optional=frozenset({"task"})),
    INVOKE_SKILL: ActionSpec(
        required=frozenset({"skill"}),
        optional=frozenset({"inputs", "expect", ON_SUCCESS, ON_FAILURE}),
    ),
}
ACTIONS = frozenset(ACTION_SPECS)
RELATION_TYPES = frozenset(r.value for r in TaskRelationType)
# Actions that close the gated task; references to it by expression.
_OWN_TASK_REFS = frozenset({"$.task.id", "$.task.id!", "$.task.publicId", "$.task.publicId!"})


@dataclass(frozen=True)
class Action:
    """One declared action.

    ``on_success``/``on_failure`` — for ``invokeSkill``, the actions that
    react to an invocation that succeeded as expected / did not; on such a
    reaction in the flat list (:meth:`ApprovalSchema.actions_for`),
    ``reacts_to`` is the index of its ``invokeSkill`` and ``when`` the ending
    it reacts to (``onSuccess`` or ``onFailure``).
    """

    name: str
    inputs: dict[str, Any]
    on_success: tuple["Action", ...] = ()
    on_failure: tuple["Action", ...] = ()
    reacts_to: int | None = None
    when: str | None = None


@dataclass(frozen=True)
class Precondition:
    """One precondition of a decision (TAI-ADR-0041 p.7).

    Selects the newest ``observation.recorded`` of ``kind`` (and ``source``)
    about ``task`` (an expression naming a task id; the gated task if neither
    it nor ``external_ref`` is given) and/or the external object
    ``external_ref`` (a template of its ``externalRef.id``). It holds when
    such an observation exists and ``condition`` (if any) is true on it.
    ``reason`` — a template shown to the decider when it does not hold.
    """

    kind: str
    reason: str
    source: str | None = None
    task: str | None = None
    external_ref: str | None = None
    condition: Any = None

    def paths(self) -> list[Path]:
        """Every decision-context expression the precondition reads."""
        return [
            path
            for text in (self.task, self.external_ref, self.reason)
            if text is not None
            for path in expressions_in(text)
        ]


@dataclass(frozen=True)
class ApprovalSchema:
    gates: dict[str, dict[str, tuple[Action, ...]]]
    # gate -> outcome -> preconditions of that decision.
    preconditions: dict[str, dict[str, tuple[Precondition, ...]]] = field(default_factory=dict)

    def preconditions_for(self, gate: str, outcome: str) -> tuple[Precondition, ...]:
        return self.preconditions.get(gate, {}).get(outcome, ())

    def actions_for(self, gate: str, outcome: str) -> tuple[Action, ...]:
        """The outcome's actions, then the reactions of its ``invokeSkill`` steps.

        One flat list, so every reaction has a stable index — the executor's
        idempotency key — and waits behind the actions that do not depend on
        how a skill ended. Per ``invokeSkill``: its ``onSuccess`` reactions,
        then its ``onFailure`` ones.
        """
        actions = self.gates.get(gate, {}).get(outcome, ())
        reactions = tuple(
            replace(reaction, reacts_to=index, when=when)
            for index, action in enumerate(actions)
            for when, branch in ((ON_SUCCESS, action.on_success), (ON_FAILURE, action.on_failure))
            for reaction in branch
        )
        return actions + reactions

    @property
    def empty(self) -> bool:
        return not any(actions for outcomes in self.gates.values() for actions in outcomes.values())


EMPTY_SCHEMA = ApprovalSchema(gates={})


def _schema_error(message: str, path: str) -> ValidationError:
    return ValidationError("invalid_approval_schema", message, details={"path": path})


def _check_string(value: Any, path: str, *, invocation: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _schema_error(f"{path} must be a non-empty string", path)
    if len(value) > MAX_INPUT_LENGTH:
        raise _schema_error(f"{path} exceeds {MAX_INPUT_LENGTH} characters", path)
    expressions_in(value, where=path, invocation=invocation)
    return value


def _is_literal(value: str) -> bool:
    return _EXPRESSION.search(value) is None


def _check_action(
    raw: Any,
    path: str,
    *,
    statuses: frozenset[str] | None,
    terminal: frozenset[str] | None,
    reaction: bool = False,
) -> Action:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise _schema_error(
            f"{path} must be an object with exactly one action key ({sorted(ACTIONS)})", path
        )
    ((name, inputs),) = raw.items()
    if name not in ACTION_SPECS:
        raise _schema_error(f"{path}: unknown action {name!r}; expected {sorted(ACTIONS)}", path)
    if reaction and name == INVOKE_SKILL:
        # A reaction that could itself wait on a skill would make the outcome
        # a tree; one level is what the flat, indexed list can express.
        raise _schema_error(f"{path}: a reaction cannot invoke another skill", path)
    where = f"{path}.{name}"
    if not isinstance(inputs, dict):
        raise _schema_error(f"{where} must be an object", where)
    spec = ACTION_SPECS[name]
    unknown = set(inputs) - spec.required - spec.optional
    if unknown:
        raise _schema_error(f"{where}: unknown inputs {sorted(unknown)}", where)
    missing = spec.required - set(inputs)
    if missing:
        raise _schema_error(f"{where}: missing inputs {sorted(missing)}", where)

    reactions: dict[str, tuple[Action, ...]] = {}
    for key, value in inputs.items():
        field_path = f"{where}.{key}"
        if name == ENSURE_WORK and key == "relation":
            _check_relation(value, field_path, invocation=reaction)
        elif name == ENSURE_WORK and key == CUSTOM_FIELDS:
            _check_custom_fields(value, field_path, invocation=reaction)
        elif name == ENSURE_WORK and key == REQUEST_APPROVAL:
            _check_request_approval(value, field_path, invocation=reaction)
        elif name == INVOKE_SKILL and key == "inputs":
            _check_skill_input(value, field_path)
        elif name == INVOKE_SKILL and key == "expect":
            _check_expect(value, field_path)
        elif name == INVOKE_SKILL and key in REACTIONS:
            reactions[key] = check_actions(
                value, field_path, statuses=statuses, terminal=terminal, reaction=True
            )
        else:
            _check_string(value, field_path, invocation=reaction)

    if name == ENSURE_WORK:
        type_key = inputs["type"]
        if _is_literal(type_key) and not _TYPE_KEY.fullmatch(type_key):
            raise _schema_error(f"{where}.type is not a task type key", f"{where}.type")
        if not _WORK_KEY.fullmatch(inputs["key"]):
            raise _schema_error(
                f"{where}.key must be 1..200 of [A-Za-z0-9_.:/ -] (expressions allowed)",
                f"{where}.key",
            )
        priority = inputs.get("priority")
        if priority is not None and _is_literal(priority) and priority not in set(TaskPriority):
            raise _schema_error(f"{where}.priority: unknown priority {priority!r}", where)
    if name == INVOKE_SKILL:
        skill = inputs["skill"]
        if not _is_literal(skill) or not _SKILL_REF.fullmatch(skill):
            raise _schema_error(
                f"{where}.skill must be a literal skill reference (name or name@version)",
                f"{where}.skill",
            )
    if name == TRANSITION and _targets_own_task(inputs):
        # The target is this type's own task: whether the move closes it must
        # be known now (the order rule below), so its status is a literal.
        status = inputs["status"]
        if not _is_literal(status):
            raise _schema_error(
                f"{where}.status: a transition of the gated task takes a literal status",
                f"{where}.status",
            )
    if name == TRANSITION and statuses is not None and "task" not in inputs:
        # An undeclared status of this type's own task can be refused now.
        # For another task the lifecycle is only known at runtime.
        status = inputs["status"]
        if status not in statuses:
            raise _schema_error(
                f"{where}.status {status!r} is not declared by this type's lifecycle",
                f"{where}.status",
            )
    declared = {k: v for k, v in inputs.items() if k not in REACTIONS}
    return Action(
        name=name,
        inputs=declared,
        on_success=reactions.get(ON_SUCCESS, ()),
        on_failure=reactions.get(ON_FAILURE, ()),
    )


def _targets_own_task(inputs: Mapping[str, Any]) -> bool:
    target = inputs.get("task")
    return target is None or target in _OWN_TASK_REFS


def _closes_own_task(action: Action, terminal: frozenset[str] | None) -> bool:
    """Does this action close the gated task itself?

    Decidable at publication: a ``transition`` of the gated task only takes a
    literal status (:func:`_check_action`).
    """
    if not _targets_own_task(action.inputs):
        return False
    if action.name == COMPLETE_TASK:
        return True
    status = action.inputs.get("status")
    return action.name == TRANSITION and terminal is not None and status in terminal


def check_actions(
    raw: Any,
    path: str,
    *,
    statuses: frozenset[str] | None,
    terminal: frozenset[str] | None,
    reaction: bool = False,
) -> tuple[Action, ...]:
    if not isinstance(raw, list):
        raise _schema_error(f"{path} must be a list of actions", path)
    if len(raw) > MAX_ACTIONS_PER_OUTCOME:
        raise _schema_error(f"{path} allows at most {MAX_ACTIONS_PER_OUTCOME} actions", path)
    actions: list[Action] = []
    closed_at: int | None = None
    invoked_at: int | None = None
    for index, item in enumerate(raw):
        action = _check_action(
            item, f"{path}[{index}]", statuses=statuses, terminal=terminal, reaction=reaction
        )
        # The approval is the external_write basis only while its task is
        # open: an invocation whose task is closed is cancelled at claim
        # (basis_revoked / task_terminal). Closing the task next to an
        # invokeSkill would close it before the skill could run, so an outcome
        # that invokes a skill closes its task only in a reaction — once the
        # call has ended (onSuccess / onFailure).
        if action.name == INVOKE_SKILL and closed_at is not None:
            raise _schema_error(
                f"{path}[{index}]: invokeSkill must come before {path}[{closed_at}], "
                "which closes the task the approval is the basis on; close it in "
                "onSuccess/onFailure of the invokeSkill instead",
                f"{path}[{index}]",
            )
        closes = _closes_own_task(action, terminal)
        if closes and invoked_at is not None:
            raise _schema_error(
                f"{path}[{index}] closes the task while the skill of {path}[{invoked_at}] "
                "may not have run yet; close it in onSuccess/onFailure of the invokeSkill",
                f"{path}[{index}]",
            )
        if closed_at is None and closes:
            closed_at = index
        if invoked_at is None and action.name == INVOKE_SKILL:
            invoked_at = index
        actions.append(action)
    return tuple(actions)


def _check_expect(value: Any, path: str) -> None:
    """``expect``: output fields and the literal values success requires."""
    if not isinstance(value, dict) or not value:
        raise _schema_error(f"{path} must be a non-empty object", path)
    for key, item in value.items():
        if not _OUTPUT_KEY.fullmatch(key):
            raise _schema_error(f"{path}.{key}: not an output field name", path)
        if isinstance(item, str) and not _is_literal(item):
            raise _schema_error(f"{path}.{key}: expect takes literals, not expressions", path)
        if item is not None and not isinstance(item, str | bool | int | float):
            raise _schema_error(f"{path}.{key}: only strings, numbers, booleans, null", path)


def _check_relation(value: Any, path: str, *, invocation: bool = False) -> None:
    if not isinstance(value, dict) or len(value) != 1:
        raise _schema_error(
            f"{path} must be an object with exactly one relation type ({sorted(RELATION_TYPES)})",
            path,
        )
    ((relation_type, target),) = value.items()
    if relation_type not in RELATION_TYPES:
        raise _schema_error(f"{path}: unknown relation type {relation_type!r}", path)
    _check_string(target, f"{path}.{relation_type}", invocation=invocation)


def _check_custom_fields(value: Any, path: str, *, invocation: bool = False) -> None:
    """``{field: string}``: each value an expression or a template.

    Only the form is checked here; whether the rendered values fit is up to
    the ``field_schema`` of the type the work is filed under, which is only
    known when the action runs (the type key may itself be an expression).
    """
    if not isinstance(value, dict) or not value:
        raise _schema_error(f"{path} must be a non-empty object of field -> string", path)
    if len(value) > MAX_CUSTOM_FIELDS:
        raise _schema_error(f"{path} allows at most {MAX_CUSTOM_FIELDS} fields", path)
    for key, item in value.items():
        if not _FIELD_NAME.fullmatch(key):
            raise _schema_error(f"{path}.{key}: not a field name", path)
        _check_string(item, f"{path}.{key}", invocation=invocation)


def _check_request_approval(value: Any, path: str, *, invocation: bool = False) -> None:
    """``{assignee, comment?}``: a gate approval on the work item just filed."""
    if not isinstance(value, dict):
        raise _schema_error(f"{path} must be an object", path)
    unknown = set(value) - {"assignee", "comment"}
    if unknown:
        raise _schema_error(f"{path}: unknown inputs {sorted(unknown)}", path)
    if "assignee" not in value:
        raise _schema_error(f"{path}: missing inputs ['assignee']", path)
    for key, item in value.items():
        _check_string(item, f"{path}.{key}", invocation=invocation)


def _check_skill_input(value: Any, path: str) -> None:
    if not isinstance(value, dict):
        raise _schema_error(f"{path} must be an object", path)
    for key, item in value.items():
        if isinstance(item, str):
            _check_string(item, f"{path}.{key}")
        elif isinstance(item, dict):
            _check_skill_input(item, f"{path}.{key}")
        elif item is not None and not isinstance(item, bool | int | float):
            raise _schema_error(f"{path}.{key}: only strings, numbers, booleans, objects", path)


def parse_approval_schema(
    document: Any,
    *,
    statuses: frozenset[str] | None = None,
    terminal: frozenset[str] | None = None,
) -> ApprovalSchema:
    """Validate an ``approval_schema`` document; ``{}`` means "no outcomes".

    ``statuses`` — the status keys of the type's own lifecycle, to refuse a
    ``transition`` of the gated task into a status the type never declares;
    ``terminal`` — those of them that close the task, to refuse an
    ``invokeSkill`` that would only run after its basis is gone.
    """
    if document is None or document == {}:
        return EMPTY_SCHEMA
    guard_json_document(document, label="approvalSchema")
    reject_secret_material(document, label="approvalSchema")
    unknown = set(document) - {"gates"}
    if unknown:
        raise _schema_error(f"approvalSchema: unknown keys {sorted(unknown)}", "approvalSchema")
    gates_doc = document.get("gates")
    if not isinstance(gates_doc, dict) or not gates_doc:
        raise _schema_error("approvalSchema.gates must be a non-empty object", "gates")
    if len(gates_doc) > MAX_GATES:
        raise _schema_error(f"approvalSchema.gates allows at most {MAX_GATES} gates", "gates")

    gates: dict[str, dict[str, tuple[Action, ...]]] = {}
    preconditions: dict[str, dict[str, tuple[Precondition, ...]]] = {}
    for gate_name, gate in gates_doc.items():
        path = f"gates.{gate_name}"
        if not _GATE_NAME.fullmatch(gate_name):
            raise _schema_error(f"{path}: gate names are [a-z][a-z0-9_.-]*", path)
        if gate_name != DEFAULT_GATE:
            # An approval does not carry a gate name yet, so a named gate could
            # never fire: refusing it now beats publishing outcomes that
            # silently never run.
            raise _schema_error(
                f"{path}: only the {DEFAULT_GATE!r} gate is supported until approvals "
                "carry a gate name",
                path,
            )
        if not isinstance(gate, dict) or not gate or set(gate) - {"outcomes", "preconditions"}:
            raise _schema_error(
                f"{path} must be an object with 'outcomes' and/or 'preconditions'", path
            )
        if "preconditions" in gate:
            preconditions[gate_name] = _check_preconditions(
                gate["preconditions"], f"{path}.preconditions"
            )
        outcomes_doc = gate.get("outcomes", {})
        if not isinstance(outcomes_doc, dict):
            raise _schema_error(f"{path}.outcomes must be an object", f"{path}.outcomes")
        unknown_outcomes = set(outcomes_doc) - set(OUTCOMES)
        if unknown_outcomes:
            raise _schema_error(
                f"{path}.outcomes: unknown outcomes {sorted(unknown_outcomes)}; "
                f"expected {list(OUTCOMES)}",
                f"{path}.outcomes",
            )
        outcomes: dict[str, tuple[Action, ...]] = {}
        for outcome, actions in outcomes_doc.items():
            outcomes[outcome] = check_actions(
                actions, f"{path}.outcomes.{outcome}", statuses=statuses, terminal=terminal
            )
        gates[gate_name] = outcomes
    return ApprovalSchema(gates=gates, preconditions=preconditions)


def _check_preconditions(raw: Any, path: str) -> dict[str, tuple[Precondition, ...]]:
    if not isinstance(raw, dict):
        raise _schema_error(f"{path} must be an object", path)
    unknown = set(raw) - set(PRECONDITION_OUTCOMES)
    if unknown:
        raise _schema_error(
            f"{path}: preconditions only of {list(PRECONDITION_OUTCOMES)}, not {sorted(unknown)}; "
            "rejecting is always possible",
            path,
        )
    checked: dict[str, tuple[Precondition, ...]] = {}
    for outcome, items in raw.items():
        where = f"{path}.{outcome}"
        if not isinstance(items, list):
            raise _schema_error(f"{where} must be a list of preconditions", where)
        if len(items) > MAX_PRECONDITIONS:
            raise _schema_error(f"{where} allows at most {MAX_PRECONDITIONS} preconditions", where)
        checked[outcome] = tuple(
            _check_precondition(item, f"{where}[{index}]") for index, item in enumerate(items)
        )
    return checked


def _check_precondition(raw: Any, path: str) -> Precondition:
    if not isinstance(raw, dict):
        raise _schema_error(f"{path} must be an object", path)
    unknown = set(raw) - {"observation", "condition", "reason"}
    missing = {"observation", "reason"} - set(raw)
    if unknown:
        raise _schema_error(f"{path}: unknown keys {sorted(unknown)}", path)
    if missing:
        raise _schema_error(f"{path}: missing keys {sorted(missing)}", path)

    selector = raw["observation"]
    where = f"{path}.observation"
    if not isinstance(selector, dict):
        raise _schema_error(f"{where} must be an object", where)
    unknown = set(selector) - {"kind", "source", "task", "externalRef"}
    if unknown:
        raise _schema_error(f"{where}: unknown keys {sorted(unknown)}", where)
    kind = selector.get("kind")
    if not isinstance(kind, str) or not _OBSERVATION_KIND.fullmatch(kind):
        raise _schema_error(f"{where}.kind must be an observation kind", f"{where}.kind")
    source = selector.get("source")
    if source is not None and (
        not isinstance(source, str) or not _OBSERVATION_SOURCE.fullmatch(source)
    ):
        raise _schema_error(f"{where}.source must be an observation source", f"{where}.source")
    task = selector.get("task")
    if task is not None:
        task = _check_string(task, f"{where}.task")
        exact = _EXPRESSION.fullmatch(task) is not None
        target = parse_path(task, where=f"{where}.task") if exact else None
        if target is None or target.root not in TASK_ROOTS or target.field != "id":
            raise _schema_error(
                f"{where}.task must be $.task.id or $.spawnedBy.id", f"{where}.task"
            )
    external_ref = selector.get("externalRef")
    if external_ref is not None:
        external_ref = _check_context_text(external_ref, f"{where}.externalRef")

    condition = raw.get("condition")
    if condition is not None:
        # work_rules imports this module (through work_graph).
        from control_plane.domain.work_rules import validate_expression

        try:
            validate_expression(
                condition, roots=frozenset({OBSERVATION_ROOT}), where=f"{path}.condition"
            )
        except ValidationError as exc:
            raise _schema_error(f"{path}.condition: {exc.message}", f"{path}.condition") from exc

    reason = _check_context_text(raw["reason"], f"{path}.reason")
    if len(reason) > MAX_REASON_LENGTH:
        raise _schema_error(f"{path}.reason exceeds {MAX_REASON_LENGTH} characters", path)
    return Precondition(
        kind=kind,
        reason=reason,
        source=source,
        task=task,
        external_ref=external_ref,
        condition=condition,
    )


def _check_context_text(value: Any, path: str) -> str:
    """A template over the gated task and the task it was spawned by only.

    A precondition is checked before the decision exists, so ``$.approval``
    has nothing to read yet.
    """
    text = _check_string(value, path)
    for expression in expressions_in(text, where=path):
        if expression.root not in TASK_ROOTS:
            raise _schema_error(
                f"{path}: a precondition reads only $.task and $.spawnedBy, "
                f"not {expression.text!r}",
                path,
            )
    return text


def schema_actions(
    document: Mapping[str, Any] | None, gate: str, outcome: str
) -> tuple[Action, ...]:
    """Actions of an already-published (hence valid) document."""
    return parse_approval_schema(dict(document or {})).actions_for(gate, outcome)


@dataclass(frozen=True)
class SkillCall:
    """An ``invokeSkill`` of a parsed schema, for the checks that need the registry."""

    outcome: str
    path: str
    name: str
    version: str | None


def skill_calls(schema: ApprovalSchema) -> list[SkillCall]:
    """Every ``invokeSkill`` of the schema (reactions never invoke skills)."""
    calls: list[SkillCall] = []
    for gate_name, outcomes in schema.gates.items():
        for outcome, actions in outcomes.items():
            for index, action in enumerate(actions):
                if action.name != INVOKE_SKILL:
                    continue
                name, _, version = str(action.inputs["skill"]).partition("@")
                calls.append(
                    SkillCall(
                        outcome=outcome,
                        path=f"gates.{gate_name}.outcomes.{outcome}[{index}].invokeSkill.skill",
                        name=name,
                        version=version or None,
                    )
                )
    return calls
