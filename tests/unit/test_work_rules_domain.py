"""The rule documents and their language (CP-ADR-0063): validation and evaluation.

The expression language is the whole of what a rule can compute: a closed
set of operators over JSON, strict equality, and an error — not a silent
``false`` — when a rule compares things that cannot be compared.
"""

from typing import Any

import pytest

from control_plane.domain.errors import ValidationError
from control_plane.domain.work_rules import (
    BASE_ROOTS,
    ConditionError,
    VarPath,
    action_roots,
    evaluate,
    normalize_action,
    normalize_condition,
    normalize_interpretation,
    normalize_rule_spec,
    normalize_trigger,
    render,
    rule_roots,
    trigger_matches,
    validate_expression,
    walk,
)

FACTS: dict[str, Any] = {
    "payload": {
        "kind": "repo.commit_observed",
        "data": {"repo": "control-plane", "count": 3, "flag": True, "list": ["a", "b"]},
    },
    "trigger": {"kind": "observation", "type": "repo.commit_observed"},
}


def resolve(path: VarPath) -> Any:
    return walk(FACTS.get(path.root), path.segments)


def ev(expression: Any) -> bool:
    validate_expression(expression, roots=BASE_ROOTS)
    return evaluate(expression, resolve, roots=BASE_ROOTS)


def var(path: str) -> dict[str, str]:
    return {"var": path}


ENSURE = {
    "kind": "ensure_work",
    "taskType": "coding-task",
    "dedupKeyTemplate": "drift:{{payload.data.repo}}",
    "fields": {"title": "Drift in {{payload.data.repo}}"},
}


# --- evaluation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        (True, True),
        (False, False),
        ({"eq": [var("payload.data.repo"), "control-plane"]}, True),
        ({"ne": [var("payload.data.repo"), "control-plane"]}, False),
        ({"gt": [var("payload.data.count"), 2]}, True),
        ({"le": [var("payload.data.count"), 2]}, False),
        ({"lt": ["a", "b"]}, True),
        ({"in": [var("payload.data.repo"), ["memory-service", "control-plane"]]}, True),
        ({"in": ["c", var("payload.data.list")]}, False),
        ({"in": ["x", var("payload.data.missing")]}, False),
        ({"exists": "payload.data.repo"}, True),
        ({"exists": "payload.data.nope"}, False),
        ({"not": {"exists": "payload.data.nope"}}, True),
        ({"and": [{"exists": "payload.data"}, {"eq": [var("trigger.kind"), "observation"]}]}, True),
        ({"or": [False, {"eq": [1, 1.0]}]}, True),
        # Strict JSON equality: true is not 1, and a missing value is null.
        ({"eq": [var("payload.data.flag"), 1]}, False),
        ({"eq": [var("payload.data.nope"), None]}, True),
        ({"eq": [var("payload.data.list"), {"const": ["a", "b"]}]}, True),
        # Ordering against null is false, not an error.
        ({"gt": [var("payload.data.nope"), 1]}, False),
    ],
)
def test_expressions_evaluate_strictly(expression: Any, expected: bool) -> None:
    assert ev(expression) is expected


@pytest.mark.parametrize(
    "expression",
    [
        {"lt": [var("payload.data.repo"), 3]},
        {"gt": [var("payload.data.flag"), 0]},
        {"in": ["a", var("payload.data.repo")]},
    ],
)
def test_comparing_what_cannot_be_compared_is_an_error_not_false(expression: Any) -> None:
    with pytest.raises(ConditionError):
        ev(expression)


