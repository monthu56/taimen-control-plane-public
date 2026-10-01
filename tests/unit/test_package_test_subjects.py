"""The pure parts of rule and task type tests (CP-ADR-0074 Z1, Z3; package-sdk S020).

The file of a test names its subject; the core finds the object in the
package, warns about fields only a process test runs, and counts the coverage
of a rule's expressions and of a task type's declarations. Running the tests
takes the database: ``tests/integration/test_package_test_subjects.py``.
"""

from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from control_plane import sandbox
from control_plane.application.commands import package_trials
from control_plane.application.common import utcnow
from control_plane.domain.package_source import PackageObject, parse_package
from control_plane.domain.work_rules import (
    BASE_ROOTS,
    VarPath,
    branch_outcomes,
    expression_branches,
    walk,
)

API_VERSION = "catalog.test/v1"  # the core reads the format version only
RULE = {
    "trigger": {"kind": "observation", "type": "sample.opened"},
    "condition": {"and": [{"exists": "payload.data.number"}, {"not": {"gt": [1, 2]}}]},
    "action": {"kind": "ensure_work", "taskType": "case", "dedupKeyTemplate": "k"},
}


def document(kind: str, key: str, spec: dict[str, Any]) -> str:
    body = {"apiVersion": API_VERSION, "kind": kind, "key": key, "spec": spec}
    return str(yaml.safe_dump(body, sort_keys=False))


def files(*tests: tuple[str, dict[str, Any]]) -> list[tuple[str, str]]:
    return [
        ("rules/opened.yaml", document("WorkRule", "opened", RULE)),
        ("task-types/case.yaml", document("TaskType", "case", {"displayName": "Case"})),
        *((f"tests/{name}.test.yaml", yaml.safe_dump(data)) for name, data in tests),
    ]


def rule_test(**extra: Any) -> dict[str, Any]:
    return {
        "subject": "rule",
        "rule": "opened",
        "name": "n",
        "given": {"observation": {"kind": "sample.opened"}},
        "steps": [{"expect": {"result": "matched"}}],
        **extra,
    }


# --- the files --------------------------------------------------------------------------


def test_a_test_names_its_subject_and_object() -> None:
    task_type = {
        "subject": "taskType",
        "taskType": "case",
        "name": "t",
        "steps": [{"approve": {"decision": "approved"}}],
    }
    package = parse_package(files(("rule", rule_test()), ("type", task_type)))
    assert package.problems == []
    by_file = {test.file: test for test in package.tests}
    rule = by_file["tests/rule.test.yaml"]
    assert (rule.subject, rule.object, rule.process) == ("rule", "opened", "")
    kind = by_file["tests/type.test.yaml"]
    assert (kind.subject, kind.object) == ("taskType", "case")


def test_a_test_without_a_subject_is_a_process_test_as_before() -> None:
    test = {"process": "sample", "name": "p", "steps": [{"advance": "P1D"}]}
    package = parse_package(files(("p", test)))
    (only,) = package.tests
    assert (only.subject, only.object, only.process) == ("process", "sample", "sample")
    (problem,) = package.problems
    assert (problem.code, problem.path) == ("unknown_test_process", "/process")


def test_an_object_the_package_lacks_is_a_finding_of_its_kind() -> None:
    task_type = {"subject": "taskType", "taskType": "other", "name": "t", "steps": [{"expect": {}}]}
    package = parse_package(files(("r", rule_test(rule="closed")), ("t", task_type)))
    found = {(p.code, p.path, p.file, p.hint) for p in package.problems}
    assert found == {
        ("unknown_test_rule", "/rule", "tests/r.test.yaml", "rules of the package: opened"),
        (
            "unknown_test_task_type",
            "/taskType",
            "tests/t.test.yaml",
            "task types of the package: case",
        ),
    }


def test_fields_only_a_process_runs_are_warnings_with_their_line() -> None:
    test = rule_test(version=1, mocks={"agents": {"a": [{"output": {}}]}, "recall": []})
    package = parse_package(files(("r", test)))
    warnings = {(p.code, p.severity, p.path) for p in package.problems}
    assert warnings == {
        ("test_field_ignored", "warning", "/version"),
        ("test_field_ignored", "warning", "/mocks/agents"),
        ("test_field_ignored", "warning", "/mocks/recall"),
    }
    assert all(p.line for p in package.problems)
    assert len(package.tests) == 1


@pytest.mark.parametrize(
    ("test", "where"),
    [
        pytest.param(rule_test(given={"clock": "2026-01-05T09:00:00Z"}), "/given", id="no-input"),
        pytest.param(rule_test(steps=[{"advance": "P1D"}]), "/steps/0", id="rule-advance"),
        pytest.param(
            {
                "subject": "taskType",
                "taskType": "case",
                "name": "t",
                "steps": [{"approve": {"decision": "approve"}}],
            },
            "/steps/0/approve/decision",
            id="process-decision",
        ),
        pytest.param(rule_test(rule=None), "", id="no-rule"),
    ],
)
def test_what_the_schema_refuses_the_core_refuses(test: dict[str, Any], where: str) -> None:
    test = {k: v for k, v in test.items() if v is not None}
    package = parse_package(files(("r", test)))
    assert package.tests == []
    codes = {(p.code, p.path) for p in package.problems}
    assert ("invalid_test", where) in codes


# --- coverage of a rule -----------------------------------------------------------------


