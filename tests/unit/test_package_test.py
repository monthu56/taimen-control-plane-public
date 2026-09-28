"""Package tests in the core: files, the sandbox, coverage (CP-ADR-0074 §10; process-packages P013).

The sandbox runs the engine a live instance runs; here it runs without a
database — the catalog is given as a :class:`World`, as the application
layer builds it from the read-only transaction.
"""

import ast
import copy
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from control_plane.application.commands.package_test import sql_writes
from control_plane.application.commands.process_instances import remembered
from control_plane.application.context.graph import entity_key
from control_plane.domain import process_definition as pd
from control_plane.domain import process_sandbox as sb
from control_plane.domain.package_source import (
    TEST_SCHEMA_FILE,
    load_yaml,
    parse_package,
    select_tests,
)
from control_plane.domain.process_definition import Catalog, SkillEntry
from control_plane.domain.process_engine import Definition
from tests.unit.test_process_contract import (
    PACKAGE_TEST,
    PINNED,
    PROCESS,
    retrospective_contract,
)
from tests.unit.test_process_engine import AUTHOR, CALENDARS, CATALOG, INSTANCE, OTHER, spec
from tests.unit.test_process_engine import Run as EngineRun

SANDBOX = Path(sb.__file__)
API_VERSION = PROCESS["apiVersion"]  # the catalog format of the fixtures


def world(body: str, **extra: Any) -> sb.World:
    definition = Definition.build("test", pd.normalized_spec(spec(body)), CATALOG)
    return sb.World(
        definitions={"test": definition},
        skills=CATALOG.skills,
        task_types=CATALOG.task_types,
        agents=CATALOG.agents,
        roles=frozenset({"lead"}),
        calendars=CALENDARS,
        **extra,
    )


def run(body: str, steps: list[dict[str, Any]], **test: Any) -> sb.TestResult:
    return sb.run_test(
        world(body), "tests/t.test.yaml", {"process": "test", "name": "t", "steps": steps, **test}
    )


def opened(**payload: Any) -> dict[str, Any]:
    body = {"number": "N-1", "amount": 100, "deadline": "2026-05-04T09:00:00Z", "author": AUTHOR}
    return {"emit": {"observation": "case.opened", "payload": {**body, **payload}}}


# --- the files of a package ----------------------------------------------------------------


def test_the_core_holds_the_superproject_test_schema() -> None:
    assert TEST_SCHEMA_FILE.read_bytes() == (PINNED / "test.schema.json").read_bytes()


def test_yaml_is_read_as_yaml_1_2_with_lines_by_pointer() -> None:
    document, lines = load_yaml("a:\n  on: {observation: x}\n  flag: yes\n  list:\n    - true\n")
    assert document == {"a": {"on": {"observation": "x"}, "flag": "yes", "list": [True]}}
    assert lines["/a/on/observation"] == 2 and lines["/a/list/0"] == 5


def _process_file(spec_body: dict[str, Any], key: str = "test") -> str:
    document = {"apiVersion": API_VERSION, "kind": "Process", "key": key, "spec": spec_body}
    return str(yaml.safe_dump(document, sort_keys=False, allow_unicode=True))


def test_a_package_parses_objects_tests_and_places_its_findings() -> None:
    test = {"process": "test", "name": "t", "steps": [{"expect": {"status": "running"}}]}
    package = parse_package(
        [
            ("package.yaml", f"apiVersion: {API_VERSION}\nkind: Package\nkey: p\nspec: {{}}\n"),
            ("processes/test.yaml", _process_file(spec("stages: []"))),
            ("tests/ok.test.yaml", yaml.safe_dump(test)),
            ("tests/bad.test.yaml", "process: test\nname: t\nsteps:\n  - {jump: 1}\n"),
            ("tests/other.test.yaml", yaml.safe_dump({**test, "process": "nothing"})),
            ("broken.yaml", "a: [1,\n"),
            ("odd.yaml", f"apiVersion: {API_VERSION}\nkind: Oddity\nkey: x\nspec: {{}}\n"),
            ("again.yaml", _process_file(spec("stages: []"))),
            ("future.yaml", "apiVersion: example.org/v2\nkind: Role\nkey: r\nspec: {}\n"),
            ("schemas/data.yaml", "type: object\n"),
            ("README.md", "not a catalog object"),
        ]
    )
    assert package.manifest == {}
    assert [o.ref for o in package.objects] == ["Process/test"]
    assert [t.file for t in package.tests] == ["tests/ok.test.yaml", "tests/other.test.yaml"]
    found = {(p.code, p.file, p.line) for p in package.problems}
    assert ("invalid_yaml", "broken.yaml", 2) in found
    assert ("unknown_kind", "odd.yaml", 2) in found
    assert ("invalid_document", "future.yaml", 1) in found
    assert ("duplicate_object", "processes/test.yaml", 3) in found  # again.yaml came first
    assert ("invalid_test", "tests/bad.test.yaml", 4) in found
    assert ("unknown_test_process", "tests/other.test.yaml", 2) in found  # keys sorted


