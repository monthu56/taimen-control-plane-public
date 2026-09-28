"""The earlier syntaxes translated into CEL give the same values (CP-ADR-0075 §7; P005).

Every expression of the packages selfdev, sdd and notify — conditions of work
and notification rules (``var``), ``forEach`` and ``where``, ``$.`` inputs and
templates of outcomes and checks, ``when`` of checks, execution inputs and
context anchors — is found in the package files, translated, compiled in the
profile and evaluated on the recorded facts of
``fixtures/legacy_expressions/recorded.yaml``, once by the evaluator it is
written for today and once as CEL. Both give the same value, or both fail.

The package files are copies of the superproject's ``packages/`` (only the
objects that carry such expressions, taken at superproject 1180aee): refresh
them with the packages.
"""

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.application.commands.approval_outcomes import DecisionContext
from control_plane.domain import work_rules
from control_plane.domain.approval_outcomes import parse_path, render
from control_plane.domain.cel_profile import (
    LegacyExpression,
    TranslationError,
    legacy_environment,
    legacy_expressions,
    translate,
    translate_condition,
    translate_context_source,
    translate_execution_input,
    translate_path,
    translate_rule_path,
    translate_when,
)
from control_plane.domain.completion_work import is_met
from control_plane.domain.context_schema import parse_source
from control_plane.domain.errors import DomainError
from control_plane_agent import skills

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_expressions"
PACKAGES = ("selfdev", "sdd", "notify")
RECORDED: dict[str, Any] = yaml.safe_load((FIXTURES / "recorded.yaml").read_text("utf-8"))
ENV = legacy_environment()
RULE_ROOTS = work_rules.BASE_ROOTS | {work_rules.ROOT_SKILL, work_rules.ROOT_ITEM}
FAILED = object()


def _package_expressions() -> Iterator[tuple[str, str, LegacyExpression]]:
    for package in PACKAGES:
        for path in sorted((FIXTURES / package).rglob("*.yaml")):
            document = yaml.safe_load(path.read_text("utf-8"))
            for expression in legacy_expressions(document["kind"], document["spec"]):
                yield str(path.relative_to(FIXTURES)), document["kind"], expression


EXPRESSIONS = list(_package_expressions())


def _id(entry: tuple[str, str, LegacyExpression]) -> str:
    return f"{entry[0]}#{entry[2].pointer}"


def test_every_expression_of_the_packages_is_found() -> None:
    # The count per package pins the copies: an expression the finder stops
    # seeing is an expression the translation stops checking.
    counts = {p: sum(1 for f, _, _ in EXPRESSIONS if f.startswith(f"{p}/")) for p in PACKAGES}
    assert counts == {"selfdev": 36, "sdd": 28, "notify": 2}
    assert {e.syntax for _, _, e in EXPRESSIONS} == {
        "condition",
        "rule_path",
        "path",
        "when",
        "execution_input",
        "context_source",
    }


# --- the evaluators of today -----------------------------------------------------


def _trigger(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "event",
        "type": event["type"],
        "eventId": event["id"],
        "eventType": event["type"],
        "entityType": event["entityType"],
        "entityId": event["entityId"],
        "occurredAt": event["time"],
    }


def _rule_documents(record: dict[str, Any], item: Any) -> dict[str, Any]:
    return {
        work_rules.ROOT_TRIGGER: _trigger(record["event"]),
        work_rules.ROOT_PAYLOAD: record["event"]["payload"],
        work_rules.ROOT_TASK: record.get("task"),
        work_rules.ROOT_SKILL: record.get("skill"),
        work_rules.ROOT_ITEM: item,
    }


def _legacy_rule(expression: LegacyExpression, record: dict[str, Any], item: Any) -> Any:
    documents = _rule_documents(record, item)

    def resolve(path: work_rules.VarPath) -> Any:
        return work_rules.walk(documents.get(path.root), path.segments)

    if expression.syntax == "condition":
        return work_rules.evaluate(expression.source, resolve, roots=RULE_ROOTS)
    path = work_rules.parse_path(expression.source, roots=RULE_ROOTS, where="x", code="x")
    return resolve(path)