@pytest.mark.parametrize(
    ("expression", "fragment"),
    [
        ({"eval": ["1+1"]}, "unknown operator"),
        ({"eq": [1]}, "exactly two operands"),
        ({"and": []}, "list of 1.."),
        ({"eq": [1, 2], "ne": [1, 2]}, "single-operator"),
        ("payload.data.repo", "single-operator"),
        ({"eq": [var("secrets.token"), 1]}, "unknown root"),
        ({"eq": [var("skill.output.status"), 1]}, "unknown root"),
        ({"eq": [var("payload.data..x"), 1]}, "not a valid path"),
        ({"exists": "payload.__class__.mro()"}, "not a valid path"),
        ({"eq": [{"var": "payload", "const": 1}, 1]}, "single-operator"),
    ],
)
def test_anything_outside_the_grammar_is_refused(expression: Any, fragment: str) -> None:
    with pytest.raises(ValidationError) as exc:
        normalize_condition(expression)
    assert exc.value.code == "invalid_rule_condition"
    assert fragment in exc.value.message


def test_expressions_are_bounded_in_depth_and_size() -> None:
    deep: Any = True
    for _ in range(17):
        deep = {"not": deep}
    with pytest.raises(ValidationError, match="nested deeper"):
        normalize_condition(deep)
    wide = {"and": [{"eq": [i, i]} for i in range(50)] + [True]}
    with pytest.raises(ValidationError):
        normalize_condition(wide)


def test_an_omitted_condition_is_always_true() -> None:
    assert normalize_condition(None) is True


# --- templates ---------------------------------------------------------------------


def test_templates_keep_raw_values_or_substitute_text() -> None:
    assert render("{{payload.data.list}}", resolve, roots=BASE_ROOTS) == ["a", "b"]
    assert render("{{ payload.data.count }}", resolve, roots=BASE_ROOTS) == 3
    assert render("{{payload.data.repo}}#{{payload.data.count}}", resolve, roots=BASE_ROOTS) == (
        "control-plane#3"
    )
    assert render("x{{payload.data.nope}}y", resolve, roots=BASE_ROOTS) == "xy"
    assert render(
        {"repository": "{{payload.data.repo}}", "n": ["{{payload.data.count}}"]},
        resolve,
        roots=BASE_ROOTS,
    ) == {"repository": "control-plane", "n": [3]}


# --- documents ---------------------------------------------------------------------


def test_triggers() -> None:
    assert normalize_trigger({"kind": "observation", "type": "repo.commit_observed"}) == {
        "kind": "observation",
        "type": "repo.commit_observed",
    }
    assert normalize_trigger({"kind": "event", "type": "task.created"})["type"] == "task.created"
    assert normalize_trigger({"kind": "schedule", "type": "interval", "everySeconds": 3600})
    for bad in (
        {"kind": "event", "type": "rule.evaluated"},
        {"kind": "event", "type": "work.derived"},
        # A skill call's life is written by its executor: a rule on it would
        # re-queue itself through its own interpretation.
        {"kind": "event", "type": "skill.invocation_succeeded"},
        {"kind": "event", "type": "skill.invocation_failed"},
        {"kind": "event", "type": "skill.invocation_claimed"},
        {"kind": "event", "type": "observation.recorded"},
        {"kind": "schedule", "type": "interval", "everySeconds": 5},
        {"kind": "schedule", "type": "cron", "everySeconds": 3600},
        {"kind": "webhook", "type": "x"},
        {"kind": "observation", "type": "Repo Commit"},
        {"kind": "observation", "type": "a", "extra": 1},
    ):
        with pytest.raises(ValidationError) as exc:
            normalize_trigger(bad)
        assert exc.value.code == "invalid_rule_trigger"


def test_trigger_matching() -> None:
    observation = {"kind": "observation", "type": "repo.commit_observed", "source": "git"}
    payload = {"kind": "repo.commit_observed", "source": "git"}
    assert trigger_matches(observation, "observation.recorded", payload)
    assert not trigger_matches(observation, "observation.recorded", {**payload, "source": "ci"})
    assert not trigger_matches(observation, "task.created", payload)
    assert trigger_matches({"kind": "event", "type": "task.created"}, "task.created", {})