def test_a_data_ref_is_inlined_from_the_package_and_never_from_outside() -> None:
    body = spec("stages: []")
    schema = body["data"]
    package = parse_package(
        [
            (
                "processes/a.yaml",
                _process_file({**body, "data": {"$ref": "../schemas/a.json"}}, "a"),
            ),
            ("processes/b.yaml", _process_file({**body, "data": {"$ref": "../../x.json"}}, "b")),
            ("schemas/a.json", json.dumps(schema)),
        ]
    )
    assert package.process("a") is not None and package.process("a").spec["data"] == schema  # type: ignore[union-attr]
    (problem,) = package.problems
    assert (problem.code, problem.file, problem.path) == (
        "unresolved_data_ref",
        "processes/b.yaml",
        "/spec/data/$ref",
    )


def test_the_request_may_name_tests_and_a_name_that_is_no_test_is_a_finding() -> None:
    test = {"process": "test", "name": "t", "steps": [{"advance": "P1D"}]}
    package = parse_package(
        [
            ("processes/test.yaml", _process_file(spec("stages: []"))),
            ("tests/a.test.yaml", yaml.safe_dump(test)),
            ("tests/b.test.yaml", yaml.safe_dump(test)),
        ]
    )
    chosen, problems = select_tests(package, ["tests/b.test.yaml", "tests/c.test.yaml"])
    assert [t.file for t in chosen] == ["tests/b.test.yaml"]
    assert [(p.code, p.path) for p in problems] == [("unknown_test", "/tests/1")]


# --- the sandbox ---------------------------------------------------------------------------

SKILL = """
stages:
  - id: s
    steps:
      - id: work
        call: {skill: work.do@1, input: {text: "'x'"}}
        output: {as: {value: step.result.value}}
"""


def test_a_skill_mock_that_matches_the_skill_schema_answers_the_call() -> None:
    result = run(
        SKILL,
        [opened(), {"expect": {"status": "completed", "data": {"value": "done"}}}],
        mocks={"skills": {"work.do@1": [{"output": {"value": "done"}}]}},
    )
    assert result.status == "passed", result.failures


def test_a_skill_mock_off_the_skill_schema_fails_the_test() -> None:
    result = run(
        SKILL,
        [opened(), {"expect": {"status": "completed"}}],
        mocks={"skills": {"work.do@1": [{"output": {"value": 5}}]}},
    )
    assert result.status == "failed"
    (failure,) = result.failures
    assert failure.step == 0
    assert "does not match the skill's output schema" in failure.message
    assert failure.actual == {"value": 5}


def test_a_call_without_a_mock_waits_and_a_mock_error_is_the_steps_error() -> None:
    waiting = run(SKILL, [opened(), {"expect": {"status": "running", "stages": {"s": "open"}}}])
    assert waiting.status == "passed", waiting.failures
    failing = run(
        SKILL,
        [opened(), {"expect": {"status": "failed", "error": "busy"}}],
        mocks={"skills": {"work.do@1": [{"error": {"type": "busy", "status": 503}}]}},
    )
    assert failing.status == "passed", failing.failures


def test_a_recall_mock_off_the_form_of_a_memory_answer_fails_the_test() -> None:
    body = """
stages:
  - id: s
    steps:
      - id: history
        recall: {anchors: [{kind: person, key: data.author}], kinds: [case]}
        output: {as: {history: step.result.nodes}}
"""
    good = run(
        body,
        [opened(), {"expect": {"memory": {"recalled": ["history"]}, "status": "completed"}}],
        mocks={
            "recall": [{"step": "history", "output": {"nodes": [{"kind": "case", "key": "c"}]}}]
        },
    )
    assert good.status == "passed", good.failures
    bad = run(body, [opened()], mocks={"recall": [{"output": {"nodes": [{"kind": "case"}]}}]})
    assert bad.status == "failed" and "not a memory answer" in bad.failures[0].message


