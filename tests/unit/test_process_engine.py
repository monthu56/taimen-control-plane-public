"""The pure step function of the process engine (CP-ADR-0074 §4-§8; process-packages P007).

Every test runs a small process through :func:`process_engine.step` the way
the application layer will: one input at a time, the state stored as JSON
(``jsonb`` sorts keys) between inputs. The harness checks every emitted
``process.*`` event against its schema in the event catalog.
"""

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from control_plane.domain import process_definition as pd
from control_plane.domain import process_engine as pe
from control_plane.domain import process_replay
from control_plane.domain.calendar import Calendar
from control_plane.domain.event_catalog import current_version, schema_for
from control_plane.domain.process_definition import Catalog, SkillEntry
from control_plane.domain.process_engine import Definition, Input
from tests.unit.test_process_contract import PROCESS, _yaml12_loader, retrospective_contract

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
CALENDARS = {
    "ru": Calendar.from_spec(
        yaml.load((FIXTURES / "ru.calendar.yaml").read_text(), Loader=_yaml12_loader())["spec"]
    )
}
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
INSTANCE = "0b9f7f0e-6a57-4c2e-9d0f-3f1f1d6a7c11"
AUTHOR = "7d1c1c5e-2f57-4d3a-8e57-2a4c6b1f0a01"
OTHER = "9a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d"

CATALOG = Catalog(
    skills={
        "work.do@1": SkillEntry(
            {"type": "object", "properties": {"text": {"type": "string"}}},
            {"type": "object", "properties": {"value": {"type": "string"}}},
        ),
        "process.retrospective@1": SkillEntry(None, None),
    },
    task_types={
        "review": {"type": "object", "properties": {"decision": {"type": "string"}}},
        "lessons": None,
        "sign-off": None,
    },
    agents=frozenset({"proc", "helper"}),
    calendars=frozenset({"ru"}),
    artifact_types=frozenset(),
    processes=frozenset({"child"}),
)

HEAD = """
version: 1
displayName: Test process
identity: {agent: proc}
owner: [{role: lead}]
calendar: ru
data:
  type: object
  properties:
    number: {type: string}
    amount: {type: number}
    deadline: {type: string, format: date-time}
    author: {type: string}
    decision: {type: string}
    note: {type: string}
    level: {type: number}
    count: {type: integer}
    trail: {type: array, items: {type: string}}
    history: {type: array}
    levels: {type: array}
    outcome: {type: string}
    value: {type: string}
start:
  on: {observation: case.opened}
  key: event.payload.number
  set:
    number: string(event.payload.number)
    amount: double(event.payload.amount)
    deadline: timestamp(event.payload.deadline)
    author: string(event.payload.author)
correlate:
  - on: {observation: case.changed}
    key: event.payload.number
    set: {deadline: timestamp(event.payload.deadline)}
"""


def spec(body: str) -> dict[str, Any]:
    loader = _yaml12_loader()
    out = yaml.load(HEAD, Loader=loader)
    out.update(yaml.load(body, Loader=loader))
    return out


def _time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class Run:
    """One instance driven input by input, its state stored as JSON between them."""

    def __init__(self, body: str, *, cost_limit: int | None = None) -> None:
        extra = {} if cost_limit is None else {"cost_limit": cost_limit}
        self.definition = Definition.build("test", pd.normalized_spec(spec(body)), CATALOG, **extra)
        self.state: dict[str, Any] | None = None
        self.clock = T0
        self.records: list[tuple[Input, list[pe.Decision], list[pe.Intent]]] = []
        self.events_made = 0

    # --- feeding ----------------------------------------------------------------------

    def feed(
        self,
        kind: str,
        body: dict[str, Any] | None = None,
        *,
        at: datetime | None = None,
        actor: str | None = None,
    ) -> tuple[list[pe.Decision], list[pe.Intent]]:
        self.clock = at or self.clock
        given = Input(kind, self.clock, body or {}, actor, CALENDARS)
        before = copy.deepcopy(self.state)
        state, decisions, intents = pe.step(self.definition, self.state, given)
        assert self.state == before, "step must not change the state it was given"
        self.state = json.loads(json.dumps(state, sort_keys=True))
        self.records.append((given, decisions, intents))
        for intent in intents:
            if intent.kind == "emit_event":
                _check_event(intent.body["type"], intent.body["payload"])
        return decisions, intents

    def event_doc(self, observation: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.events_made += 1
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"event-{self.events_made}")),
            "type": "observation.recorded",
            "time": _time(self.clock),
            "observation": observation,
            "payload": payload,
        }

    def start(self, **payload: Any) -> tuple[list[pe.Decision], list[pe.Intent]]:
        payload = {
            "number": "N-1",
            "amount": 100,
            "deadline": "2026-05-04T09:00:00Z",
            "author": AUTHOR,
            **payload,
        }
        return self.feed(
            "start", {"instanceId": INSTANCE, "event": self.event_doc("case.opened", payload)}
        )

    def observe(
        self, observation: str, **payload: Any
    ) -> tuple[list[pe.Decision], list[pe.Intent]]:
        return self.feed("event", {"event": self.event_doc(observation, payload)})

    def activity(self, element: str) -> dict[str, Any]:
        assert self.state is not None
        found = [a for a in self.state["activities"].values() if a["element"] == element]
        assert len(found) == 1, f"{element}: {found}"
        return found[0]

    def complete_task(self, element: str, fields: dict[str, Any], **kw: Any) -> Any:
        activity = self.activity(element)
        self.events_made += 1
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"task-{self.events_made}"))
        task = {"id": task_id, "status": "done", "customFields": fields}
        return self.feed(
            "task",
            {"activityId": activity["id"], "status": kw.pop("status", "completed"), "task": task},
            **kw,
        )

    def skill(self, element: str, output: Any = None, error: Any = None, **kw: Any) -> Any:
        activity = self.activity(element)
        body = {"activityId": activity["id"], "status": "failed" if error else "succeeded"}
        body["error" if error else "output"] = error if error else output
        return self.feed("skill", body, **kw)

    def vote(self, element: str, principal: str, outcome: str, total: int) -> Any:
        activity = self.activity(element)
        return self.feed(
            "approval",
            {
                "activityId": activity["id"],
                "approvalId": f"approval-{principal}",
                "principal": principal,
                "outcome": outcome,
                "total": total,
            },
        )

    def timer(self, element: str, kind: str | None = None) -> dict[str, Any]:
        assert self.state is not None
        found = [
            t
            for t in self.state["timers"].values()
            if t["element"] == element and (kind is None or t["kind"] == kind)
        ]
        assert len(found) == 1, f"{element}: {found}"
        return found[0]

    def fire(self, element: str, kind: str | None = None) -> Any:
        timer = self.timer(element, kind)
        due = datetime.fromisoformat(timer["dueAt"].replace("Z", "+00:00"))
        return self.feed("timer", {"timerId": timer["id"]}, at=max(due, self.clock))

    def command(self, action: str, **body: Any) -> Any:
        return self.feed("command", {"action": action, **body}, actor=AUTHOR)

    # --- reading ----------------------------------------------------------------------

    @property
    def data(self) -> dict[str, Any]:
        assert self.state is not None
        return dict(self.state["data"])

    @property
    def status(self) -> str:
        assert self.state is not None
        return str(self.state["status"])

    def intents(self, kind: str) -> list[dict[str, Any]]:
        return [i.out() for _, _, intents in self.records for i in intents if i.kind == kind]

    def decisions(self, kind: str) -> list[dict[str, Any]]:
        return [d.out() for _, decisions, _ in self.records for d in decisions if d.kind == kind]

    def events(self, event_type: str | None = None) -> list[dict[str, Any]]:
        found = self.intents("emit_event")
        if event_type is None:
            return found
        return [e["payload"] for e in found if e["type"] == event_type]

    def event_types(self) -> list[str]:
        return [e["type"] for e in self.intents("emit_event")]


def _check_event(event_type: str, payload: dict[str, Any]) -> None:
    schema = schema_for(event_type, current_version(event_type))
    assert schema is not None, event_type
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload))
    assert not errors, f"{event_type}: {[e.message for e in errors]}"


# The same step in block style (after "      - id: review") and in flow style.
HUMAN_REVIEW = """
        human:
          taskType: review
          assign: [{role: lead}]
        output: {as: {decision: step.result.decision}}
"""
REVIEW = (
    "{id: review, human: {taskType: review, assign: [{role: lead}]},"
    " output: {as: {decision: step.result.decision}}}"
)
ONE_REVIEW = f"stages: [{{id: s, steps: [{REVIEW}]}}]"


