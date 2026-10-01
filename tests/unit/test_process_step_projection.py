"""Step events as a projection of engine steps (CP-ADR-0074 §13, amendment 2026-09-29).

:func:`process_steps.step_events` compares the activities before and after
each step: a waiting step opened is ``process.step_entered``, one closed is
``process.step_exited`` with the outcome the step's decisions give. Instant
steps give nothing; the engine's decisions and intents are not touched.
"""

import copy
import uuid
from datetime import timedelta
from typing import Any

from control_plane.domain import process_steps
from control_plane.domain.process_migration import MIGRATION_INPUT
from tests.unit.test_process_engine import (
    HUMAN_REVIEW,
    INSTANCE,
    LISTEN,
    RETRY,
    Run,
    _approve,
    _check_event,
)


class Projected(Run):
    """A run that projects the step events of every input, as ``take()`` does."""

    def __init__(self, body: str) -> None:
        super().__init__(body)
        self.attempts: dict[str, int] = {}
        self.refs: dict[str, Any] = {}
        self.step_events: list[process_steps.StepEvent] = []
        self.per_input: list[list[process_steps.StepEvent]] = []

    def feed(self, kind: str, body: dict[str, Any] | None = None, **kw: Any) -> Any:
        before = copy.deepcopy(self.state)
        decisions, intents = super().feed(kind, body, **kw)
        assert self.state is not None
        for intent in intents:
            # What the executor would write into refs for the opened work.
            prefix = {"create_task": "task", "invoke_skill": "skill", "start_child": "child"}
            if intent.kind in prefix:
                made = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{intent.kind}-{len(self.refs)}"))
                self.refs[f"{prefix[intent.kind]}:{made}"] = {
                    "activity": intent.body["activityId"],
                    "element": intent.body.get("element"),
                }
        projection = process_steps.step_events(
            self.definition,
            instance_id=INSTANCE,
            before=before,
            after=self.state,
            decisions=[d.out() for d in decisions],
            given=self.records[-1][0].out(),
            at=self.clock,
            attempts=self.attempts,
            refs=self.refs,
        )
        self.attempts, self.refs = projection.attempts, projection.refs
        events = projection.events
        for event in events:
            _check_event(event.type, {**event.payload, "workspaceId": None})
        self.step_events += events
        self.per_input.append(events)
        return decisions, intents

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [e.payload for e in self.step_events if e.type == event_type]

    def pairs(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for event in self.step_events:
            out.setdefault(event.activity_id, []).append(event.type)
        return out


ENTERED = process_steps.STEP_ENTERED
EXITED = process_steps.STEP_EXITED
REVIEW_THEN_SET = (
    """
stages:
  - id: s
    steps:
      - {id: first, set: {note: "'a'"}}
      - id: review
"""
    + HUMAN_REVIEW
    + """
      - {id: last, set: {note: "'b'"}}
"""
)


def test_a_human_step_enters_with_its_task_and_exits_completed() -> None:
    run = Projected(REVIEW_THEN_SET)
    run.start()
    [entered] = run.of(ENTERED)
    activity = run.activity("review")
    [task_ref] = [r for r in run.refs if r.startswith("task:")]
    assert entered == {
        "instanceId": INSTANCE,
        "definitionKey": "test",
        "version": 1,
        "instanceKey": "N-1",
        "element": "review",
        "stage": "s",
        "stepKind": "human",
        "waitsFor": "task",
        "attempt": 1,
        "activityId": activity["id"],
        "enteredAt": "2026-03-02T09:00:00Z",
        "taskId": task_ref.split(":", 1)[1],
        "approvalIds": [],
        "skillInvocationId": None,
        "childInstanceId": None,
        "due": None,
        "warnAt": None,
        "provisional": False,
    }
    run.complete_task("review", {"decision": "yes"}, at=run.clock + timedelta(hours=2))
    [exited] = run.of(EXITED)
    assert {k: exited[k] for k in ("activityId", "attempt", "outcome", "durationSeconds")} == {
        "activityId": activity["id"],
        "attempt": 1,
        "outcome": "completed",
        "durationSeconds": 7200,
    }
    assert (exited["exitedAt"], exited["due"], exited["breached"], exited["overdueSeconds"]) == (
        "2026-03-02T11:00:00Z",
        None,
        False,
        None,
    )
    assert run.status == "completed"
    assert run.attempts == {"review": 1}


def test_instant_steps_give_no_step_events() -> None:
    run = Projected(
        """
stages:
  - id: s
    steps:
      - {id: a, set: {note: "'a'"}}
      - {id: b, set: {level: "1.0"}}
      - {id: c, complete: {outcome: done}}
"""
    )
    run.start()
    assert run.status == "completed"
    assert run.step_events == []
    assert run.attempts == {}


def test_every_waiting_step_is_one_pair_and_at_most_two_events_per_step() -> None:
    run = Projected(REVIEW_THEN_SET)
    run.start()
    run.complete_task("review", {"decision": "yes"})
    assert list(run.pairs().values()) == [[ENTERED, EXITED]]
    assert all(len(events) <= 2 for events in run.per_input)


def test_a_timeout_exits_timed_out() -> None:
    run = Projected(LISTEN)
    run.start()
    [entered] = run.of(ENTERED)
    assert (entered["stepKind"], entered["waitsFor"]) == ("listen", "event")
    run.fire("wait")
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "timed_out"
    assert exited["durationSeconds"] == int(timedelta(days=5).total_seconds())


def test_the_answering_event_exits_completed() -> None:
    run = Projected(LISTEN)
    run.start()
    run.observe("case.answered", number="N-1", ok=True, text="yes")
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "completed"


def test_a_cancel_command_exits_cancelled() -> None:
    run = Projected(REVIEW_THEN_SET)
    run.start()
    run.command("cancel", reason="withdrawn", compensate=False)
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "cancelled"


def test_an_interrupting_timer_exits_interrupted() -> None:
    run = Projected(
        """
stages:
  - id: s
    timers:
      - id: stop
        at: P1D
        interrupting: true
        do: [{id: stopped, complete: {outcome: late}}]
    steps:
      - id: review
"""
        + HUMAN_REVIEW
    )
    run.start()
    run.fire("stop")
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "interrupted"
    assert exited["element"] == "review"


def test_an_error_without_handler_exits_failed() -> None:
    run = Projected(
        """
stages:
  - id: s
    steps:
      - id: work
        call: {skill: work.do@1, input: {text: data.number}}
"""
    )
    run.start()
    [entered] = run.of(ENTERED)
    assert (entered["stepKind"], entered["waitsFor"]) == ("call", "skill")
    assert entered["skillInvocationId"] is not None
    run.skill("work", error={"code": "boom", "message": "no"})
    assert run.status == "failed"
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "failed"


def test_a_refused_opening_intent_exits_failed() -> None:
    run = Projected(REVIEW_THEN_SET)
    run.start()
    activity = run.activity("review")
    run.feed(
        "intent_failed",
        {"activityId": activity["id"], "intent": "create_task", "code": "x", "status": 422},
    )
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "failed"


def test_a_retried_step_counts_its_attempts_and_the_retry_pause_is_no_step() -> None:
    run = Projected(RETRY)
    run.start()
    run.skill("work", error={"code": "boom", "message": "no"})
    run.fire("attempt", "retry")
    run.skill("work", output={"value": "ok"})
    assert run.status == "completed"
    events = [(e.type, e.payload["element"], e.payload["attempt"]) for e in run.step_events]
    assert events == [
        (ENTERED, "work", 1),
        (EXITED, "work", 1),
        (ENTERED, "work", 2),
        (EXITED, "work", 2),
    ]
    assert run.attempts == {"work": 2}


PARALLEL_CHECKS = (
    """
onEvent:
  - on: {observation: case.note}
    do:
      - id: check
"""
    + HUMAN_REVIEW
    + """
stages:
  - id: s
    steps:
      - id: review
"""
    + HUMAN_REVIEW
)


def test_parallel_activities_of_one_element_exit_with_the_attempt_they_entered_with() -> None:
    run = Projected(PARALLEL_CHECKS)
    run.start()
    run.observe("case.note", number="N-1")
    run.observe("case.note", number="N-1")
    assert run.state is not None
    first, second = sorted(
        (a for a in run.state["activities"].values() if a["element"] == "check"),
        key=lambda a: a["n"],
    )
    assert run.attempts == {"review": 1, "check": 2}
    assert run.refs[f"activity:{first['id']}"] == {"attempt": 1}
    assert run.refs[f"activity:{second['id']}"] == {"attempt": 2}
    for activity in (first, second):
        run.feed(
            "task",
            {
                "activityId": activity["id"],
                "status": "completed",
                "task": {"id": str(uuid.uuid4()), "status": "done", "customFields": {}},
            },
        )
    checks = [
        (e.type, e.activity_id, e.payload["attempt"])
        for e in run.step_events
        if e.payload["element"] == "check"
    ]
    assert checks == [
        (ENTERED, first["id"], 1),
        (ENTERED, second["id"], 2),
        (EXITED, first["id"], 1),
        (EXITED, second["id"], 2),
    ]
    # The attempt of a closed activity is not kept: refs stay bounded.
    assert f"activity:{first['id']}" not in run.refs
    assert f"activity:{second['id']}" not in run.refs
    assert run.attempts["check"] == 2


def test_an_activity_opened_before_step_events_exits_with_attempt_one() -> None:
    run = Projected(REVIEW_THEN_SET)
    run.start()
    activity = run.activity("review")
    # What an instance started before the rollout looks like: no counter, no attempt kept.
    run.attempts, run.refs = {}, {r: t for r, t in run.refs.items() if r.startswith("task:")}
    run.complete_task("review", {"decision": "yes"})
    [exited] = run.of(EXITED)
    assert (exited["activityId"], exited["attempt"]) == (activity["id"], 1)


def test_the_attempt_kept_joins_the_activity_record_of_an_approval() -> None:
    before = {"activities": {}}
    refs = {"activity:a-1": {"element": "sign", "pending": [], "total": 2, "cancelled": 0}}
    run = Projected(REVIEW_THEN_SET)
    after = {
        "status": "running",
        "activities": {
            "a-1": {
                "id": "a-1",
                "n": 1,
                "kind": "approval",
                "element": "sign",
                "openedAt": "2026-03-02T09:00:00Z",
            }
        },
    }
    projection = process_steps.step_events(
        run.definition,
        instance_id=INSTANCE,
        before=before,
        after=after,
        decisions=[],
        given={"kind": "start", "body": {}},
        at=run.clock,
        attempts={},
        refs=refs,
    )
    assert projection.refs["activity:a-1"] == {**refs["activity:a-1"], "attempt": 1}
    closed = process_steps.step_events(
        run.definition,
        instance_id=INSTANCE,
        before=after,
        after={"status": "running", "activities": {}},
        decisions=[],
        given={"kind": "approval", "body": {"activityId": "a-1", "outcome": "approved"}},
        at=run.clock,
        attempts=projection.attempts,
        refs=projection.refs,
    )
    assert closed.events[0].payload["attempt"] == 1
    assert closed.refs == refs, "the approval's own record stays, without the attempt"


def test_an_escalation_raise_caught_by_try_exits_cancelled() -> None:
    run = Projected(
        """
stages:
  - id: s
    steps:
      - id: guarded
        try:
          do:
            - id: review
              human:
                taskType: review
                assign: [{role: lead}]
                due: P1D
                escalations: [{after: due, action: raise, error: {type: overdue}}]
          catch: [{errors: {type: overdue}, do: [{id: late, set: {note: "'overdue'"}}]}]
"""
    )
    run.start()
    run.fire("review")
    [exited] = run.of(EXITED)
    assert (exited["element"], exited["outcome"]) == ("review", "cancelled")
    assert run.status == "completed"


GUARDED_REVIEW = (
    """
stages:
  - id: s
    steps:
      - id: guarded
        try:
          do:
            - id: review
"""
    + HUMAN_REVIEW.replace("\n        ", "\n              ")
    + """
          catch: [{errors: {type: task_cancelled}, do: [{id: gone, set: {note: "'gone'"}}]}]
"""
)


def test_a_task_cancelled_outside_the_process_exits_withdrawn() -> None:
    """A participant cancelled the step's task: not the step's result, not the process."""
    run = Projected(GUARDED_REVIEW)
    run.start()
    run.complete_task("review", {}, status="cancelled")
    [exited] = run.of(EXITED)
    assert (exited["element"], exited["outcome"]) == ("review", "withdrawn")
    assert run.status == "completed"


def test_a_task_cancelled_without_a_handler_still_exits_withdrawn() -> None:
    """The instance fails because of it, but the step itself was withdrawn."""
    run = Projected(REVIEW_THEN_SET)
    run.start()
    run.complete_task("review", {}, status="cancelled")
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "withdrawn"
    assert run.status == "failed"


def test_a_task_done_exits_completed() -> None:
    """``terminal_success`` is the step's result."""
    run = Projected(GUARDED_REVIEW)
    run.start()
    run.complete_task("review", {"decision": "yes"})
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "completed"


def test_a_cancelled_approval_exits_withdrawn() -> None:
    run = Projected(_approve("any"))
    run.start()
    run.vote("sign", "p1", "cancelled", total=0)
    [exited] = run.of(EXITED)
    assert (exited["element"], exited["stepKind"], exited["outcome"]) == (
        "sign",
        "approve",
        "withdrawn",
    )


def test_a_quorum_all_approved_after_a_cancelled_vote_exits_completed() -> None:
    """The quorum decided the step, not the withdrawal."""
    run = Projected(_approve("all"))
    run.start()
    run.vote("sign", "p1", "approved", total=2)
    run.vote("sign", "p2", "cancelled", total=1)
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "completed"
    assert run.state is not None and run.state["data"]["decision"] == "approved"


def test_an_at_least_out_of_reach_after_a_cancelled_vote_exits_completed() -> None:
    """``atLeast`` the remaining approvers cannot reach is a rejection by quorum."""
    run = Projected(_approve("{atLeast: 2}"))
    run.start()
    run.vote("sign", "p1", "cancelled", total=1)
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "completed"
    assert run.state is not None and run.state["data"]["decision"] == "rejected"


def test_every_approval_cancelled_exits_withdrawn() -> None:
    """Nobody is left to vote (``no_approvers``): the step was withdrawn."""
    run = Projected(_approve("all"))
    run.start()
    run.vote("sign", "p1", "cancelled", total=1)
    run.vote("sign", "p2", "cancelled", total=0)
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "withdrawn"
    [event] = [e for e in run.step_events if e.type == EXITED]
    assert process_steps.withdrawn(event)


def test_only_a_withdrawn_exit_is_the_participants() -> None:
    run = Projected(GUARDED_REVIEW)
    run.start()
    run.complete_task("review", {}, status="cancelled")
    assert [process_steps.withdrawn(e) for e in run.step_events] == [False, True]


def test_a_rejected_approval_exits_completed() -> None:
    run = Projected(_approve("any"))
    run.start()
    run.vote("sign", "p1", "rejected", total=1)
    [exited] = run.of(EXITED)
    assert exited["outcome"] == "completed"


def test_one_cancelled_approval_of_several_does_not_close_the_step() -> None:
    run = Projected(_approve("all"))
    run.start()
    run.vote("sign", "p1", "cancelled", total=1)
    assert run.of(EXITED) == []
    assert run.activity("sign")


def test_a_migration_exits_migrated_and_leaves_the_decisions_alone() -> None:
    run = Projected(REVIEW_THEN_SET)
    run.start()
    assert run.state is not None
    before = copy.deepcopy(run.state)
    after = {**copy.deepcopy(before), "activities": {}}
    events = process_steps.step_events(
        run.definition,
        instance_id=INSTANCE,
        before=before,
        after=after,
        decisions=[],
        given={"kind": MIGRATION_INPUT, "body": {}},
        at=run.clock,
        attempts=run.attempts,
        refs=run.refs,
    ).events
    assert [(e.type, e.payload["outcome"]) for e in events] == [(EXITED, "migrated")]


def test_the_engine_output_is_the_same_with_or_without_the_projection() -> None:
    """The projection reads the step; the journal and intents stay the engine's (FR-028)."""
    plain = Run(REVIEW_THEN_SET)
    projected = Projected(REVIEW_THEN_SET)
    for run in (plain, projected):
        run.start()
        run.complete_task("review", {"decision": "yes"})
    assert [
        (i.out(), [d.out() for d in ds], [x.out() for x in xs]) for i, ds, xs in plain.records
    ] == [
        (i.out(), [d.out() for d in ds], [x.out() for x in xs]) for i, ds, xs in projected.records
    ]
    assert all(
        d.kind not in ("step_entered", "step_exited") for _, ds, _ in plain.records for d in ds
    )