APPROVE = """
stages:
  - id: s
    steps:
      - id: sign
        approve:
          approvers: [{role: lead}]
          quorum: {atLeast: 1}
          separationOfDuties: "[data.author]"
        output: {as: {decision: step.result.outcome}}
"""


def test_the_core_refuses_an_excluded_or_ineligible_approver() -> None:
    principals = {"lead": [AUTHOR, "bob"]}
    steps = [
        opened(),
        {
            "approve": {
                "step": "sign",
                "by": AUTHOR,
                "decision": "approve",
                "expectRefused": "separation_of_duties_violation",
            }
        },
        {
            "approve": {
                "step": "sign",
                "by": "eve",
                "decision": "approve",
                "expectRefused": "not_eligible",
            }
        },
        {"approve": {"step": "sign", "by": "bob", "decision": "approve"}},
        {"expect": {"status": "completed", "data": {"decision": "approved"}}},
    ]
    result = run(APPROVE, steps, given={"principals": principals})
    assert result.status == "passed", result.failures

    refused = run(
        APPROVE,
        [opened(), {"approve": {"step": "sign", "by": AUTHOR, "decision": "approve"}}],
        given={"principals": principals},
    )
    assert refused.status == "failed"
    assert refused.failures[0].actual == "separation_of_duties_violation"


def test_virtual_time_fires_timers_in_order_at_their_own_moment() -> None:
    body = """
stages:
  - id: s
    steps:
      - {id: pause, wait: P2D}
      - {id: note, set: {note: "string(instance.clock)"}}
"""
    steps = [
        opened(),
        {"advance": "P1D"},
        {
            "expect": {
                "status": "running",
                "timers": [{"id": "pause", "at": "2026-03-04T09:00:00Z"}],
            }
        },
        {"advance": "until:pause"},
        {"expect": {"status": "completed", "data": {"note": "2026-03-04T09:00:00Z"}}},
    ]
    result = run(body, steps, given={"clock": "2026-03-02T09:00:00Z"})
    assert result.status == "passed", result.failures


def test_a_human_step_is_completed_by_its_assignee_with_its_field_schema() -> None:
    body = """
stages:
  - id: s
    steps:
      - id: review
        human: {taskType: review, assign: [{role: lead}]}
        output: {as: {decision: step.result.decision}}
"""
    given = {"principals": {"lead": ["alice"]}}
    steps = [
        opened(),
        {"expect": {"tasks": [{"step": "review", "assignee": "alice", "status": "open"}]}},
        {"complete": {"step": "review", "by": "alice", "output": {"decision": "yes"}}},
        {"expect": {"status": "completed", "data": {"decision": "yes"}}},
    ]
    assert run(body, steps, given=given).status == "passed"
    stranger = run(body, [opened(), {"complete": {"step": "review", "by": "mallory"}}], given=given)
    assert stranger.status == "failed" and "may not complete" in stranger.failures[0].message
    wrong = run(
        body, [opened(), {"complete": {"step": "review", "output": {"decision": 1}}}], given=given
    )
    assert wrong.status == "failed" and "field schema" in wrong.failures[0].message


def test_an_expectation_that_does_not_hold_names_what_was_expected_and_what_is() -> None:
    result = run(
        "stages: [{id: s, steps: [{id: note, set: {note: \"'x'\"}}]}]",
        [
            opened(),
            {"expect": {"status": "running", "data": {"note": "y"}, "events": ["process.failed"]}},
            {"expect": {"outcome": "completed", "noSideEffects": True}},
        ],
    )
    assert result.status == "failed"
    assert [(f.step, f.expected, f.actual) for f in result.failures] == [
        (1, "running", "completed"),
        (1, "y", "x"),
        (
            1,
            "process.failed",
            [
                "process.started",
                "process.stage_entered",
                "process.stage_exited",
                "process.data_changed",
                "process.completed",
            ],
        ),
    ]