# --- start, correlate, the case -------------------------------------------------------------


def test_start_sets_data_enters_stages_projects_the_case_and_completes() -> None:
    run = Run(
        """
memory:
  case: {key: "'case:' + data.number", title: data.number}
  facts: {amount: data.amount}
  entities: [{kind: person, key: data.author, rel: author}]
stages:
  - id: first
    steps:
      - {id: note, set: {note: "'hello ' + data.number"}}
"""
    )
    run.start()
    assert run.status == "completed" and run.state is not None
    assert run.state["outcome"] == "completed"
    assert run.data["note"] == "hello N-1" and run.data["amount"] == 100
    assert run.data["deadline"] == "2026-05-04T09:00:00Z"
    assert run.event_types() == [
        "process.started",
        "process.stage_entered",
        "process.stage_exited",
        "process.data_changed",
        "process.completed",
    ]
    started = run.events("process.started")[0]
    assert started["instanceKey"] == "N-1" and started["triggerType"] == "observation:case.opened"
    assert started["memory"] == {
        "case": {"kind": "case", "key": "case:N-1", "title": "N-1"},
        "facts": {"amount": 100},
        "entities": [{"kind": "person", "key": AUTHOR, "name": None, "rel": "author"}],
        "documents": {},
    }
    assert run.intents("complete") == [
        {"kind": "complete", "status": "completed", "outcome": "completed", "error": None}
    ]


def test_the_start_key_and_correlation_keys_for_the_application() -> None:
    run = Run("stages: [{id: s, steps: [{id: a, set: {note: \"'x'\"}}]}]")
    opened = run.event_doc("case.opened", {"number": 42})
    changed = run.event_doc("case.changed", {"number": "N-9", "deadline": "2026-01-01T00:00:00Z"})
    assert pe.start_key(run.definition, opened) == "42"
    assert pe.start_key(run.definition, changed) is None
    assert pe.correlation_keys(run.definition, changed) == ["N-9"]
    assert pe.correlation_keys(run.definition, opened) == []


def test_a_repeated_start_event_reaches_the_instance_as_correlated() -> None:
    run = Run(ONE_REVIEW)
    run.start()
    assert run.state is not None
    first = copy.deepcopy(run.state)
    decisions, _ = run.start()
    assert [d.kind for d in decisions] == ["correlated"]
    assert run.events("process.correlated")[-1]["changedFields"] == []
    assert run.state["instanceId"] == first["instanceId"] and run.state["data"] == first["data"]
    assert len(run.intents("create_task")) == 1