def _task_view(decision: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in decision["task"].items() if k != "artifacts"}


def _context(decision: dict[str, Any]) -> DecisionContext:
    artifacts = {
        ("task", kind): artifact["metadata"]
        for kind, artifact in (decision["task"].get("artifacts") or {}).items()
    }
    return DecisionContext(
        approval_id=None,
        decided_by=None,
        outcome=decision["approval"]["outcome"],
        approval=decision["approval"],
        task=_task_view(decision),
        spawned_by={},
        artifacts=artifacts,
    )


def _legacy_decision(expression: LegacyExpression, decision: dict[str, Any]) -> Any:
    context = _context(decision)
    invocation = decision.get("invocation")
    if expression.syntax == "path":
        return render(expression.source, lambda path: context.resolve(path, invocation))
    if expression.syntax == "when":
        return all(is_met(context.resolve(parse_path(text))) for text in expression.source)
    if expression.syntax == "context_source":
        return context.resolve(parse_source(expression.source, where="from"))
    # The daemon leaves a missing input out; CEL reads it as null.
    value = skills.resolve_path(_task_view(decision), expression.source)
    return None if value is skills._MISSING else value


# --- the same facts for CEL ---------------------------------------------------------


def _rule_values(record: dict[str, Any], item: Any) -> dict[str, Any]:
    skill = record.get("skill") or {}
    return {
        "event": record["event"],
        "task": record.get("task"),
        "step": {
            "id": skill.get("invocationId"),
            "skill": skill.get("skill"),
            "status": skill.get("status"),
            "result": skill.get("output"),
        },
        "trigger": _trigger(record["event"]),
        "item": item,
    }


def _decision_values(decision: dict[str, Any]) -> dict[str, Any]:
    invocation = decision.get("invocation") or {}
    return {
        "task": decision["task"],
        "approval": decision["approval"],
        "step": {
            "id": invocation.get("id"),
            "skill": invocation.get("skill"),
            "status": invocation.get("status"),
            "result": invocation.get("output"),
            "error": invocation.get("error"),
        },
    }