def test_no_side_effects_reads_the_count_of_writes_outside_the_sandbox() -> None:
    writes = [0]
    body = "stages: [{id: s, steps: [{id: n, set: {note: \"'x'\"}}]}]"
    here = dataclasses.replace(world(body), writes=lambda: writes[0])
    test = {
        "process": "test",
        "name": "t",
        "steps": [opened(), {"expect": {"noSideEffects": True}}],
    }
    assert sb.run_test(here, "t", test).status == "passed"
    writes[0] = 2
    failed = sb.run_test(here, "t", test)
    assert failed.status == "failed" and failed.failures[0].actual == 2


def test_raw_sql_is_a_write_by_its_keyword_and_a_read_is_not() -> None:
    reads = [
        "SELECT version_num FROM alembic_version",
        "  -- the ancestors\n  WITH RECURSIVE a AS (SELECT id FROM workspaces) SELECT id FROM a",
        "WITH t AS (SELECT id FROM roles FOR UPDATE) SELECT id FROM t",
        "WITH t AS (SELECT id FROM roles FOR NO KEY UPDATE) SELECT 'delete' FROM t",
        '/* insert */ SELECT deleted_at, "update" FROM tasks',
        "SET TRANSACTION READ ONLY",
        "",
    ]
    writes = [
        "INSERT INTO roles (slug) VALUES ('x')",
        "update roles SET slug = 'x'",
        "/* note */ DELETE FROM roles",
        "MERGE INTO roles USING t ON true WHEN MATCHED THEN DELETE",
        "COPY roles FROM STDIN",
        "TRUNCATE roles",
        "WITH gone AS (DELETE FROM roles RETURNING id) SELECT id FROM gone",
    ]
    assert [sql for sql in reads if sql_writes(sql)] == []
    assert [sql for sql in writes if not sql_writes(sql)] == []


def test_the_example_test_of_the_superproject_passes() -> None:
    example = copy.deepcopy(PROCESS["spec"])
    example["data"]["properties"]["approversNeeded"] = {"type": "number"}
    del example["migrations"]  # a map into version 2 belongs to version 2
    catalog = Catalog(
        skills={
            "notify.send@1": SkillEntry(
                {"type": "object", "properties": {"text": {"type": "string"}}}, None
            ),
            "process.retrospective@1": SkillEntry(None, None),
        },
        task_types={"go-no-go": None, "lessons-review": None},
        agents=frozenset({"example-process"}),
        calendars=frozenset({"ru"}),
        artifact_types=frozenset({"notice-document"}),
        processes=frozenset(),
    )
    definition = Definition.build(PROCESS["key"], pd.normalized_spec(example), catalog)
    here = sb.World(
        definitions={definition.key: definition},
        skills=catalog.skills,
        task_types=catalog.task_types,
        agents=catalog.agents,
        roles=frozenset({"director"}),
        calendars=CALENDARS,
    )
    result = sb.run_test(here, "tests/purchase.test.yaml", PACKAGE_TEST)
    assert result.status == "passed", result.failures


# --- coverage ------------------------------------------------------------------------------

BRANCHES = """
decisions:
  - id: level
    hitPolicy: first
    inputs: [{id: amount, expr: data.amount, type: number}]
    outputs: [{id: level, type: number}]
    rules:
      - {when: {amount: "[0..1000)"}, then: {level: 1}}
      - {when: {amount: "-"}, then: {level: 2}}
stages:
  - id: s
    steps:
      - id: pick
        decide: {table: level}
        output: {as: {level: step.result.level}}
      - id: big
        when: data.level == 2.0
        set: {note: "'big'"}
      - id: guarded
        try:
          do:
            - {id: work, call: {skill: work.do@1, input: {text: "'x'"}}}
          catch:
            - errors: {type: busy}
              do: [{id: fallback, set: {note: "'fallback'"}}]
"""