def resolver(payload: dict[str, Any]) -> Any:
    def resolve(path: VarPath) -> Any:
        return walk({"payload": payload}.get(path.root), path.segments)

    return resolve


def test_the_branches_of_a_condition_are_each_node_true_and_false() -> None:
    assert expression_branches(RULE["condition"], "/condition") == [
        "/condition:true",
        "/condition:false",
        "/condition/and/0:true",
        "/condition/and/0:false",
        "/condition/and/1:true",
        "/condition/and/1:false",
        "/condition/and/1/not:true",
        "/condition/and/1/not:false",
    ]
    assert expression_branches(None, "/condition") == []
    assert expression_branches(True, "/condition") == []


def test_every_node_is_evaluated_on_its_own_not_short_circuited() -> None:
    reached = branch_outcomes(RULE["condition"], resolver({}), roots=BASE_ROOTS, at="/condition")
    # The first operand is false, and the second is still counted.
    assert reached == {
        "/condition:false",
        "/condition/and/0:false",
        "/condition/and/1:true",
        "/condition/and/1/not:false",
    }


def test_a_node_whose_evaluation_breaks_reaches_no_branch() -> None:
    clash = {"or": [{"lt": [{"var": "payload.a"}, 1]}, {"exists": "payload.a"}]}
    reached = branch_outcomes(clash, resolver({"a": "x"}), roots=BASE_ROOTS, at="/where")
    # The comparison breaks, and so does the ``or`` over it; its other operand holds.
    assert reached == {"/where/or/1:true"}


def test_the_coverage_of_a_package_lists_every_rule_and_every_declaring_type() -> None:
    approval = {
        "displayName": "Approval",
        "approvalSchema": {
            "gates": {
                "default": {
                    "preconditions": {
                        "approved": [{"observation": {"kind": "sample.ready"}, "reason": "r"}]
                    },
                    "outcomes": {
                        "approved": [
                            {
                                "invokeSkill": {
                                    "skill": "text.summarize@1",
                                    "onSuccess": [{"completeTask": {}}],
                                }
                            }
                        ],
                        "rejected": [{"comment": {"body": "no"}}],
                    },
                }
            }
        },
        "completionSchema": {"onComplete": {"actions": [{"comment": {"body": "done"}}]}},
        "acceptance": [{"key": "checked", "kind": "human", "description": "d"}],
    }
    package = parse_package(
        [*files(), ("task-types/approval.yaml", document("TaskType", "approval", approval))]
    )
    rules = package_trials.rule_coverage(package, [])
    assert [(r.rule, r.tests, r.branches.total, r.outcomes.missing) for r in rules] == [
        ("opened", 0, 8, ["matched", "not_matched"])
    ]
    (kind,) = package_trials.task_type_coverage(package, [], {"approval": 3})
    assert (kind.task_type, kind.version, kind.tests) == ("approval", 3, 0)
    assert kind.outcomes.missing == [
        "default/approved",
        "default/approved/0/onSuccess",
        "default/rejected",
    ]
    assert kind.preconditions.missing == [
        "default/preconditions/approved/0:held",
        "default/preconditions/approved/0:refused",
    ]
    assert kind.completion.missing == ["completion/0"]
    assert kind.acceptance.missing == ["acceptance/checked:passed", "acceptance/checked:failed"]


# --- variables, time and outgoing calls -----------------------------------------------------


def test_variables_are_the_tests_over_the_manifests_defaults() -> None:
    manifest = SimpleNamespace(
        manifest={"variables": {"ROLE": {"kind": "role", "default": "r-1"}, "WS": {}}}
    )
    variables = package_trials.manifest_variables(manifest)  # type: ignore[arg-type]
    assert variables == {"ROLE": "r-1"}
    spec = {"a": "${ROLE}", "b": ["x-${WS}"], "c": 1}
    assert package_trials.substitute(spec, {**variables, "WS": "w"}) == {
        "a": "r-1",
        "b": ["x-w"],
        "c": 1,
    }
    assert package_trials.substitute(spec, {}) == spec


def test_inside_a_test_time_is_virtual_and_moves_forward() -> None:
    state = sandbox.Trial(clock=package_trials._time("2026-01-05T09:00:00Z"))
    with sandbox.trial(state):
        first, second = utcnow(), utcnow()
        assert first.isoformat().startswith("2026-01-05T09:00:00")
        assert first < second
        assert sandbox.public_id_source() is not None
        assert state.next_public_id() == "TEST-000001"
    assert utcnow().year >= 2026 and sandbox.active() is None


def test_an_outgoing_call_is_refused_only_inside_a_test() -> None:
    sandbox.refuse_outgoing("memory")  # outside a test: nothing happens
    state = sandbox.Trial(clock=package_trials._time("2026-01-05T09:00:00Z"))
    with sandbox.trial(state), pytest.raises(sandbox.SandboxOutgoingCall):
        sandbox.refuse_outgoing("memory")
    assert state.outgoing == ["memory"]


def test_an_object_names_what_it_refers_to() -> None:
    rule = PackageObject("WorkRule", "r", {"interpretation": {"skill": "text.sum@1"}}, "r.yaml")
    skill = PackageObject("Skill", "text.sum", {"version": "1"}, "s.yaml")
    other = PackageObject("Skill", "text", {"version": "1"}, "t.yaml")
    assert package_trials._names(rule, skill)
    assert not package_trials._names(rule, other)