def test_interpretation_needs_a_pinned_skill() -> None:
    assert normalize_interpretation(
        {"skill": "check@1", "inputs": {"repository": "{{payload.data.repo}}"}}
    ) == {"skill": "check@1", "inputs": {"repository": "{{payload.data.repo}}"}}
    for bad in (
        {"skill": "check"},
        {"skill": "check@1", "inputs": {"x": "{{skill.output}}"}},
        {"skill": "check@1", "code": "import os"},
    ):
        with pytest.raises(ValidationError) as exc:
            normalize_interpretation(bad)
        assert exc.value.code == "invalid_rule_interpretation"


def test_actions() -> None:
    assert normalize_action(ENSURE, interpreted=False)["kind"] == "ensure_work"
    per_item = normalize_action(
        {
            **ENSURE,
            "forEach": "skill.output.results",
            "where": {"ne": [var("item.status"), "implemented"]},
            "dedupKeyTemplate": "{{item.adr}}",
        },
        interpreted=True,
    )
    assert per_item["forEach"] == "skill.output.results"
    assert normalize_action(
        {"kind": "cancel_work", "dedupKeyTemplate": "k"}, interpreted=False
    ) == {"kind": "cancel_work", "dedupKeyTemplate": "k", "fields": {}}
    cases: list[tuple[dict[str, Any], bool]] = [
        ({**ENSURE, "kind": "delete_everything"}, False),
        ({**ENSURE, "taskType": None}, False),
        ({**ENSURE, "fields": {}}, False),
        ({**ENSURE, "dedupKeyTemplate": ""}, False),
        ({**ENSURE, "fields": {"title": "t", "sql": "drop"}}, False),
        # skill.* only after an interpretation, item.* only inside forEach.
        ({**ENSURE, "dedupKeyTemplate": "{{skill.output.ref}}"}, False),
        ({**ENSURE, "dedupKeyTemplate": "{{item.adr}}"}, True),
        ({**ENSURE, "where": True}, True),
        ({"kind": "update_work", "taskType": "x", "dedupKeyTemplate": "k"}, False),
        ({"kind": "cancel_work", "dedupKeyTemplate": "k", "fields": {"title": "x"}}, False),
        ({**ENSURE, "kind": "request_decision"}, False),
        ({**ENSURE, "fields": {"title": "t", "approver": "a"}}, False),
    ]
    for bad, interpreted in cases:
        with pytest.raises(ValidationError) as exc:
            normalize_action(bad, interpreted=interpreted)
        assert exc.value.code == "invalid_rule_action", bad


def test_a_scheduled_rule_that_files_work_must_have_a_fact_to_cite() -> None:
    schedule = {"kind": "schedule", "type": "interval", "everySeconds": 3600}
    with pytest.raises(ValidationError) as exc:
        normalize_rule_spec(trigger=schedule, condition=None, interpretation=None, action=ENSURE)
    assert exc.value.code == "invalid_rule"
    spec = normalize_rule_spec(
        trigger=schedule,
        condition=None,
        interpretation={"skill": "check@1", "inputs": {}},
        action=ENSURE,
    )
    assert spec.condition is True


def test_documents_refuse_secret_material() -> None:
    with pytest.raises(ValidationError) as exc:
        normalize_interpretation({"skill": "check@1", "inputs": {"apiToken": "x"}})
    assert exc.value.code == "secret_material_rejected"
    # The condition too: a constant object is stored with the rule.
    with pytest.raises(ValidationError) as exc:
        normalize_condition({"eq": [var("payload.data"), {"const": {"password": "hunter2"}}]})
    assert exc.value.code == "secret_material_rejected"


def test_rule_roots_names_what_a_rule_reads() -> None:
    roots = rule_roots(
        condition={"eq": [var("task.status"), "open"]},
        interpretation=None,
        action={**ENSURE, "fields": {"title": "{{goal.title}}"}},
    )
    assert roots == {"task", "payload", "goal"}
    assert action_roots(interpreted=True, for_each=True) >= {"skill", "item"}