def test_correlate_changes_data_and_reschedules_the_timers_that_read_it() -> None:
    run = Run(
        """
stages:
  - id: s
    timers:
      - id: before-deadline
        at: {at: "data.deadline - duration('P1D')"}
        do: [{id: warn, set: {note: "'warned'"}}]
    steps:
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start()
    assert run.timer("before-deadline")["dueAt"] == "2026-05-03T09:00:00Z"
    run.observe("case.changed", number="N-1", deadline="2026-05-11T09:00:00Z")
    correlated = run.events("process.correlated")[-1]
    assert correlated["changedFields"] == ["deadline"]
    moved = run.events("process.timer_rescheduled")[-1]
    assert (moved["previousDueAt"], moved["dueAt"]) == (
        "2026-05-03T09:00:00Z",
        "2026-05-10T09:00:00Z",
    )
    assert moved["cause"] == "data_changed" and moved["changedFields"] == ["deadline"]
    assert run.intents("set_timer")[-1]["dueAt"] == "2026-05-10T09:00:00Z"

    run.fire("before-deadline")
    assert run.data["note"] == "warned"
    # A fired timer does not roll back when the date moves again (spec, edge cases).
    run.observe("case.changed", number="N-1", deadline="2026-06-11T09:00:00Z")
    assert run.data["note"] == "warned" and len(run.events("process.timer_rescheduled")) == 1


def test_an_event_of_another_key_or_kind_is_ignored() -> None:
    run = Run(ONE_REVIEW)
    run.start()
    decisions, intents = run.observe("case.changed", number="N-2", deadline="2026-01-01T00:00:00Z")
    assert [d.out()["reason"] for d in decisions] == ["no_match"] and intents == []


def test_stages_enter_and_exit_by_guards_and_reach_milestones() -> None:
    run = Run(
        """
stages:
  - id: screen
    milestones: [{id: decided, when: "has(data.decision)"}]
    exit: has(data.decision)
    steps:
      - id: review
"""
        + HUMAN_REVIEW
        + """
  - id: go
    entry: "stage.screen.completed && data.decision == 'go'"
    steps: [{id: finish, complete: {outcome: done}}]
  - id: stop
    entry: "stage.screen.completed && data.decision == 'no-go'"
    steps: [{id: halt, complete: {outcome: declined}}]
"""
    )
    run.start()
    assert run.state is not None
    assert {s: r["state"] for s, r in run.state["stages"].items()} == {
        "screen": "active",
        "go": "available",
        "stop": "available",
    }
    run.complete_task("review", {"decision": "go"})
    assert run.status == "completed" and run.state["outcome"] == "done"
    assert run.events("process.milestone_reached") == [
        {**run.events("process.milestone_reached")[0], "milestone": "decided", "stage": "screen"}
    ]
    kinds = [d["kind"] for d in run.decisions("stage_entered") + run.decisions("stage_exited")]
    assert kinds.count("stage_entered") == 2 and "stop" not in {
        d["element"] for d in run.decisions("stage_entered")
    }


def test_an_exit_guard_ends_the_open_work_of_its_stage() -> None:
    run = Run(
        """
stages:
  - id: s
    exit: "data.deadline > timestamp('2026-06-01T00:00:00Z')"
    timers: [{id: t, at: P10D, do: [{id: w, set: {note: "'late'"}}]}]
    steps:
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start()
    task = run.activity("review")
    timer = run.timer("t")
    run.observe("case.changed", number="N-1", deadline="2026-07-01T09:00:00Z")
    assert run.intents("cancel_task") == [
        {
            "kind": "cancel_task",
            "activityId": task["id"],
            "element": "review",
            "reason": "stage_exited",
        }
    ]
    assert {"kind": "cancel_timer", "timerId": timer["id"], "element": "t"} in run.intents(
        "cancel_timer"
    )
    assert run.status == "completed"


def test_discretionary_work_starts_by_an_operator_command() -> None:
    run = Run(
        """
stages:
  - id: s
    discretionary: [{id: extra, set: {note: "'added'"}}]
    steps:
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start()
    run.command("start_discretionary", stage="s", step="extra")
    assert run.data["note"] == "added"
    assert run.decisions("discretionary_started")[0]["actor"] == AUTHOR
    with pytest.raises(pe.EngineError):
        run.command("start_discretionary", stage="s", step="review")


def test_a_repeatable_stage_enters_again_on_a_later_input() -> None:
    run = Run(
        """
stages:
  - id: main
    steps:
      - id: review
"""
        + HUMAN_REVIEW
        + """
  - id: again
    repeatable: true
    entry: "data.deadline > timestamp('2026-06-01T00:00:00Z')"
    exit: "true"
    steps: [{id: bump, set: {note: "'bumped'"}}]
"""
    )
    run.start()
    run.observe("case.changed", number="N-1", deadline="2026-07-01T09:00:00Z")
    run.observe("case.changed", number="N-1", deadline="2026-08-01T09:00:00Z")
    # Entered on each of the two later inputs, never twice on one.
    assert run.state is not None and run.state["stages"]["again"]["runs"] == 2
    assert run.data["note"] == "bumped"


# --- human steps, timers, escalation, calendar ---------------------------------------------


def test_a_human_step_asks_for_a_task_with_form_assignment_due_escalation_and_context() -> None:
    run = Run(
        """
memory:
  case: {key: "'case:' + data.number"}
stages:
  - id: s
    steps:
      - id: review
        displayName: Review the case
        human:
          taskType: review
          title: "'Review ' + data.number"
          form:
            schema:
              type: object
              required: [decision]
              properties: {decision: {enum: [go, no-go]}}
          assign: [{expr: "'role:' + 'lead'"}, {expr: "'agent:helper'"}, {expr: data.author}]
          due: {at: "cal.addWorkdays(data.deadline, -3)"}
          escalations: [{after: due, action: notify, to: [{role: boss}]}]
          context:
            anchors: [{case: true}, {kind: person, key: data.author}]
            traverse: [{relation: applies_to, direction: in}]
        output: {as: {decision: step.result.decision}}
"""
    )
    run.start()
    (task,) = run.intents("create_task")
    activity = run.activity("review")
    assert task == {
        "kind": "create_task",
        "activityId": activity["id"],
        "element": "review",
        "taskType": "review",
        "title": "Review N-1",
        "form": {
            "schema": {
                "type": "object",
                "required": ["decision"],
                "properties": {"decision": {"enum": ["go", "no-go"]}},
            }
        },
        "assign": [{"role": "lead"}, {"agent": "helper"}, {"principal": AUTHOR}],
        # Three working days before Monday 4 May 2026: 1 May is a holiday.
        "due": "2026-04-28T09:00:00Z",
        "escalations": [{"level": 1, "after": "due", "action": "notify", "to": [{"role": "boss"}]}],
        "context": {
            "anchors": [
                {"case": True, "kind": "case", "key": "case:N-1"},
                {"kind": "person", "key": AUTHOR},
            ],
            "traverse": [{"relation": "applies_to", "direction": "in"}],
            "semantic": True,
        },
        "input": None,
        "externalRef": f"process/{INSTANCE}/review",
    }
    assert run.timer("review", "escalation")["dueAt"] == "2026-04-28T09:00:00Z"

    run.complete_task("review", {"decision": "go"})
    assert run.data["decision"] == "go" and run.status == "completed"
    # The finished step's escalation timer is gone with it.
    assert any(i["element"] == "review" for i in run.intents("cancel_timer"))


def test_a_task_result_outside_the_form_is_an_error_of_the_step() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: review
        human:
          taskType: review
          form: {schema: {type: object, properties: {decision: {enum: [go, no-go]}}}}
          assign: [{role: lead}]
        output: {as: {decision: step.result.decision}}
"""
    )
    run.start()
    run.complete_task("review", {"decision": "maybe"})
    assert run.status == "failed" and run.state is not None
    assert (
        run.state["error"]["type"] == "form_invalid" and run.state["error"]["element"] == "review"
    )
    assert run.events("process.failed")[0]["error"]["type"] == "form_invalid"


def test_escalation_levels_notify_reassign_and_raise() -> None:
    run = Run(
        f"""
stages:
  - id: s
    steps:
      - id: guarded
        try:
          do:
            - id: review
              human:
                taskType: review
                assign: [{{role: lead}}]
                due: P1D
                escalations:
                  - {{after: due, action: notify, to: [{{role: boss}}]}}
                  - {{after: P1D, action: reassign, to: [{{principal: "{OTHER}"}}]}}
                  - {{after: P3D, action: raise, error: {{type: overdue, status: 408}}}}
          catch: [{{errors: {{type: overdue}}, do: [{{id: late, set: {{note: "'overdue'"}}}}]}}]
"""
    )
    run.start()
    task = run.activity("review")
    assert run.state is not None
    timers = run.state["timers"].values()
    levels = sorted((t for t in timers if t["kind"] == "escalation"), key=lambda t: t["level"])
    assert [t["dueAt"] for t in levels] == [
        "2026-03-03T09:00:00Z",
        "2026-03-04T09:00:00Z",
        "2026-03-06T09:00:00Z",
    ]
    for level in levels:
        run.feed("timer", {"timerId": level["id"]}, at=datetime.fromisoformat(level["dueAt"]))
    escalated = run.events("process.escalated")
    assert [(e["level"], e["action"], e["to"]) for e in escalated] == [
        (1, "notify", ["role:boss"]),
        (2, "reassign", [OTHER]),
        (3, "raise", [OTHER]),  # to the assignee the second level chose
    ]
    assert run.intents("reassign_task") == [
        {
            "kind": "reassign_task",
            "activityId": task["id"],
            "element": "review",
            "assign": [{"principal": OTHER}],
            "level": 2,
        }
    ]
    assert run.intents("cancel_task")[0]["reason"] == "error"
    assert run.data["note"] == "overdue" and run.status == "completed"


def test_a_due_on_a_provisional_calendar_year_is_marked() -> None:
    run = Run(
        """
stages:
  - id: s
    timers:
      - {id: t, at: {at: "cal.addWorkdays(data.deadline, -2)"}, do: [{id: n, set: {note: "'x'"}}]}
    steps:
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start(deadline="2027-03-10T09:00:00Z")
    assert run.timer("t")["provisional"] is True
    assert run.decisions("timer_set")[0]["provisional"] is True


def test_a_new_calendar_version_reschedules_the_timers_that_call_it() -> None:
    run = Run(
        """
stages:
  - id: s
    timers:
      - {id: t, at: {at: "cal.addWorkdays(data.deadline, -1)"}, do: [{id: n, set: {note: "'x'"}}]}
    steps:
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start(deadline="2026-05-05T09:00:00Z")
    assert run.timer("t")["dueAt"] == "2026-05-04T09:00:00Z"
    moved = copy.deepcopy(
        yaml.load((FIXTURES / "ru.calendar.yaml").read_text(), Loader=_yaml12_loader())["spec"]
    )
    moved["years"][0]["holidays"].append("2026-05-04")
    given = Input("calendar", T0, {"key": "ru"}, None, {"ru": Calendar.from_spec(moved)})
    state, _, intents = pe.step(run.definition, run.state, given)
    assert state["timers"][run.timer("t")["id"]]["dueAt"] == "2026-04-30T09:00:00Z"
    rescheduled = [i.body for i in intents if i.kind == "emit_event"]
    assert rescheduled[0]["payload"]["cause"] == "calendar_changed"


# --- suspend and resume -------------------------------------------------------------------


SUSPENDABLE = """
onEvent:
  - on: {observation: case.suspended}
    do: [{id: pause, suspend: {reason: "'complaint'"}}]
  - on: {observation: case.resumed}
    do: [{id: unpause, resume: {}}]
stages:
  - id: s
    timers: [{id: at-deadline, at: {at: data.deadline}, do: [{id: n, set: {note: "'deadline'"}}]}]
    steps:
      - id: review
        human:
          taskType: review
          assign: [{role: lead}]
          due: P2D
          escalations: [{after: due, action: remind}]
        output: {as: {decision: step.result.decision}}
      - {id: after, set: {note: "'reviewed'"}}
"""


def test_suspension_freezes_timers_and_resume_shifts_them_by_its_length() -> None:
    run = Run(SUSPENDABLE)
    run.start()
    assert run.timer("review")["dueAt"] == "2026-03-04T09:00:00Z"
    run.clock = T0 + timedelta(days=1)
    run.observe("case.suspended", number="N-1")
    assert run.status == "suspended"
    assert run.events("process.suspended")[0]["reason"] == "complaint"
    escalation = run.timer("review")
    assert (escalation["state"], escalation["dueAt"], escalation["remaining"]) == (
        "frozen",
        None,
        86400.0,
    )
    # A timer from a date in the data keeps no remainder: it is computed again from the data.
    assert run.timer("at-deadline")["remaining"] is None

    # Work finished during the suspension waits for the resume.
    run.clock = T0 + timedelta(days=2)
    decisions, _ = run.complete_task("review", {"decision": "go"})
    assert [d.kind for d in decisions] == ["deferred"] and "decision" not in run.data

    run.clock = T0 + timedelta(days=3)
    run.observe("case.resumed", number="N-1")
    moved = {e["element"]: e for e in run.events("process.timer_rescheduled")}
    assert moved["review"]["previousDueAt"] == "2026-03-04T09:00:00Z"
    assert moved["review"]["dueAt"] == "2026-03-06T09:00:00Z"  # one day left, from the resume
    assert moved["at-deadline"]["dueAt"] == "2026-05-04T09:00:00Z"
    assert moved["review"]["cause"] == "resumed"
    # The deferred completion was taken after the resume, and the stage finished its work.
    assert run.data["decision"] == "go" and run.data["note"] == "reviewed"
    assert run.status == "completed"


def test_an_operator_suspends_and_resumes_and_stale_commands_are_recorded() -> None:
    run = Run(SUSPENDABLE)
    run.start()
    run.command("suspend", reason="hold")
    decisions, _ = run.command("suspend")
    assert decisions[0].out()["reason"] == "not_running"
    timer = run.timer("review")
    decisions, _ = run.feed("timer", {"timerId": timer["id"]})
    assert decisions[0].out()["reason"] == "stale"  # a frozen timer does not fire
    run.command("resume")
    assert run.status == "running"
    assert run.events("process.resumed")[0]["cause"] == "operator"


# --- blocks ---------------------------------------------------------------------------------


def test_do_runs_in_order_and_when_skips_a_step() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: group
        do:
          - {id: one, set: {note: "'one'"}}
          - {id: skipped, when: "data.amount > 1000.0", set: {note: "'big'"}}
          - {id: two, set: {level: "data.amount * 2.0"}}
"""
    )
    run.start()
    assert run.data["note"] == "one" and run.data["level"] == 200
    assert [d["element"] for d in run.decisions("step_skipped")] == ["skipped"]


def test_fork_all_waits_for_every_branch() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: both
        fork:
          branches:
            - id: left
              do:
                - """
        + REVIEW
        + """
            - id: right
              do: [{id: r, set: {note: "'right'"}}]
      - {id: after, set: {level: "1.0"}}
"""
    )
    run.start()
    assert run.data["note"] == "right" and "level" not in run.data
    run.complete_task("review", {"decision": "go"})
    assert run.data["level"] == 1 and run.status == "completed"
    assert run.decisions("fork_completed")[0]["finished"] == ["right", "left"]


def test_fork_compete_ends_the_losing_branches() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: race
        fork:
          mode: compete
          branches:
            - id: by-person
              do:
                - """
        + REVIEW
        + """
            - id: by-event
              do:
                - id: wait-answer
                  listen:
                    any: [{on: {observation: case.answered}}]
                - {id: answered, set: {note: event.payload.text}}
"""
    )
    run.start()
    task = run.activity("review")
    run.observe("case.answered", number="N-1", text="answer")
    assert run.data["note"] == "answer" and run.status == "completed"
    assert run.intents("cancel_task") == [
        {
            "kind": "cancel_task",
            "activityId": task["id"],
            "element": "review",
            "reason": "branch_lost",
        }
    ]


LISTEN = """
stages:
  - id: s
    steps:
      - id: wait
        listen:
          any:
            - on: {observation: case.answered, where: "event.payload.ok == true"}
              do: [{id: took, set: {note: event.payload.text}}]
            - on: {observation: case.withdrawn}
              do: [{id: gone, set: {note: "'withdrawn'"}}]
          timeout: P5D
          onTimeout: [{id: silent, set: {note: "'timeout'"}}]
        output: {as: {value: string(step.result.option)}}
      - {id: after, set: {outcome: "event.type == '' ? 'none' : 'event'"}}
"""


def test_listen_takes_the_first_matching_event_and_cancels_its_timeout() -> None:
    run = Run(LISTEN)
    run.start()
    timeout = run.timer("wait")
    decisions, _ = run.observe("case.answered", number="N-1", ok=False, text="no")
    assert [d.kind for d in decisions] == ["ignored"]
    run.observe("case.answered", number="N-1", ok=True, text="yes")
    assert run.data["note"] == "yes" and run.data["value"] == "0"
    assert {"kind": "cancel_timer", "timerId": timeout["id"], "element": "wait"} in run.intents(
        "cancel_timer"
    )
    run2 = Run(LISTEN)
    run2.start()
    run2.observe("case.withdrawn", number="N-1")
    assert run2.data["note"] == "withdrawn" and run2.data["value"] == "1"


def test_listen_without_an_event_in_time_runs_on_timeout() -> None:
    run = Run(LISTEN)
    run.start()
    run.fire("wait")
    assert run.data["note"] == "timeout" and "value" not in run.data
    assert run.status == "completed"
    assert run.decisions("step_timed_out")[0]["element"] == "wait"


RETRY = """
stages:
  - id: s
    steps:
      - id: attempt
        try:
          do:
            - id: work
              call: {skill: work.do@1, input: {text: data.number}}
              output: {as: {value: step.result.value}}
          retry: {limit: 2, delay: PT1M, backoff: exponential, on: [boom]}
          catch:
            - errors: {type: boom}
              as: err
              do: [{id: caught, set: {note: "err.type + ':' + string(err.status)"}}]
"""


def test_try_retries_with_backoff_then_catches_with_the_error_bound() -> None:
    run = Run(RETRY)
    run.start()
    assert run.intents("invoke_skill")[0]["input"] == {"text": "N-1"}
    run.skill("work", error={"code": "boom", "message": "no"})
    assert run.timer("attempt", "retry")["dueAt"] == "2026-03-02T09:01:00Z"
    run.fire("attempt", "retry")
    run.skill("work", error={"code": "boom", "message": "no"})
    assert run.timer("attempt", "retry")["dueAt"] == "2026-03-02T09:03:00Z"
    run.fire("attempt", "retry")
    assert len(run.intents("invoke_skill")) == 3
    run.skill("work", error={"code": "boom", "message": "no"})
    assert run.data["note"] == "boom:502" and run.status == "completed"
    assert [d["attempt"] for d in run.decisions("retry_scheduled")] == [1, 2]


def test_try_passes_a_success_through() -> None:
    run = Run(RETRY)
    run.start()
    run.skill("work", output={"value": "ok"})
    assert run.data["value"] == "ok" and "note" not in run.data and run.status == "completed"


def test_an_error_no_handler_takes_fails_the_instance_and_ends_its_work() -> None:
    run = Run(
        """
stages:
  - id: waiting
    steps:
      - id: review
"""
        + HUMAN_REVIEW
        + """
  - id: broken
    steps:
      - {id: boom, raise: {type: bad-input, status: 400, detail: "'number ' + data.number"}}
"""
    )
    run.start()
    assert run.status == "failed" and run.state is not None
    assert run.state["error"] == {
        "type": "bad-input",
        "status": 400,
        "detail": "number N-1",
        "element": "boom",
    }
    assert run.events("process.failed")[0]["element"] == "boom"
    assert run.intents("cancel_task")[0]["element"] == "review"
    assert run.intents("complete")[0]["status"] == "failed"
    decisions, _ = run.observe("case.changed", number="N-1", deadline="2026-01-01T00:00:00Z")
    assert decisions[0].out()["reason"] == "instance_closed"


def test_an_error_in_a_fork_branch_goes_up_to_the_try_around_the_fork() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: guarded
        try:
          do:
            - id: split
              fork:
                branches:
                  - id: slow
                    do:
                      - id: review
                        human: {taskType: review, assign: [{role: lead}]}
                  - id: failing
                    do: [{id: nope, raise: {type: broken}}]
          catch: [{do: [{id: handled, set: {note: "'handled'"}}]}]
"""
    )
    run.start()
    assert run.data["note"] == "handled" and run.status == "completed"
    assert run.intents("cancel_task")[0]["reason"] == "branch_failed"