def test_coverage_counts_what_the_tests_reached_and_lists_the_rest() -> None:
    here = world(BRANCHES)
    test = {
        "process": "test",
        "name": "small",
        "mocks": {"skills": {"work.do@1": [{"output": {"value": "v"}}]}},
        "steps": [opened(amount=10), {"expect": {"status": "completed"}}],
    }
    result = sb.run_test(here, "t", test)
    assert result.status == "passed", result.failures
    (coverage,) = sb.package_coverage(here.definitions.values(), [result])
    out = coverage.out()
    assert out["elements"]["missing"] == ["big", "fallback"]
    # correlate/0 comes with the header every process of these tests shares
    assert out["transitions"] == {"covered": 1, "total": 3, "missing": ["correlate/0", "big:when"]}
    assert out["decisionRows"] == {"covered": 1, "total": 2, "missing": ["level/1"]}
    assert out["handlers"] == {"covered": 0, "total": 1, "missing": ["guarded/catch/0"]}

    other = {
        **test,
        "name": "big and failing",
        "mocks": {"skills": {"work.do@1": [{"error": {"type": "busy"}}]}},
        "steps": [opened(amount=5000), {"expect": {"data": {"note": "fallback"}}}],
    }
    second = sb.run_test(here, "t", other)
    assert second.status == "passed", second.failures
    (both,) = sb.package_coverage(here.definitions.values(), [result, second])
    out = both.out()
    assert [out[name]["missing"] for name in ("elements", "decisionRows", "handlers")] == [
        [],
        [],
        [],
    ]
    assert out["transitions"]["missing"] == ["correlate/0"]


def test_a_test_below_its_coverage_minimum_fails() -> None:
    result = run(
        BRANCHES,
        [opened(amount=10)],
        mocks={"skills": {"work.do@1": [{"output": {"value": "v"}}]}},
        coverage={"minimum": 100},
    )
    assert result.status == "failed"
    assert result.failures[0].actual["missing"] == ["big", "fallback"]


# --- nothing leaves the sandbox --------------------------------------------------------------


def test_the_sandbox_has_no_client() -> None:
    """The sandbox imports the domain and plain libraries only: no database, HTTP or memory."""
    tree = ast.parse(SANDBOX.read_text(encoding="utf-8"))
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name) or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    project = {name for name in imported if name.startswith("control_plane")}
    assert all(name.startswith("control_plane.domain") for name in project), project
    assert not imported & {"httpx", "sqlalchemy", "boto3", "socket", "urllib.request"}


# --- the trial run on a stand (P014) -------------------------------------------------------

REVIEW = """
stages:
  - id: s
    steps:
      - id: review
        human: {taskType: review, assign: [{role: lead}]}
        output: {as: {decision: step.result.decision}}
      - id: sign
        approve:
          approvers: [{role: lead}]
          quorum: {atLeast: 1}
          separationOfDuties: "[data.author]"
        output: {as: {outcome: step.result.outcome}}
"""


def _live(body: str, *, until: str) -> tuple[sb.LiveInstance, Any]:
    """A live instance as the core keeps it, stopped at the open step ``until``."""
    live = EngineRun(body)
    live.clock = live.clock.replace(day=9)
    live.start(amount=700)
    if until == "sign":
        live.complete_task("review", {"decision": "go"})
    assert live.state is not None
    activity = live.activity(until)
    approvals = (
        (
            {
                "activity": activity["id"],
                "element": "sign",
                "approver": "role:lead",
                "excluded": [AUTHOR],
            },
        )
        if until == "sign"
        else ()
    )
    return (
        sb.LiveInstance(
            id=INSTANCE,
            process="test",
            state=live.state,
            approvals=approvals,
            totals={activity["id"]: 1} if until == "sign" else {},
        ),
        live,
    )


def _trial(body: str, live: sb.LiveInstance, steps: list[dict[str, Any]], **given: Any) -> Any:
    here = dataclasses.replace(world(body), live={live.id: live})
    test = {
        "process": "test",
        "name": "trial",
        "given": {"fromInstance": live.id, "principals": {"lead": [AUTHOR, "bob"]}, **given},
        "steps": steps,
    }
    return sb.run_test(here, "tests/trial.test.yaml", test)


def test_a_trial_run_goes_on_from_a_copy_of_a_live_instance() -> None:
    live, _ = _live(REVIEW, until="review")
    before = copy.deepcopy(live.state)
    steps = [
        {"expect": {"status": "running", "data": {"amount": 700}}},
        {"expect": {"tasks": [{"step": "review", "assignee": "role:lead", "status": "open"}]}},
        {"complete": {"step": "review", "by": "bob", "output": {"decision": "go"}}},
        {"expect": {"data": {"decision": "go"}}},
        {"approve": {"step": "sign", "by": "bob", "decision": "approve"}},
        {"expect": {"status": "completed", "data": {"outcome": "approved"}}},
    ]
    result = _trial(REVIEW, live, steps)
    assert result.status == "passed", result.failures
    assert live.state == before, "the live state is copied, never changed"