# --- amendment 2026-09-25: complete_work, acceptance, check ----------------------


def test_complete_work_takes_a_check_and_no_fields() -> None:
    assert normalize_action(
        {"kind": "complete_work", "dedupKeyTemplate": "k"}, interpreted=False
    ) == {"kind": "complete_work", "dedupKeyTemplate": "k", "fields": {}}
    assert (
        normalize_action(
            {"kind": "complete_work", "dedupKeyTemplate": "k", "check": "{{payload.data.check}}"},
            interpreted=False,
        )["check"]
        == "{{payload.data.check}}"
    )
    cases: list[dict[str, Any]] = [
        {"kind": "complete_work", "dedupKeyTemplate": "k", "fields": {"title": "x"}},
        {"kind": "complete_work", "dedupKeyTemplate": "k", "check": "Not A Key"},
        {"kind": "complete_work", "dedupKeyTemplate": "k", "check": "{{skill.output.x}}"},
        {"kind": "complete_work", "dedupKeyTemplate": "k", "check": 3},
        {"kind": "cancel_work", "dedupKeyTemplate": "k", "check": "ci"},
        {**ENSURE, "check": "ci"},
        {"kind": "complete_work", "dedupKeyTemplate": "k", "taskType": "task"},
    ]
    for bad in cases:
        with pytest.raises(ValidationError) as exc:
            normalize_action(bad, interpreted=False)
        assert exc.value.code == "invalid_rule_action", bad


def test_creating_actions_carry_acceptance_checked_by_the_task_grammar() -> None:
    acceptance = [
        {"key": "green", "kind": "external_state", "description": "Back to green"},
        {
            "key": "sum",
            "kind": "deterministic",
            "description": "Sums agree",
            "spec": {"skill": "check.sample@1"},
        },
    ]
    assert (
        normalize_action({**ENSURE, "acceptance": acceptance}, interpreted=False)["acceptance"]
        == acceptance
    )
    # Values known only when the rule fires: their paths are checked, not their form.
    templated = [
        "{{payload.data.check}}",
        {"key": "{{payload.data.key}}", "kind": "external_state", "description": "{{payload.x}}"},
        {
            "key": "who",
            "kind": "human",
            "description": "Signed off",
            "spec": {"approver": "{{payload.data.approver}}"},
        },
    ]
    assert (
        normalize_action({**ENSURE, "acceptance": templated}, interpreted=False)["acceptance"]
        == templated
    )
    assert (
        normalize_action({**ENSURE, "acceptance": "{{payload.data.checks}}"}, interpreted=False)[
            "acceptance"
        ]
        == "{{payload.data.checks}}"
    )

    def cause(bad_acceptance: Any, action: dict[str, Any] = ENSURE) -> tuple[str, Any]:
        with pytest.raises(ValidationError) as exc:
            normalize_action({**action, "acceptance": bad_acceptance}, interpreted=False)
        assert exc.value.code == "invalid_rule_action"
        return exc.value.details.get("cause", ""), exc.value.details.get("field")

    # The grammar's own code and path travel in details.
    assert cause(
        [{"key": "x", "kind": "deterministic", "description": "d", "spec": {"suite": "all"}}]
    ) == (
        "invalid_acceptance_spec",
        "action.acceptance[0].spec",
    )
    assert cause([{"key": "x", "kind": "vibes", "description": "d"}])[0] == "invalid_acceptance"
    human = {"key": "x", "kind": "human", "description": "d"}
    assert cause([human, human])[0] == "duplicate_check_key"
    assert cause({"key": "x"})[1] == "action.acceptance"
    assert cause([{**human, "key": "{{skill.output.key}}"}])[1] == ("action.acceptance[0].key")
    # Only work a rule files gets acceptance.
    assert cause([], {"kind": "cancel_work", "dedupKeyTemplate": "k"})[1] == "action.acceptance"