def test_call_timeout_is_an_error_and_a_late_answer_is_stale() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: guarded
        try:
          do:
            - id: work
              call: {skill: work.do@1, input: {text: data.number}, timeout: PT10M}
          catch: [{errors: {type: timeout}, do: [{id: slow, set: {note: "'timeout'"}}]}]
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start()
    activity = run.activity("work")
    run.fire("work")
    assert run.data["note"] == "timeout"
    decisions, _ = run.feed(
        "skill", {"activityId": activity["id"], "status": "succeeded", "output": {"value": "x"}}
    )
    assert decisions[0].out()["reason"] == "stale" and "value" not in run.data


def test_call_an_agent_and_a_nested_process() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: ask-agent
        input: {from: "{'number': data.number}"}
        call:
          agent: helper
          input: {text: "'please'"}
          context: {anchors: [{kind: person, key: data.author}], semantic: false}
        output: {as: {value: string(step.result.answer)}}
      - id: nested
        call: {process: child, input: {amount: data.amount}}
        output: {as: {outcome: step.result.outcome}}
"""
    )
    run.start()
    (task,) = run.intents("create_task")
    assert task["assign"] == [{"agent": "helper"}] and task["taskType"] is None
    assert task["input"] == {"number": "N-1", "text": "please"}
    assert task["context"] == {
        "anchors": [{"kind": "person", "key": AUTHOR}],
        "traverse": [],
        "semantic": False,
    }
    run.complete_task("ask-agent", {"answer": "done"})
    (child,) = run.intents("start_child")
    assert child["process"] == "child" and child["input"] == {"amount": 100}
    assert child["parentInstanceId"] == INSTANCE
    run.feed(
        "child",
        {"activityId": child["activityId"], "status": "completed", "outcome": "ok", "data": {}},
    )
    assert run.data == {**run.data, "value": "done", "outcome": "ok"}
    assert run.status == "completed"


DECIDE = """
decisions:
  - id: level
    hitPolicy: first
    inputs: [{id: amount, expr: data.amount, type: number}]
    outputs: [{id: level, type: number}]
    rules:
      - {when: {amount: "[0..1000)"}, then: {level: 1}}
      - {when: {amount: "[1000..10000)"}, then: {level: 2}}
  - id: levels
    hitPolicy: collect
    inputs: [{id: amount, expr: data.amount, type: number}]
    outputs: [{id: level, type: number}]
    rules:
      - {when: {amount: ">= 0"}, then: {level: 1}}
      - {when: {amount: ">= 50"}, then: {level: 2}}
