"""SLA deadlines of steps and of the process in the engine (CP-ADR-0078 §1, §3; P011).

Under engine revision 2 a waiting step with ``due`` sets the timers ``sla``
and, with ``warnBefore``, ``sla_warning`` of its activity; ``spec.due`` sets
them for the process at the start. A timer that fires is a decision and a
``process.sla_warning``/``process.sla_breached`` event (checked against the
event catalog by :class:`Run`); closing the step or the instance cancels
them. A deadline that cannot be computed is ``process.sla_failed`` and the
instance goes on. Revision 1 knows none of it (FR-031).
"""

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import yaml

from control_plane.domain import process_engine as pe
from control_plane.domain import process_sla as sla
from control_plane.domain import process_steps
from control_plane.domain.calendar import Calendar
from control_plane.domain.process_engine import Input
from tests.unit.test_process_contract import _yaml12_loader
from tests.unit.test_process_engine import CALENDARS, FIXTURES, INSTANCE, T0, Run, _check_event

RU_SPEC = yaml.load((FIXTURES / "ru.calendar.yaml").read_text(), Loader=_yaml12_loader())["spec"]
HOURS_SPEC = {
    **copy.deepcopy(RU_SPEC),
    "workingHours": {"intervals": [{"from": "09:00", "to": "18:00"}], "shortDayReduction": "PT1H"},
}
WITH_HOURS = {"ru": Calendar.from_spec(HOURS_SPEC)}


class HoursRun(Run):
    """A run whose inputs carry ``calendars`` (by default ``ru`` with working hours)."""

    calendars: dict[str, Calendar] = WITH_HOURS

    def feed(
        self,
        kind: str,
        body: dict[str, Any] | None = None,
        *,
        at: datetime | None = None,
        actor: str | None = None,
    ) -> tuple[list[pe.Decision], list[pe.Intent]]:
        self.clock = at or self.clock
        given = Input(kind, self.clock, body or {}, actor, self.calendars)
        state, decisions, intents = pe.step(self.definition, self.state, given)
        self.state = copy.deepcopy(state)
        self.records.append((given, decisions, intents))
        for intent in intents:
            if intent.kind == "emit_event":
                _check_event(intent.body["type"], intent.body["payload"])
        return decisions, intents


def _step(body: str) -> str:
    """One stage with the step ``ask`` (a human task) configured by ``body``, then done."""
    return f"""
stages:
  - id: s
    steps:
      - id: ask
        human:
          taskType: review
          assign: [{{role: lead}}]
{body}
      - {{id: done, complete: {{outcome: ok}}}}
"""


def _fire(run: Run, kind: str, *, element: str = "ask", late: timedelta = timedelta(0)) -> Any:
    timer = run.timer(element, kind)
    due = datetime.fromisoformat(timer["dueAt"].replace("Z", "+00:00"))
    detected = (due + late).isoformat().replace("+00:00", "Z")
    return run.feed("timer", {"timerId": timer["id"], "detectedAt": detected}, at=due)


def _time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


# --- recipes ------------------------------------------------------------------------------


def test_the_forms_of_a_due_are_recipes() -> None:
    at = "/spec/stages/0/steps/0/human/due"
    assert sla.due_recipe("P2D", at, "ru") == {"kind": "duration", "value": "P2D"}
    assert sla.due_recipe({"at": "data.deadline"}, at, "ru") == {"kind": "at", "path": at + "/at"}
    assert sla.due_recipe({"duration": "PT4H"}, at, None) == {"kind": "duration", "value": "PT4H"}
    assert sla.due_recipe({"workdays": 2}, at, "ru") == {
        "kind": "workdays",
        "n": 2,
        "calendar": "ru",
    }
    assert sla.due_recipe({"workhours": 8, "calendar": "other"}, at, "ru") == {
        "kind": "workhours",
        "hours": 8,
        "calendar": "other",
    }
    due = sla.due_recipe({"workhours": 8}, at, "ru")
    assert sla.warn_recipe({"workhours": 8}, due, "ru") is None
    assert sla.warn_recipe({"workhours": 8, "warnBefore": "PT1H"}, due, "ru") == {
        "kind": "before",
        "due": due,
        "span": {"kind": "duration", "value": "PT1H"},
    }
    assert sla.warn_recipe({"workhours": 8, "warnBefore": {"workdays": 1}}, due, "ru") == {
        "kind": "before",
        "due": due,
        "span": {"kind": "workdays", "n": 1, "calendar": "ru"},
    }