def test_a_scheduled_rule_that_completes_work_must_have_a_fact_to_cite() -> None:
    schedule = {"kind": "schedule", "type": "interval", "everySeconds": 3600}
    complete = {"kind": "complete_work", "dedupKeyTemplate": "k"}
    with pytest.raises(ValidationError) as exc:
        normalize_rule_spec(trigger=schedule, condition=None, interpretation=None, action=complete)
    assert exc.value.code == "invalid_rule"
    cancel = {"kind": "cancel_work", "dedupKeyTemplate": "k"}
    assert normalize_rule_spec(
        trigger=schedule, condition=None, interpretation=None, action=cancel
    ).action == {**cancel, "fields": {}}


def test_rule_roots_include_acceptance_and_check_templates() -> None:
    roots = rule_roots(
        condition=True,
        interpretation=None,
        action={
            "kind": "complete_work",
            "dedupKeyTemplate": "k",
            "check": "{{goal.id}}",
        },
    )
    assert roots == frozenset({"goal"})
    roots = rule_roots(
        condition=True,
        interpretation=None,
        action={**ENSURE, "acceptance": [{"key": "{{task.id}}", "kind": "human"}]},
    )
    assert "task" in roots


# --- fields.customFields of creating actions ----------------------------------------


def test_creating_actions_carry_custom_field_templates() -> None:
    custom = {"repo": "{{payload.data.repo}}", "count": "{{payload.data.count}}", "fixed": "x"}
    action = {**ENSURE, "fields": {**ENSURE["fields"], "customFields": custom}}
    assert normalize_action(action, interpreted=False)["fields"]["customFields"] == custom
    decision = {
        **action,
        "kind": "request_decision",
        "fields": {**action["fields"], "approver": "{{payload.data.who}}"},
    }
    assert normalize_action(decision, interpreted=False)["fields"]["customFields"] == custom
    # The templates render like any field: an exact placeholder keeps the raw value.
    rendered = render(custom, resolve, roots=BASE_ROOTS)
    assert rendered == {"repo": "control-plane", "count": 3, "fixed": "x"}


@pytest.mark.parametrize(
    ("custom", "field"),
    [
        ("{{payload.data}}", "action.fields.customFields"),
        ({}, "action.fields.customFields"),
        (["repo"], "action.fields.customFields"),
        ({"repo": 1}, "action.fields.customFields.repo"),
        ({"repo": {"nested": "x"}}, "action.fields.customFields.repo"),
        ({"not-a-name": "x"}, "action.fields.customFields.not-a-name"),
        ({"repo": "{{nowhere.repo}}"}, "action.fields.customFields.repo"),
        ({f"f{i}": "x" for i in range(33)}, "action.fields.customFields"),
    ],
)
def test_custom_fields_of_the_wrong_form_are_refused(custom: Any, field: str) -> None:
    action = {**ENSURE, "fields": {**ENSURE["fields"], "customFields": custom}}
    with pytest.raises(ValidationError) as exc:
        normalize_action(action, interpreted=False)
    assert exc.value.code == "invalid_rule_action"
    assert exc.value.details["field"] == field


@pytest.mark.parametrize("kind", ["update_work", "cancel_work", "complete_work"])
def test_only_creating_actions_take_custom_fields(kind: str) -> None:
    # Work found by the key keeps its fields: they are the executor's.
    action = {
        "kind": kind,
        "dedupKeyTemplate": "k",
        "fields": {"customFields": {"repo": "x"}},
    }
    with pytest.raises(ValidationError) as exc:
        normalize_action(action, interpreted=False)
    assert exc.value.code == "invalid_rule_action"
    assert exc.value.details["field"] == "action.fields.customFields"


def test_rule_roots_include_custom_field_templates() -> None:
    roots = rule_roots(
        condition=True,
        interpretation=None,
        action={**ENSURE, "fields": {"title": "t", "customFields": {"g": "{{goal.id}}"}}},
    )
    assert "goal" in roots