stages:
  - id: s
    steps:
      - id: guarded
        try:
          do:
            - id: choose
              decide: {table: level}
              output: {as: {level: step.result.level}}
            - id: all-levels
              decide: {table: levels, input: {amount: "data.amount + 1.0"}}
              output: {as: {levels: step.result.items}}
          catch: [{as: err, do: [{id: nomatch, set: {note: err.type}}]}]
"""


def test_decide_applies_a_table_and_records_the_rule() -> None:
    run = Run(DECIDE)
    run.start(amount=100)
    assert run.data["level"] == 1 and run.data["levels"] == [{"level": 1}, {"level": 2}]
    decided = run.decisions("table_decided")
    assert decided[0] == {
        "kind": "table_decided",
        "element": "choose",
        "table": "level",
        "inputs": {"amount": 100},
        "rules": [0],
    }
    assert decided[1]["inputs"] == {"amount": 101} and decided[1]["rules"] == [0, 1]


def test_decide_without_a_matching_rule_is_an_error() -> None:
    run = Run(DECIDE)
    run.start(amount=50000)
    assert run.data["note"] == "decision_no_match"


# --- compensation and cancel ----------------------------------------------------------------


COMPENSABLE = """
stages:
  - id: s
    steps:
      - id: reserve
        set: {note: "'reserved'"}
        onCompensate: [{id: release, remember: {facts: {released: "'reserve'"}}}]
      - id: book
        set: {value: "'booked'"}
        onCompensate: [{id: unbook, remember: {facts: {released: "'book'"}}}]
      - id: review
"""


def test_compensate_all_runs_compensations_in_reverse_order() -> None:
    run = Run(
        COMPENSABLE
        + HUMAN_REVIEW
        + """
onEvent:
  - on: {observation: case.cancelled}
    do: [{id: undo, compensate: all}, {id: closed, complete: {outcome: cancelled-by-customer}}]
"""
    )
    run.start()
    run.observe("case.cancelled", number="N-1")
    remembered = run.intents("remember")
    assert [(r["element"], r["facts"]) for r in remembered] == [
        ("unbook", {"released": "book"}),
        ("release", {"released": "reserve"}),
    ]
    assert run.events("process.compensated")[0]["steps"] == ["book", "reserve"]
    assert run.status == "completed" and run.state is not None
    assert run.state["outcome"] == "cancelled-by-customer"
    assert run.intents("cancel_task")[0]["element"] == "review"


def test_compensate_named_steps_only_once() -> None:
    run = Run(
        COMPENSABLE
        + HUMAN_REVIEW
        + """
onEvent:
  - on: {observation: case.cancelled}
    do: [{id: undo, compensate: [reserve]}, {id: again, compensate: [reserve]}]
"""
    )
    run.start()
    run.observe("case.cancelled", number="N-1")
    assert [r["element"] for r in run.intents("remember")] == ["release"]
    assert [e["steps"] for e in run.events("process.compensated")] == [["reserve"], []]


COMPENSATED_CALL = """
stages:
  - id: s
    steps:
      - id: reserve
        call: {skill: work.do@1, input: {text: data.number}}
        output: {as: {value: step.result.value}}
        onCompensate:
          - id: release
            call: {skill: work.do@1, input: {text: "'release ' + compensated.result.value"}}
            output: {as: {note: step.result.value}}
          - id: trace
            set: {trail: "[compensated.id, step.id]"}
      - id: review
"""


def test_in_on_compensate_step_is_the_blocks_step_and_compensated_the_compensated_one() -> None:
    run = Run(COMPENSATED_CALL + HUMAN_REVIEW)
    run.start()
    run.skill("reserve", {"value": "R-1"})
    assert run.data["value"] == "R-1"
    run.command("cancel", reason="customer")
    release = run.intents("invoke_skill")[-1]
    assert (release["element"], release["input"]) == ("release", {"text": "release R-1"})
    run.skill("release", {"value": "released"})
    assert run.data["note"] == "released", "output.as reads the result of the block's own step"
    assert run.data["trail"] == ["reserve", "release"]
    assert run.status == "cancelled"


def test_compensated_is_typed_by_the_output_of_the_compensated_step() -> None:
    body = COMPENSATED_CALL.replace("compensated.result.value", "compensated.result.missing")
    with pytest.raises(pe.DefinitionError, match="onCompensate/0/call/input"):
        Run(body + HUMAN_REVIEW)
    outside = COMPENSATED_CALL.replace("{text: data.number}", "{text: compensated.id}", 1)
    with pytest.raises(pe.DefinitionError, match="/steps/0/call/input"):
        Run(outside + HUMAN_REVIEW)


def test_a_compensation_frame_of_an_earlier_engine_reads_as_compensated() -> None:
    run = Run(COMPENSATED_CALL + HUMAN_REVIEW)
    run.start()
    run.skill("reserve", {"value": "R-1"})
    run.command("cancel", reason="customer")
    assert run.state is not None
    # The state an earlier engine left: the compensated step as stepVar of the frame.
    for thread in run.state["threads"].values():
        for frame in thread["stack"]:
            if "compensated" in (frame.get("bindings") or {}):
                frame["stepVar"] = frame.pop("bindings")["compensated"]
    run.skill("release", {"value": "released"})
    assert run.data["note"] == "released"
    assert run.data["trail"] == ["reserve", "release"]


def test_a_failing_compensation_puts_the_instance_to_a_person() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: reserve
        set: {note: "'reserved'"}
        onCompensate: [{id: release, raise: {type: release-failed}}]
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start()
    run.command("cancel", reason="customer")
    assert run.status == "failed" and run.state is not None
    assert run.state["attention"]["reason"] == "compensation_failed"
    assert run.state["error"]["type"] == "release-failed"
    assert not run.events("process.cancelled")


def test_cancel_ends_open_work_runs_compensations_then_closes() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: reserve
        set: {note: "'reserved'"}
        onCompensate:
          - id: confirm-release
            human: {taskType: sign-off, assign: [{role: lead}]}
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start()
    review = run.activity("review")
    run.command("cancel", reason="customer withdrew")
    assert run.intents("cancel_task")[0]["activityId"] == review["id"]
    assert run.status == "running" and run.state is not None
    assert run.state["closing"]["kind"] == "cancel"
    run.complete_task("confirm-release", {})
    assert run.status == "cancelled"
    assert run.events("process.cancelled") == [
        {**run.events("process.cancelled")[0], "reason": "customer withdrew", "compensated": True}
    ]
    assert run.intents("complete")[-1]["status"] == "cancelled"


def test_cancel_without_anything_to_compensate_closes_at_once() -> None:
    run = Run(ONE_REVIEW)
    run.start()
    run.command("cancel", reason="stop")
    assert run.status == "cancelled"
    assert run.events("process.cancelled")[0]["compensated"] is False
    decisions, _ = run.command("resume")
    assert decisions[0].out()["reason"] == "instance_closed"


def test_an_operator_may_cancel_without_compensating() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: reserve
        set: {note: "'reserved'"}
        onCompensate:
          - id: confirm-release
            human: {taskType: sign-off, assign: [{role: lead}]}
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start()
    run.command("cancel", reason="stop", compensate=False)
    assert run.status == "cancelled"
    assert run.events("process.cancelled")[0]["compensated"] is False
    assert run.decisions("cancel_requested")[0]["compensate"] is False
    assert [i["element"] for i in run.intents("create_task")] == ["review"]


# --- explicit start and standing goals (TAI-ADR-0055) ----------------------------------------


def test_an_explicit_start_names_the_key_and_the_data_without_an_event() -> None:
    run = Run(ONE_REVIEW)
    run.feed("start", {"instanceId": INSTANCE, "key": "K-7", "data": {"number": "K-7"}})
    assert run.state is not None
    assert (run.state["key"], run.data) == ("K-7", {"number": "K-7"})
    assert run.decisions("data_set") == [], "the given data is the initial data, not a change"
    [started] = run.events("process.started")
    assert (started["triggerEventId"], started["triggerType"]) == (None, "command")
    assert run.decisions("started")[0]["trigger"] == {"eventId": None, "type": "command"}
    assert [i["element"] for i in run.intents("create_task")] == ["review"]


GOAL = """
stages:
  - id: watch
    milestones: [{id: clear, when: "has(data.count) && data.count == 0"}]
    steps:
      - id: hold
        listen:
          any: [{on: {observation: case.never}}]
correlate:
  - on: {observation: case.counted}
    key: event.payload.number
    set: {count: int(event.payload.count)}