def _outcome(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except (DomainError, work_rules.ConditionError):
        return FAILED


def _cel(expression: LegacyExpression, values: dict[str, Any]) -> Any:
    program = ENV.compile(translate(expression).expression)
    return _outcome(lambda: program.evaluate(values).value)


def _cases(
    entry: tuple[str, str, LegacyExpression],
) -> Iterator[tuple[str, Callable[[], Any], dict[str, Any]]]:
    _, kind, expression = entry
    if kind in ("WorkRule", "NotificationRule"):
        for record in RECORDED["events"]:
            items = record.get("items") if expression.pointer.endswith("/where") else [None]
            for item in items or []:
                yield (
                    f"{record['name']} / {item}",
                    lambda r=record, i=item: _legacy_rule(expression, r, i),
                    _rule_values(record, item),
                )
    else:
        for decision in RECORDED["decisions"]:
            yield (
                decision["name"],
                lambda d=decision: _legacy_decision(expression, d),
                _decision_values(decision),
            )


@pytest.mark.parametrize("entry", EXPRESSIONS, ids=_id)
def test_the_translation_gives_the_same_values(
    entry: tuple[str, str, LegacyExpression],
) -> None:
    expression = entry[2]
    translation = translate(expression)
    ENV.compile(translation.expression, path=expression.pointer)
    compared = 0
    for name, legacy, values in _cases(entry):
        before = _outcome(legacy)
        after = _cel(expression, values)
        assert (after is FAILED) == (before is FAILED), (name, translation.expression, before)
        if before is not FAILED:
            assert after == before, (name, translation.expression)
        compared += 1
    assert compared >= 3


def test_the_recordings_tell_the_outcomes_apart() -> None:
    # Not a fixture that answers everything the same way: the conditions of
    # the packages both hold and fail on it.
    conditions = [(entry, e) for entry in EXPRESSIONS if (e := entry[2]).syntax == "condition"]
    seen = {_outcome(legacy) for entry, _ in conditions for _, legacy, _ in _cases(entry)}
    assert seen == {True, False}


# --- the forms, one by one ---------------------------------------------------------------


def test_a_condition() -> None:
    translation = translate_condition(
        {
            "and": [
                {"eq": [{"var": "payload.kind"}, "results"]},
                {"not": {"exists": "task.customFields.branch"}},
                {"lt": [{"var": "skill.output.count"}, 3]},
                {"in": ["x", {"var": "item"}]},
            ]
        }
    )
    assert translation.expression == (
        '(event.payload.?kind.orValue(null) == "results")'
        " && (!(task.customFields.?branch.orValue(null) != null))"
        " && (step.result.?count.orValue(null) != null"
        " && step.result.?count.orValue(null) < 3)"
        ' && (item != null && "x" in item)'
    )
    assert translation.bindings == ("item",)
    ENV.compile(translation.expression)


def test_a_missing_value_reads_null_as_before() -> None:
    program = ENV.compile(
        translate_condition({"ne": [{"var": "payload.blocked"}, True]}).expression
    )
    assert program.evaluate({"event": {"payload": {}}}).value is True
    program = ENV.compile(translate_condition({"lt": [{"var": "payload.n"}, 3]}).expression)
    assert program.evaluate({"event": {"payload": {}}}).value is False


def test_the_other_roots_are_named_bindings() -> None:
    assert translate_rule_path("trigger.eventId").bindings == ("trigger",)
    assert translate_rule_path("skill.invocationId").expression == "step.id"
    assert translate_path("$.spawnedBy.artifact[commit].metadata.sha").expression == (
        "spawnedBy.artifacts.?commit.?metadata.?sha.orValue(null)"
    )
    assert translate_path("$.spawnedBy.id").bindings == ("spawnedBy",)
    assert translate_path("$.approval.comment").expression == "approval.comment"


def test_an_artifact_type_that_is_not_a_name_is_indexed() -> None:
    assert translate_path("$.task.artifact[tasks-document].metadata.slug!").expression == (
        'task.artifacts["tasks-document"].metadata.slug'
    )


def test_a_template_and_truncate() -> None:
    translation = translate_path("Merge $.task.publicId!: $.invocation.output.details|truncate:5")
    program = ENV.compile(translation.expression)
    values = {"task": {"publicId": "TASK-1"}, "step": {"result": {"details": "abcdefgh"}}}
    assert program.evaluate(values).value == "Merge TASK-1: abcd…"
    assert program.evaluate({"task": {"publicId": "TASK-1"}}).value == "Merge TASK-1: "


def test_a_required_value_that_is_missing_fails() -> None:
    program = ENV.compile(translate_path("$.task.customFields.branch!").expression)
    with pytest.raises(DomainError):
        program.evaluate({"task": {"customFields": {}}})


def test_when_is_met_as_before() -> None:
    program = ENV.compile(
        translate_when(["$.task.customFields.a", "$.task.customFields.b"]).expression
    )
    for a, b, met in [("x", True, True), ("", True, False), ("x", False, False), (None, 1, False)]:
        assert program.evaluate({"task": {"customFields": {"a": a, "b": b}}}).value is met


def test_execution_inputs_and_context_sources() -> None:
    assert translate_execution_input("$").expression == "task"
    assert translate_execution_input("$.customFields.items[0]").expression == (
        "task.customFields.?items[?0].orValue(null)"
    )
    with pytest.raises(TranslationError):
        translate_execution_input("$.nothing")
    assert translate_context_source("title").expression == "task.title"
    assert translate_context_source("$.spawnedBy.artifact[commit].diffPaths").expression == (
        "spawnedBy.artifacts.?commit.?metadata.?diffPaths.orValue(null)"
    )


def test_an_expression_outside_the_old_grammar_is_refused() -> None:
    with pytest.raises(TranslationError):
        translate_condition({"like": ["a", "b"]})
    with pytest.raises(TranslationError):
        translate_path("$.nobody.knows")
    with pytest.raises(TranslationError):
        translate_rule_path("nowhere.at.all")