def test_working_units_count_by_the_working_hours_of_the_calendar() -> None:
    # T0 is Monday 2026-03-02 12:00 in Moscow (09:00Z).
    hours = sla.due_recipe({"workhours": 8}, "/x", "ru")
    moment, provisional = sla.working(hours, T0, WITH_HOURS)
    # 6 hours on Monday to 18:00, 2 on Tuesday from 09:00: Tuesday 11:00 MSK.
    assert (moment, provisional) == (datetime(2026, 3, 3, 8, 0, tzinfo=UTC), False)
    back, _ = sla.working(hours, moment, WITH_HOURS, back=True)
    assert back == T0

    days = sla.due_recipe({"workdays": 2}, "/x", "ru")
    assert sla.working(days, T0, WITH_HOURS)[0] == datetime(2026, 3, 4, 9, 0, tzinfo=UTC)
    # From a Saturday the count starts at the next working interval: Monday 09:00 MSK.
    saturday = datetime(2026, 3, 7, 7, 0, tzinfo=UTC)
    assert sla.working(days, saturday, WITH_HOURS)[0] == datetime(2026, 3, 11, 6, 0, tzinfo=UTC)
    # Without working hours the time of day stays: cal.addWorkdays, Tuesday 10:00 MSK.
    assert sla.working(days, saturday, CALENDARS)[0] == datetime(2026, 3, 10, 7, 0, tzinfo=UTC)
    # A provisional year marks the answer.
    assert sla.working(days, datetime(2027, 3, 1, 9, tzinfo=UTC), WITH_HOURS)[1] is True


@pytest.mark.parametrize(
    ("recipe", "calendars", "code"),
    [
        ({"kind": "workdays", "n": 1, "calendar": None}, WITH_HOURS, "calendar_missing"),
        ({"kind": "workdays", "n": 1, "calendar": "gone"}, WITH_HOURS, "calendar_missing"),
        ({"kind": "workhours", "hours": 8, "calendar": "ru"}, CALENDARS, "calendar_without_hours"),
    ],
)
def test_a_working_unit_without_its_calendar_is_a_deadline_error(
    recipe: dict[str, Any], calendars: dict[str, Calendar], code: str
) -> None:
    with pytest.raises(sla.DeadlineError) as raised:
        sla.working(recipe, T0, calendars)
    assert raised.value.code == code


# --- deadlines of steps -------------------------------------------------------------------


def test_sla_events_carry_the_candidates_of_their_addressees() -> None:
    """The engine computes the chains; the application resolves them into ids (P013)."""
    owner = "owner: [{expr: data.note}, {expr: \"'role:' + data.number\"}, {role: lead}]\n"
    run = Run(owner + _step("          due: {duration: PT4H, warnBefore: PT1H}"))
    run.start()
    _fire(run, "sla_warning")
    _fire(run, "sla")
    # data.note is not set: that candidate fails and is skipped, not an error.
    chain = [{"role": "N-1"}, {"role": "lead"}]
    for event in run.intents("emit_event")[-2:]:
        assert event["addressees"] == {"owner": chain, "assignee": [{"role": "lead"}]}
        assert (event["payload"]["owner"], event["payload"]["assignee"]) == (None, None)
    assert run.status == "running"

    failed = Run(owner + _step("          due: {at: timestamp(data.note)}"))
    failed.start()
    [event] = [e for e in failed.intents("emit_event") if e["type"] == "process.sla_failed"]
    assert event["addressees"] == {"owner": chain}
    # Other events name no addressees.
    assert all(
        "addressees" not in e for e in run.events() if not e["type"].startswith("process.sla_")
    )