"""


def test_a_milestone_is_lost_when_its_guard_stops_holding_and_reached_again() -> None:
    run = Run(GOAL)
    run.start()
    assert run.events("process.milestone_reached") == []
    for count in (0, 0, 2, 0):
        run.observe("case.counted", number="N-1", count=count)
    assert run.event_types().count("process.milestone_reached") == 2
    assert run.event_types().count("process.milestone_lost") == 1
    [lost] = run.events("process.milestone_lost")
    assert (lost["milestone"], lost["stage"]) == ("clear", "watch")
    assert len(run.decisions("milestone_reached")) == 2
    assert len(run.decisions("milestone_lost")) == 1
    assert run.state is not None and run.state["milestones"] == {"clear": True}
    assert run.status == "running"


def test_a_guard_reading_its_own_milestone_changes_it_once_per_input() -> None:
    run = Run(
        """
stages:
  - id: watch
    milestones: [{id: flip, when: "!milestone.?flip.orValue(false)"}]
    steps:
      - id: hold
        listen:
          any: [{on: {observation: case.never}}]
"""
    )
    run.start()
    assert run.event_types().count("process.milestone_reached") == 1
    run.observe("case.other", number="N-1")
    assert run.event_types().count("process.milestone_lost") == 1
    assert run.status == "running"


# --- memory: recall and remember ------------------------------------------------------------


RECALL = """
memory:
  case: {key: "'case:' + data.number"}
stages:
  - id: s
    steps:
      - id: history
        recall:
          anchors: [{case: true}, {kind: person, key: data.author}]
          traverse: [{relation: customer, direction: in, depth: 1}]
          kinds: [case, lesson]
          query: "'history of ' + data.number"
          limit: 20
          onTimeout: [{id: nothing, set: {note: "'no memory'"}}]
        output: {as: {history: step.result.nodes}}
"""


def test_recall_asks_memory_through_an_intent_and_takes_the_answer_as_input() -> None:
    run = Run(RECALL)
    run.start()
    (asked,) = run.intents("recall")
    activity = run.activity("history")
    assert asked == {
        "kind": "recall",
        "recallId": activity["id"],
        "activityId": activity["id"],
        "element": "history",
        "anchors": [
            {"case": True, "kind": "case", "key": "case:N-1"},
            {"kind": "person", "key": AUTHOR},
        ],
        "traverse": [{"relation": "customer", "direction": "in", "depth": 1}],
        "kinds": ["case", "lesson"],
        "query": "history of N-1",
        "limit": 20,
        "timeout": "PT600S",
        "asOf": "2026-03-02T09:00:00Z",
    }
    assert run.timer("history")["dueAt"] == "2026-03-02T09:10:00Z"
    answer = {"nodes": [{"kind": "case", "key": "case:N-0"}], "edges": [], "truncated": False}
    run.feed("recall", {"activityId": activity["id"], "status": "completed", "result": answer})
    assert run.data["history"] == [{"kind": "case", "key": "case:N-0"}]
    completed = run.events("process.recall_completed")[0]
    assert (completed["nodeCount"], completed["edgeCount"]) == (1, 0)
    assert completed["resultHash"].startswith("sha256:")
    assert run.status == "completed"


RECALL_WHERE = """
memory:
  case: {key: "'case:' + data.number"}
stages:
  - id: s
    steps:
      - id: code
        set: {value: "'62.01'"}
      - id: offers
        recall:
          anchors: [{kind: company, key: data.author}]
          query: "'software for ' + data.number"
          where:
            - {attr: okpd2, op: prefix, value: data.value}
            - {attr: validUntil, op: gte, value: data.deadline}
            - {attr: status, op: in, value: ["'active'", "'draft'"]}
            - {attr: price, op: lte, value: data.amount * 2.0}
            - {attr: blocked, op: exists, value: false}
            - {attr: okpd2, op: exists}
        output: {as: {history: step.result.nodes}}
"""


def test_recall_where_is_computed_from_the_data_into_the_intent() -> None:
    # CP-ADR-0076, amendment 2026-09-28: CEL values become JSON literals.
    run = Run(RECALL_WHERE)
    run.start()
    (asked,) = run.intents("recall")
    assert asked["where"] == [
        {"attr": "okpd2", "op": "prefix", "value": "62.01"},
        {"attr": "validUntil", "op": "gte", "value": "2026-05-04T09:00:00Z"},
        {"attr": "status", "op": "in", "value": ["active", "draft"]},
        {"attr": "price", "op": "lte", "value": 200},
        {"attr": "blocked", "op": "exists", "value": False},
        {"attr": "okpd2", "op": "exists"},
    ]
    assert list(asked).index("where") == list(asked).index("query") + 1
    # The intent is recorded whole: another deadline is another where.
    later = Run(RECALL_WHERE)
    later.start(deadline="2026-06-01T00:00:00Z")
    (other,) = later.intents("recall")
    assert other["where"][1]["value"] == "2026-06-01T00:00:00Z"
    assert (asked["anchors"], asked["query"]) == (other["anchors"], other["query"])


def test_recall_without_where_asks_as_before() -> None:
    run = Run(RECALL)
    run.start()
    (asked,) = run.intents("recall")
    assert "where" not in asked


def test_an_error_in_a_where_value_fails_the_step_like_an_anchor() -> None:
    run = Run(RECALL_WHERE.replace("value: data.value}", "value: string(int(data.number))}"))
    run.start()
    assert run.intents("recall") == []
    assert run.status == "failed"


def test_recall_without_an_answer_in_time_goes_the_timeout_way() -> None:
    run = Run(RECALL)
    run.start()
    activity = run.activity("history")
    run.fire("history")
    assert run.data["note"] == "no memory" and "history" not in run.data
    assert run.events("process.recall_timed_out")[0]["reason"] == "timeout"
    late = {"nodes": [{"kind": "case"}], "edges": [], "truncated": False}
    decisions, _ = run.feed(
        "recall", {"activityId": activity["id"], "status": "completed", "result": late}
    )
    assert decisions[0].out()["reason"] in ("stale", "instance_closed")


def test_memory_reports_no_answer_as_a_timed_out_recall() -> None:
    run = Run(RECALL)
    run.start()
    activity = run.activity("history")
    run.feed(
        "recall",
        {"activityId": activity["id"], "status": "timed_out", "reason": "memory_unavailable"},
    )
    assert run.data["note"] == "no memory"
    assert run.events("process.recall_timed_out")[0]["reason"] == "memory_unavailable"


def test_remember_is_an_intent_with_the_case_and_a_dedup_key() -> None:
    run = Run(
        """
memory:
  case: {key: "'case:' + data.number"}
stages:
  - id: s
    steps:
      - id: price
        remember: {facts: {amount: data.amount, "total.net": "data.amount * 2.0"}}
      - id: who
        remember:
          entity:
            kind: person
            key: data.author
            name: "'Author'"
            links: [{rel: authored, kind: case, key: "'case:' + data.number"}]
"""
    )
    run.start()
    facts, entity = run.intents("remember")
    assert facts == {
        "kind": "remember",
        "element": "price",
        "source": "process:test",
        "dedupKey": facts["dedupKey"],
        "case": "case:N-1",
        "facts": {"amount": 100, "total": {"net": 200}},
    }
    assert facts["dedupKey"].startswith(f"process:{INSTANCE}:price:0:")
    assert entity["entity"] == {
        "kind": "person",
        "key": AUTHOR,
        "name": "Author",
        "links": [{"rel": "authored", "kind": "case", "key": "case:N-1"}],
    }
    assert facts["dedupKey"] != entity["dedupKey"]


# --- approvals ------------------------------------------------------------------------------


def _approve(quorum: str, extra: str = "") -> str:
    return f"""
stages:
  - id: s
    steps:
      - id: sign
        approve:
          approvers: [{{role: finance}}]
          quorum: {quorum}
          separationOfDuties: "[data.author]"
          {extra}
        output: {{as: {{decision: step.result.outcome}}}}