def test_a_trial_run_starts_its_clock_at_the_instances_last_input() -> None:
    body = REVIEW.replace(
        "      - id: sign\n",
        '      - {id: stamp, set: {note: "string(instance.clock)"}}\n      - id: sign\n',
    )
    live, source = _live(body, until="review")
    assert source.state is not None
    at = source.state["clock"]
    steps = [
        {"complete": {"step": "review", "by": "bob", "output": {"decision": "go"}}},
        {"expect": {"data": {"note": at}}},
    ]
    assert _trial(body, live, steps).status == "passed"
    given = _trial(body, live, steps, clock="2026-04-01T00:00:00Z")
    assert given.status == "failed" and given.failures[0].actual == "2026-04-01T00:00:00Z"


def test_a_trial_run_takes_the_pending_approvals_of_the_instance() -> None:
    live, _ = _live(REVIEW, until="sign")
    steps = [
        {
            "approve": {
                "step": "sign",
                "by": AUTHOR,
                "decision": "approve",
                "expectRefused": "separation_of_duties_violation",
            }
        },
        {"approve": {"step": "sign", "by": "bob", "decision": "reject"}},
        {"expect": {"status": "completed", "data": {"outcome": "rejected"}}},
    ]
    result = _trial(REVIEW, live, steps)
    assert result.status == "passed", result.failures


def test_a_trial_run_needs_an_instance_of_its_process_and_nothing_else_given() -> None:
    live, _ = _live(REVIEW, until="review")
    other = dataclasses.replace(live, process="other")
    wrong = _trial(REVIEW, other, [{"expect": {"status": "running"}}])
    assert wrong.status == "failed" and "of process 'other'" in wrong.failures[0].message
    mixed = _trial(REVIEW, live, [{"expect": {"status": "running"}}], data={"amount": 1})
    assert mixed.status == "failed" and "no given.data" in mixed.failures[0].message
    missing = sb.run_test(
        world(REVIEW),
        "tests/trial.test.yaml",
        {"process": "test", "name": "t", "given": {"fromInstance": INSTANCE}, "steps": []},
    )
    assert missing.status == "failed" and "no instance" in missing.failures[0].message


# --- the retrospective of a case and the next case (SC-013) --------------------------------


LESSONS = """
memory:
  case: {key: "'case:' + data.number", title: "'Case ' + data.number"}
  entities: [{kind: legal_entity, key: data.author, name: "'Customer'", rel: customer}]
retrospective: {taskType: lessons, assign: [{role: lead}], appliesTo: [legal_entity]}
stages:
  - id: s
    steps:
      - id: history
        recall: {anchors: [{kind: legal_entity, key: data.author}], kinds: [lesson]}
        output: {as: {history: step.result.nodes}}
      - {id: finish, complete: {outcome: lost}}
"""


class MemoryStub:
    """Memory as the core writes it and a recall reads it: nodes and facts by entity keys.

    What a ``remember`` intent writes is the core's own observation of it
    (:func:`remembered`); a recall anchored on an entity finds the lessons
    that apply to it (CP-ADR-0076 §6: entity ← ``applies_to`` — lesson).
    """

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.facts: list[dict[str, str]] = []

    def write(self, intent: dict[str, Any]) -> None:
        instance = SimpleNamespace(id=INSTANCE, definition_key="test")
        observation = remembered(intent, "case", instance)  # type: ignore[arg-type]
        for assertion in observation["assertions"]:
            if assertion["assert"] == "entity":
                entity = assertion["entity"]
                self.nodes.setdefault(entity["key"], {}).update(entity)
            else:
                self.facts.append(assertion["fact"])

    def answer(self, request: dict[str, Any]) -> dict[str, Any]:
        anchors = {entity_key(str(a["kind"]), str(a["key"])) for a in request["anchors"]}
        found = [f for f in self.facts if f["predicate"] == "applies_to" and f["object"] in anchors]
        nodes = [
            {
                "kind": self.nodes[f["subject"]]["type"],
                "key": f["subject"],
                "text": self.nodes[f["subject"]]["properties"]["text"],
            }
            for f in found
            if self.nodes[f["subject"]]["type"] in (request.get("kinds") or ["lesson"])
        ]
        edges = [{"relation": "applies_to", "from": f["subject"], "to": f["object"]} for f in found]
        return {"nodes": nodes, "edges": edges}