def test_a_step_without_escalations_gives_its_warning_and_its_breach() -> None:
    run = Run(_step("          due: {duration: PT4H, warnBefore: PT1H}"))
    run.start()
    activity = run.activity("ask")
    assert activity["sla"] == {
        "state": "pending",
        "dueAt": _time(T0 + timedelta(hours=4)),
        "warnAt": _time(T0 + timedelta(hours=3)),
        "provisional": False,
        "timer": run.timer("ask", "sla")["id"],
        "warnTimer": run.timer("ask", "sla_warning")["id"],
        "error": None,
    }
    # The task's due is the same deadline: one notion of due (CP-ADR-0078 §1).
    [task] = run.intents("create_task")
    assert task["due"] == _time(T0 + timedelta(hours=4))

    _fire(run, "sla_warning")
    [warning] = run.events("process.sla_warning")
    assert warning == {
        "instanceId": INSTANCE,
        "definitionKey": "test",
        "version": 1,
        "instanceKey": "N-1",
        "scope": "step",
        "element": "ask",
        "attempt": None,
        "activityId": activity["id"],
        "dueAt": _time(T0 + timedelta(hours=4)),
        "warnAt": _time(T0 + timedelta(hours=3)),
        "provisional": False,
        "owner": None,
        "assignee": None,
    }
    assert run.activity("ask")["sla"]["state"] == "warning"

    _fire(run, "sla", late=timedelta(seconds=42))
    [breached] = run.events("process.sla_breached")
    assert breached["dueAt"] == _time(T0 + timedelta(hours=4))
    assert breached["detectedAt"] == _time(T0 + timedelta(hours=4, seconds=42))
    assert (breached["overdueSeconds"], breached["detectedBy"]) == (42, "timer")
    assert (breached["scope"], breached["element"]) == ("step", "ask")
    assert run.activity("ask")["sla"]["state"] == "breached"
    # The fact is the SLA event, not process.timer_fired; the step stays open.
    assert not run.events("process.timer_fired")
    assert [d["kind"] for d in (d.out() for d in run.records[-1][1])] == [
        "timer_fired",
        "sla_breached",
    ]
    assert run.status == "running"


def test_escalations_after_the_due_work_as_before_next_to_the_breach() -> None:
    run = Run(
        _step(
            "          due: PT2H\n"
            "          escalations: [{after: due, action: notify, to: [{role: boss}]}]"
        )
    )
    run.start()
    assert run.timer("ask", "escalation")["dueAt"] == run.timer("ask", "sla")["dueAt"]
    _fire(run, "sla")
    run.fire("ask", "escalation")
    assert len(run.events("process.sla_breached")) == 1
    assert len(run.events("process.escalated")) == 1


def test_no_warning_without_a_declared_threshold() -> None:
    run = Run(_step("          due: {duration: PT4H}"))
    run.start()
    assert [t["kind"] for t in run.state["timers"].values()] == ["sla"]  # type: ignore[index]
    assert run.activity("ask")["sla"]["warnAt"] is None
    _fire(run, "sla")
    assert not run.events("process.sla_warning")
    assert len(run.events("process.sla_breached")) == 1


def test_closing_the_step_before_its_due_cancels_the_deadline() -> None:
    run = Run(_step("          due: {duration: PT4H, warnBefore: PT1H}"))
    run.start()
    timers = {run.timer("ask", "sla")["id"], run.timer("ask", "sla_warning")["id"]}
    run.complete_task("ask", {"decision": "yes"}, at=T0 + timedelta(hours=1))
    cancelled = {i["timerId"] for i in run.intents("cancel_timer")}
    assert timers <= cancelled
    assert run.status == "completed"
    assert not run.events("process.sla_breached")


def test_every_entry_into_a_step_is_a_deadline_of_its_own() -> None:
    run = Run(
        """
onEvent:
  - on: {observation: case.ping}
    do:
      - id: ask
        human: {taskType: review, assign: [{role: lead}], due: PT2H}
stages:
  - id: s
    steps:
      - {id: hold, listen: {any: [{on: {observation: case.never}}]}}
"""
    )
    run.start()
    run.observe("case.ping")
    run.clock = T0 + timedelta(hours=1)
    run.observe("case.ping")
    assert run.state is not None
    dues = sorted(
        a["sla"]["dueAt"] for a in run.state["activities"].values() if a["element"] == "ask"
    )
    assert dues == [_time(T0 + timedelta(hours=2)), _time(T0 + timedelta(hours=3))]


def test_every_waiting_kind_but_wait_takes_a_due() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - {id: work, call: {skill: work.do@1, due: PT1H}}
      - id: hold
        listen: {any: [{on: {observation: case.go}}], due: {duration: PT3H, warnBefore: PT1H}}