"""


def test_approve_requests_approvals_without_the_excluded_principals() -> None:
    run = Run(_approve("{atLeast: 2}"))
    run.start()
    (asked,) = run.intents("request_approvals")
    assert asked["approvers"] == [{"role": "finance"}]
    assert asked["excludedPrincipals"] == [AUTHOR]
    assert (asked["mode"], asked["quorum"], asked["earlyDecision"]) == (
        "parallel",
        {"atLeast": 2},
        True,
    )


def test_two_of_three_approve_early() -> None:
    run = Run(_approve("{atLeast: 2}"))
    run.start()
    run.vote("sign", "a", "approved", 3)
    assert "decision" not in run.data
    run.vote("sign", "b", "approved", 3)
    assert run.data["decision"] == "approved"
    closed = run.intents("close_approvals")[0]
    assert (closed["outcome"], closed["reason"]) == ("approved", "quorum")


def test_two_of_three_reject_early() -> None:
    run = Run(_approve("{atLeast: 2}"))
    run.start()
    run.vote("sign", "a", "rejected", 3)
    run.vote("sign", "b", "rejected", 3)
    assert run.data["decision"] == "rejected"


def test_an_approver_leaving_recounts_the_quorum() -> None:
    run = Run(_approve("all"))
    run.start()
    run.vote("sign", "a", "approved", 3)
    run.vote("sign", "c", "cancelled", 2)
    assert "decision" not in run.data
    run.vote("sign", "b", "approved", 2)
    assert run.data["decision"] == "approved"


def test_approval_due_decides_by_on_due() -> None:
    run = Run(_approve("any", "due: P1D\n          onDue: reject"))
    run.start()
    run.fire("sign", "due")
    assert run.data["decision"] == "rejected"
    assert run.intents("close_approvals")[0]["reason"] == "due"


# --- retrospective --------------------------------------------------------------------------


RETROSPECTIVE = """
memory:
  case: {key: "'case:' + data.number", title: "'Case ' + data.number"}
  entities:
    - {kind: person, key: data.author, name: "'Author'", rel: author}
    - {kind: team, key: "'team-1'", rel: member}
retrospective:
  taskType: lessons
  assign: [{role: lead}]
  appliesTo: [person]
stages:
  - id: s
    steps: [{id: finish, complete: {outcome: won}}]
"""


def _journal(run: Run) -> list[dict[str, Any]]:
    """The journal of the run as the core keeps it: one record per input, ``seq`` from 1."""
    return [
        entry
        for seq, (given, decisions, intents) in enumerate(run.records, start=1)
        for entry in process_replay.journal_entries(
            seq=seq,
            at=given.at,
            kind=given.kind,
            source_ref=f"input-{seq}",
            actor_id=given.actor_id,
            event_id=(given.body.get("event") or {}).get("id"),
            given=given.out(),
            calendars={},
            decisions=[d.out() for d in decisions],
            intents=[i.out() for i in intents],
        )
    ]


def test_a_retrospective_asks_the_skill_by_its_contract() -> None:
    contract = retrospective_contract()
    run = Run(RETROSPECTIVE)
    run.start()
    assert run.status == "completed"
    (proposal,) = run.intents("invoke_skill")
    assert proposal["skill"] == "process.retrospective@1" and proposal["attach"] == ["journal"]
    given = process_replay.with_attachments(proposal, lambda: _journal(run))
    errors = [e.message for e in Draft202012Validator(contract["inputs"]).iter_errors(given)]
    assert not errors, errors
    assert given["case"] == {"kind": "case", "key": "case:N-1", "title": "Case N-1"}
    assert given["entities"] == [
        {"kind": "person", "key": AUTHOR, "name": "Author", "rel": "author"},
        {"kind": "team", "key": "team-1", "name": None, "rel": "member"},
    ]
    assert given["appliesToKinds"] == ["person"]
    assert (given["definitionKey"], given["version"], given["outcome"]) == ("test", 1, "won")
    # The step that closed the case is in the journal the skill reads.
    assert any(entry["reason"] == "completed" for entry in given["journal"])
    assert "journal" not in proposal["input"], "the journal is attached, not recorded twice"


def test_a_retrospective_without_a_case_is_skipped() -> None:
    run = Run(
        """
retrospective: {taskType: lessons, assign: [{role: lead}]}
stages:
  - id: s
    steps: [{id: finish, complete: {outcome: won}}]
"""
    )
    run.start()
    assert run.intents("invoke_skill") == []
    assert run.decisions("retrospective_skipped")[0]["error"] == {"type": "case_unknown"}


def test_a_closed_case_gets_a_retrospective_and_only_confirmed_lessons_are_remembered() -> None:
    contract = retrospective_contract()
    run = Run(RETROSPECTIVE)
    run.start()
    (proposal,) = run.intents("invoke_skill")
    evidence = [{"seq": 1, "eventId": None}]
    output = {
        "case": "case:N-1",
        "lessons": [
            {
                "key": "lesson:case:N-1/a",
                "text": "keep",
                "appliesTo": [{"kind": "person", "key": AUTHOR}],
                "evidence": evidence,
            },
            {
                "key": "lesson:case:N-1/b",
                "text": "fix",
                "appliesTo": [{"kind": "case", "key": "case:N-1"}],
                "evidence": evidence,
            },
            {
                "key": "lesson:case:N-1/c",
                "text": "drop",
                "appliesTo": [{"kind": "person", "key": AUTHOR}],
                "evidence": evidence,
            },
        ],
        "dropped": [],
    }
    assert Draft202012Validator(contract["outputs"]).is_valid(output)
    run.feed(
        "skill", {"activityId": proposal["activityId"], "status": "succeeded", "output": output}
    )
    (review,) = run.intents("create_task")
    # The person sees the entities as {kind, key}: the form answers in that shape.
    assert review["taskType"] == "lessons"
    assert [lesson["appliesTo"] for lesson in review["input"]["lessons"]] == [
        [{"kind": "person", "key": AUTHOR}],
        [{"kind": "case", "key": "case:N-1"}],
        [{"kind": "person", "key": AUTHOR}],
    ]
    keep, fix, drop = review["input"]["lessons"]
    answered = [
        {**keep, "decision": "confirm"},
        # The form of an earlier package answers with bare keys: nodes of the case.
        {**fix, "appliesTo": [AUTHOR, "unknown"], "decision": "edit", "editedText": "fixed"},
        {**drop, "decision": "reject"},
    ]
    run.feed(
        "task",
        {
            "activityId": review["activityId"],
            "status": "completed",
            "task": {"customFields": {"lessons": answered}},
        },
    )
    learned = {"rel": "learned_from", "kind": "case", "key": "case:N-1"}
    applies = {"rel": "applies_to", "kind": "person", "key": AUTHOR}
    assert [(r["entity"], r["evidence"]) for r in run.intents("remember")] == [
        (
            {
                "kind": "lesson",
                "key": "lesson:case:N-1/a",
                "text": "keep",
                "links": [learned, applies],
            },
            evidence,
        ),
        (
            {
                "kind": "lesson",
                "key": "lesson:case:N-1/b",
                "text": "fixed",
                "links": [learned, applies],
            },
            evidence,
        ),
    ]
    assert run.decisions("retrospective_done")[0]["confirmed"] == 2
    assert run.status == "completed"


# --- limits and failures of expressions ------------------------------------------------------


def test_an_expression_over_the_cost_limit_fails_the_step_with_a_clear_reason() -> None:
    body = """
stages:
  - id: s
    steps:
      - {id: fill, set: {trail: "event.payload.items.map(x, string(x))"}}
      - id: count
        set: {count: "size(data.trail.filter(x, x.contains('a')))"}
"""
    cheap = Run(body, cost_limit=1_000)
    cheap.start(items=["a", "b"])
    assert cheap.status == "completed" and cheap.data["count"] == 1

    heavy = Run(body, cost_limit=1_000)
    heavy.start(items=["a"] * 400)
    assert heavy.status == "failed" and heavy.state is not None
    assert heavy.state["error"]["type"] == "expression_cost_exceeded"
    assert "more than the limit 1000" in heavy.state["error"]["detail"]
    assert heavy.events("process.failed")[0]["element"] in ("fill", "count")


def test_a_guard_that_cannot_be_evaluated_fails_the_instance() -> None:
    run = Run(
        """
stages:
  - id: s
    entry: "int(data.number) > 0"
    steps: [{id: a, set: {note: "'x'"}}]
"""
    )
    run.start(number="not-a-number")
    assert run.status == "failed" and run.state is not None
    assert run.state["error"]["type"] == "expression_error"
    assert run.state["error"]["element"] == "s"


def test_a_refused_intent_is_an_error_of_its_step() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: guarded
        try:
          do:
            - """
        + REVIEW
        + """
          catch:
            - {errors: {type: intent_failed}, as: err, do: [{id: why, set: {note: err.detail}}]}
"""
    )
    run.start()
    activity = run.activity("review")
    run.feed(
        "intent_failed",
        {"activityId": activity["id"], "intent": "create_task", "code": "credential_inactive"},
    )
    assert run.data["note"] == "credential_inactive"


def test_answers_to_nothing_open_are_stale() -> None:
    run = Run(ONE_REVIEW)
    run.start()
    decisions, intents = run.feed(
        "task", {"activityId": str(uuid.uuid4()), "status": "completed", "task": {}}
    )
    assert decisions[0].out()["reason"] == "stale" and intents == []