class SandboxWithMemory(sb.Sandbox):
    """The sandbox whose remember and recall go to one memory, across tests."""

    memory: MemoryStub

    def do_recall(self, instance: Any, body: dict[str, Any]) -> None:
        answer = {"activityId": body["activityId"], "status": "completed"}
        self.queue.append(
            (instance.id, "recall", {**answer, "result": self.memory.answer(body)}, None)
        )

    def do_remember(self, instance: Any, body: dict[str, Any]) -> None:
        super().do_remember(instance, body)
        self.memory.write(body)


def _with_memory(
    here: sb.World, memory: MemoryStub, steps: list[dict[str, Any]], **test: Any
) -> SandboxWithMemory:
    sandbox = SandboxWithMemory(
        here, {"process": "test", "name": "t", "steps": steps, **test}, seed="t"
    )
    sandbox.memory = memory
    sandbox.start_given()
    for index, step in enumerate(steps):
        failures = sb._run_step(sandbox, index, step)
        assert not failures, [f.out() for f in failures]
    return sandbox


def test_a_lesson_of_a_closed_case_is_recalled_by_the_next_case_of_the_customer() -> None:
    contract = retrospective_contract()
    skills = {
        **CATALOG.skills,
        "process.retrospective@1": SkillEntry(contract["inputs"], contract["outputs"]),
    }
    here = dataclasses.replace(world(LESSONS), skills=skills)
    memory = MemoryStub()
    evidence = [{"seq": 1, "eventId": None}]
    proposed = {
        "case": "case:N-1",
        "lessons": [
            {
                "key": "lesson:case:N-1/late",
                "text": "The customer asks for documents late: ask a week earlier",
                "appliesTo": [{"kind": "legal_entity", "key": AUTHOR}],
                "evidence": evidence,
            },
            {
                "key": "lesson:case:N-1/noise",
                "text": "Nothing to learn",
                "appliesTo": [{"kind": "case", "key": "case:N-1"}],
                "evidence": evidence,
            },
        ],
        "dropped": [],
    }
    # The mock answers only an input by the skill's contract: the sandbox refuses others.
    mocks = {"skills": {"process.retrospective@1": [{"output": proposed}]}}
    first = _with_memory(
        here,
        memory,
        [
            opened(number="N-1"),
            {
                "expect": {
                    "status": "completed",
                    "data": {"history": []},
                    "tasks": [{"step": "retrospective", "status": "open"}],
                }
            },
        ],
        given={"principals": {"lead": ["alice"]}},
        mocks=mocks,
    )
    (review,) = [t for t in first.tasks if t.element == "retrospective"]
    lessons = [
        {**lesson, "appliesTo": [{"kind": "legal_entity", "key": AUTHOR}], "decision": decision}
        for lesson, decision in zip(proposed["lessons"], ["confirm", "reject"], strict=True)
    ]
    first.complete({"step": "retrospective", "by": "alice", "output": {"lessons": lessons}})
    assert review.status == "completed"
    (lesson,) = [r["entity"] for r in first.remembered]
    assert lesson["key"] == "lesson:case:N-1/late"
    assert lesson["links"] == [
        {"rel": "learned_from", "kind": "case", "key": "case:N-1"},
        {"rel": "applies_to", "kind": "legal_entity", "key": AUTHOR},
    ]

    second = _with_memory(
        here,
        memory,
        [opened(number="N-2"), {"expect": {"status": "completed"}}],
        given={"principals": {"lead": ["alice"]}},
    )
    (instance,) = second.instances.values()
    assert instance.state is not None
    assert instance.state["data"]["history"] == [
        {
            "kind": "lesson",
            "key": "lesson:lesson:case:N-1/late",
            "text": "The customer asks for documents late: ask a week earlier",
        }
    ]
    other = _with_memory(
        here,
        memory,
        [opened(number="N-3", author=OTHER), {"expect": {"data": {"history": []}}}],
        given={"principals": {"lead": ["alice"]}},
    )
    assert other.remembered == []