"""
    )
    run.start()
    assert run.activity("work")["sla"]["dueAt"] == _time(T0 + timedelta(hours=1))
    run.skill("work", {"value": "x"}, at=T0 + timedelta(minutes=90))
    [exited] = [
        e
        for e in _step_events(run)
        if e.type == process_steps.STEP_EXITED and e.payload["element"] == "work"
    ]
    # Closed after its due, before the timer fired: breached by the time (FR-021).
    assert exited.payload["due"] == _time(T0 + timedelta(hours=1))
    assert (exited.payload["breached"], exited.payload["overdueSeconds"]) == (True, 1800)
    hold = run.activity("hold")
    assert (hold["sla"]["dueAt"], hold["sla"]["warnAt"]) == (
        _time(T0 + timedelta(minutes=90, hours=3)),
        _time(T0 + timedelta(minutes=90, hours=2)),
    )


def _step_events(run: Run) -> list[process_steps.StepEvent]:
    """The step events of every step of ``run``, projected as the application does."""
    events: list[process_steps.StepEvent] = []
    before: dict[str, Any] | None = None
    attempts: dict[str, int] = {}
    refs: dict[str, Any] = {}
    for given, decisions, _ in run.records:
        after, _, _ = pe.step(run.definition, before, given)
        projection = process_steps.step_events(
            run.definition,
            instance_id=INSTANCE,
            before=before,
            after=after,
            decisions=[d.out() for d in decisions],
            given=given.out(),
            at=given.at,
            attempts=attempts,
            refs=refs,
        )
        events += projection.events
        before, attempts, refs = after, projection.attempts, projection.refs
    return events


def test_step_events_carry_the_deadline() -> None:
    run = Run(_step("          due: {duration: PT4H, warnBefore: PT1H}"))
    run.start()
    _fire(run, "sla")
    run.complete_task("ask", {"decision": "yes"}, at=T0 + timedelta(hours=5))
    entered, exited = _step_events(run)
    assert (entered.payload["due"], entered.payload["warnAt"], entered.payload["provisional"]) == (
        _time(T0 + timedelta(hours=4)),
        _time(T0 + timedelta(hours=3)),
        False,
    )
    assert (exited.payload["due"], exited.payload["breached"]) == (
        _time(T0 + timedelta(hours=4)),
        True,
    )
    assert exited.payload["overdueSeconds"] == 3600


def test_a_step_closed_in_time_exits_unbreached() -> None:
    run = Run(_step("          due: PT4H"))
    run.start()
    run.complete_task("ask", {"decision": "yes"}, at=T0 + timedelta(hours=4))
    _, exited = _step_events(run)
    assert (exited.payload["breached"], exited.payload["overdueSeconds"]) == (False, None)


def test_a_deadline_that_cannot_be_computed_is_sla_failed_and_the_instance_goes_on() -> None:
    # The calendar of the inputs declares no working hours (published later without them).
    run = Run(
        _step(
            "          due: {workhours: 8}\n"
            "          escalations: [{after: due, action: notify, to: [{role: boss}]}]"
        )
    )
    run.start()
    assert run.status == "running"
    [failed] = run.events("process.sla_failed")
    assert (failed["scope"], failed["element"]) == ("step", "ask")
    assert failed["error"]["type"] == "calendar_without_hours"
    record = run.activity("ask")["sla"]
    assert (record["state"], record["dueAt"]) == ("failed", None)
    assert record["error"]["type"] == "calendar_without_hours"
    # The task comes without a due; levels counted from it are skipped.
    [task] = run.intents("create_task")
    assert task["due"] is None
    assert [d["reason"] for d in run.decisions("escalation_skipped")] == ["due_failed"]
    assert not [t for t in run.state["timers"].values()]  # type: ignore[index]
    run.complete_task("ask", {"decision": "yes"})
    assert run.status == "completed"


def test_an_expression_error_of_a_due_is_sla_failed() -> None:
    run = Run(
        """
stages:
  - id: s
    steps:
      - id: hold
        listen:
          any: [{on: {observation: case.go}}]
          due: {at: "timestamp(data.note)"}