def test_a_definition_that_fails_the_check_does_not_run() -> None:
    broken = spec("stages: [{id: s, steps: [{id: a, set: {missing: \"'x'\"}}]}]")
    with pytest.raises(pe.DefinitionError) as caught:
        Definition.build("test", pd.normalized_spec(broken), CATALOG)
    assert caught.value.problems[0].code == "unknown_data_field"


def test_inputs_are_checked() -> None:
    run = Run("stages: [{id: s, steps: [{id: a, set: {note: \"'x'\"}}]}]")
    with pytest.raises(pe.EngineError):
        pe.step(run.definition, None, Input("event", T0, {}))
    with pytest.raises(pe.EngineError):
        pe.step(run.definition, None, Input("start", datetime(2026, 1, 1), {}))
    with pytest.raises(pe.EngineError):
        pe.step(run.definition, None, Input("nonsense", T0, {}))


# --- determinism ----------------------------------------------------------------------------


SCENARIO = """
memory:
  case: {key: "'case:' + data.number", title: data.number}
  facts: {decision: data.decision}
decisions:
  - id: level
    hitPolicy: first
    inputs: [{id: amount, expr: data.amount, type: number}]
    outputs: [{id: level, type: number}]
    rules:
      - {when: {amount: "[0..1000)"}, then: {level: 1}}
      - {when: {amount: "-"}, then: {level: 2}}
onEvent:
  - on: {observation: case.suspended}
    do: [{id: pause, suspend: {reason: "'hold'"}}]
  - on: {observation: case.resumed}
    do: [{id: unpause, resume: {}}]
stages:
  - id: screen
    timers:
      - id: remind
        at: {at: "cal.addWorkdays(data.deadline, -5)"}
        do: [{id: nudge, set: {note: "'nudged'"}}]
    exit: has(data.decision)
    steps:
      - id: history
        recall: {anchors: [{case: true}], timeout: PT2M}
        output: {as: {history: step.result.nodes}}
      - id: choose
        decide: {table: level}
        output: {as: {level: step.result.level}}
      - id: review
        human:
          taskType: review
          assign: [{role: lead}]
          due: {at: "cal.addWorkdays(data.deadline, -3)"}
          escalations: [{after: due, action: notify, to: [{role: boss}]}]
        output: {as: {decision: step.result.decision}}
  - id: sign
    entry: "stage.screen.completed && data.decision == 'go'"
    steps:
      - id: approval
        approve:
          approvers: [{role: finance}]
          quorum: {atLeast: 2}
          separationOfDuties: "[data.author]"
        output: {as: {outcome: step.result.outcome}}
      - id: wait-result
        listen:
          any:
            - on: {observation: case.result}
              do: [{id: took, set: {value: event.payload.result}}]
          timeout: P30D
      - {id: finish, complete: {outcome: finished}}
"""


def _scenario(run: Run) -> None:
    run.start(amount=500)
    history = run.activity("history")
    run.feed(
        "recall",
        {
            "activityId": history["id"],
            "status": "completed",
            "result": {"nodes": [{"key": "case:old"}], "edges": [], "truncated": False},
        },
        at=T0 + timedelta(minutes=1),
    )
    run.clock = T0 + timedelta(days=2)
    run.observe("case.changed", number="N-1", deadline="2026-05-11T09:00:00Z")
    run.observe("case.suspended", number="N-1")
    run.clock = T0 + timedelta(days=4)
    run.complete_task("review", {"decision": "go"})
    run.observe("case.resumed", number="N-1")
    run.vote("approval", "p1", "approved", 3)
    run.vote("approval", "p2", "approved", 3)
    run.clock = T0 + timedelta(days=10)
    run.observe("case.result", number="N-1", result="won")


def test_the_same_journal_gives_the_same_decisions_intents_and_state() -> None:
    first, second = Run(SCENARIO), Run(SCENARIO)
    _scenario(first)
    _scenario(second)
    assert first.status == "completed" and first.state is not None
    assert first.state["outcome"] == "finished" and first.data["value"] == "won"

    def journal(run: Run) -> list[Any]:
        return [
            ([d.out() for d in decisions], [i.out() for i in intents])
            for _, decisions, intents in run.records
        ]

    assert journal(first) == journal(second)
    assert first.state == second.state

    # A replay feeds the recorded inputs, whole, to a fresh engine: nothing differs.
    replayed: dict[str, Any] | None = None
    for recorded, decisions, intents in first.records:
        replayed, again, made = pe.step(first.definition, replayed, recorded)
        replayed = json.loads(json.dumps(replayed, sort_keys=True))
        assert [d.out() for d in again] == [d.out() for d in decisions]
        assert [i.out() for i in made] == [i.out() for i in intents]
    assert replayed == first.state


def test_ids_the_engine_makes_are_uuid5_of_instance_seq_and_index() -> None:
    run = Run(RECALL)
    run.start()
    assert run.activity("history")["id"] == pe.new_id(INSTANCE, 0, 0)
    assert str(uuid.UUID(pe.new_id(INSTANCE, 3, 1))) == pe.new_id(INSTANCE, 3, 1)


# --- the example of the catalog schema ----------------------------------------------------


def _example() -> Run:
    """The superproject's example process (every block) with the data it writes declared."""
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
    run = Run("stages: [{id: s, steps: [{id: a, set: {note: \"'x'\"}}]}]")
    run.definition = Definition.build(PROCESS["key"], pd.normalized_spec(example), catalog)
    notice = {
        "number": "0373100000126000001",
        "subject": "Data processing services",
        "nmck": 5_000_000,
        "submissionDeadline": "2026-05-12T09:00:00Z",
        "customer": {"inn": "7700000000", "name": "Customer"},
    }
    run.feed(
        "start",
        {"instanceId": INSTANCE, "event": run.event_doc("purchase.notice_published", notice)},
    )
    history = run.activity("recall-history")
    run.feed(
        "recall",
        {
            "activityId": history["id"],
            "status": "completed",
            "result": {"nodes": [{"kind": "case", "key": "purchase:old"}], "edges": []},
        },
    )
    decide = run.activity("decide-participation")
    task = {
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "go")),
        "assigneeId": AUTHOR,
        "customFields": {"decision": "go"},
    }
    run.feed("task", {"activityId": decide["id"], "status": "completed", "task": task})
    return run


def test_the_schema_example_runs_from_the_notice_to_the_close() -> None:
    run = _example()
    assert run.data["history"] == [{"kind": "case", "key": "purchase:old"}]
    assert run.data["decision"] == "go" and run.data["approversNeeded"] == 1
    (approvals,) = run.intents("request_approvals")
    assert approvals["excludedPrincipals"] == [AUTHOR]  # who decided may not approve the price
    reminder = run.timer("deadline-reminder")
    assert reminder["dueAt"] == "2026-05-07T09:00:00Z"  # three working days before Tue 12 May
    run.vote("approve-price", OTHER, "approved", 1)
    assert run.state is not None and run.state["stages"]["results"]["state"] == "active"
    run.feed(
        "event",
        {
            "event": run.event_doc(
                "purchase.protocol",
                {
                    "number": "0373100000126000001",
                    "kind": "results",
                    "result": "won",
                    "winner": {"inn": "7711111111", "name": "Us"},
                },
            )
        },
    )
    assert run.data["outcome"] == "won" and run.status == "completed"
    assert run.state["outcome"] == "finished"
    winner = run.intents("remember")[-1]
    assert winner["entity"]["key"] == "7711111111"
    assert winner["entity"]["links"] == [
        {"rel": "won", "kind": "case", "key": "purchase:0373100000126000001"}
    ]
    assert run.intents("invoke_skill")[-1]["skill"] == "process.retrospective@1"
    completed = run.events("process.completed")[0]
    assert completed["memory"]["case"]["key"] == "purchase:0373100000126000001"
    assert completed["memory"]["entities"] == [
        {"kind": "legal_entity", "key": "7700000000", "name": "Customer", "rel": "customer"}
    ]


def test_the_schema_example_cancelled_by_the_customer_compensates() -> None:
    run = _example()
    run.vote("approve-price", OTHER, "approved", 1)
    run.feed(
        "event",
        {"event": run.event_doc("purchase.cancelled", {"number": "0373100000126000001"})},
    )
    assert run.state is not None and run.state["outcome"] == "cancelled-by-customer"
    assert run.intents("remember")[-1]["facts"] == {"priceApprovalRevoked": True}
    assert run.events("process.compensated")[0]["steps"] == ["approve-price"]