"""
    )
    run.start()
    assert run.status == "running"
    [failed] = run.events("process.sla_failed")
    assert failed["element"] == "hold" and failed["error"]["status"] == 422
    assert run.activity("hold")["sla"]["state"] == "failed"


def test_a_new_calendar_version_moves_the_deadline_timers() -> None:
    run = HoursRun(_step("          due: {workdays: 2, warnBefore: {workhours: 4}}"))
    run.start()
    # Wednesday 12:00 MSK; 4 working hours before it: 3 on Wednesday, 1 on Tuesday.
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-04T09:00:00Z"
    assert run.timer("ask", "sla_warning")["dueAt"] == "2026-03-03T14:00:00Z"
    moved = copy.deepcopy(HOURS_SPEC)
    moved["years"][0]["holidays"].append("2026-03-04")
    run.calendars = {"ru": Calendar.from_spec(moved)}
    run.feed("calendar", {"key": "ru"})
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-05T09:00:00Z"
    record = run.activity("ask")["sla"]
    # Wednesday is a holiday now: 3 working hours on Thursday, 1 on Tuesday.
    assert (record["dueAt"], record["warnAt"]) == ("2026-03-05T09:00:00Z", "2026-03-03T14:00:00Z")
    causes = {e["cause"] for e in run.events("process.timer_rescheduled")}
    assert causes == {"calendar_changed"}
    # Another calendar moves nothing.
    run.feed("calendar", {"key": "other"})
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-05T09:00:00Z"


def test_a_due_from_the_data_moves_with_the_data() -> None:
    run = Run(_step('          due: {at: "data.deadline"}'))
    run.start(deadline="2026-03-10T09:00:00Z")
    assert run.activity("ask")["sla"]["dueAt"] == "2026-03-10T09:00:00Z"
    run.observe("case.changed", number="N-1", deadline="2026-03-12T09:00:00Z")
    assert run.activity("ask")["sla"]["dueAt"] == "2026-03-12T09:00:00Z"
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-12T09:00:00Z"


# --- the deadline of the process ----------------------------------------------------------


PROCESS_DUE = """
due: {workhours: 16, warnBefore: {workhours: 4}}
stages:
  - id: s
    steps:
      - {id: hold, listen: {any: [{on: {observation: case.go}}]}}
      - {id: done, complete: {outcome: ok}}
"""


def test_the_process_deadline_counts_from_the_start() -> None:
    run = HoursRun(PROCESS_DUE)
    run.start()
    assert run.state is not None
    record = run.state["sla"]
    # 16 working hours from Monday 12:00 MSK: 6 + 9 + 1, Wednesday 10:00 MSK; the
    # warning 4 working hours before it, Tuesday 15:00 MSK.
    assert (record["dueAt"], record["warnAt"]) == ("2026-03-04T07:00:00Z", "2026-03-03T12:00:00Z")
    timer = run.timer("process", "sla")
    assert (timer["scope"], timer["sla"]) == ("process", "process")
    _fire(run, "sla_warning", element="process")
    _fire(run, "sla", element="process", late=timedelta(seconds=5))
    [warning] = run.events("process.sla_warning")
    [breached] = run.events("process.sla_breached")
    for event in (warning, breached):
        assert (event["scope"], event["element"], event["attempt"], event["activityId"]) == (
            "process",
            None,
            None,
            None,
        )
    assert (breached["dueAt"], breached["overdueSeconds"]) == ("2026-03-04T07:00:00Z", 5)
    assert run.state["sla"]["state"] == "breached"
    assert run.status == "running"


def test_the_end_of_the_instance_cancels_the_process_deadline() -> None:
    run = HoursRun(PROCESS_DUE)
    run.start()
    ids = {run.timer("process", "sla")["id"], run.timer("process", "sla_warning")["id"]}
    run.observe("case.go")
    assert run.status == "completed"
    assert ids <= {i["timerId"] for i in run.intents("cancel_timer")}
    assert run.state is not None and not run.state["timers"]


def test_a_process_deadline_without_a_calendar_is_sla_failed() -> None:
    run = HoursRun(PROCESS_DUE)
    run.calendars = {}
    run.start()
    assert run.state is not None
    assert run.status == "running"
    assert run.state["sla"]["state"] == "failed"
    [failed] = run.events("process.sla_failed")
    assert (failed["scope"], failed["element"], failed["error"]["type"]) == (
        "process",
        None,
        "calendar_missing",
    )


# --- revision 1 ---------------------------------------------------------------------------


def test_a_version_of_revision_1_gets_no_deadlines() -> None:
    for body in (_step("          due: {duration: PT4H, warnBefore: PT1H}"), PROCESS_DUE):
        run = HoursRun(body)
        run.definition = replace(run.definition, engine_revision=1)
        run.start()
        assert run.state is not None
        assert "sla" not in run.state
        assert all("sla" not in a for a in run.state["activities"].values())
        kinds = {t["kind"] for t in run.state["timers"].values()}
        assert not kinds & set(sla.SLA_TIMERS)
        assert not [t for t in run.event_types() if t.startswith("process.sla_")]


def test_a_due_that_fails_under_revision_1_is_still_an_error_of_the_step() -> None:
    run = Run(_step('          due: {at: "timestamp(data.note)"}'))
    run.definition = replace(run.definition, engine_revision=1)
    run.start()
    assert run.status == "failed"
    assert not run.events("process.sla_failed")
