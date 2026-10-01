"""Deadlines and step events in the package test sandbox (CP-ADR-0078 §7; P015).

``expect.sla`` reads a deadline as the projection of an instance does
(:func:`process_sla.shown`) at the test's virtual clock; the deadline itself
is the engine's, counted by the calendar the test runs with, and its timers
fire on ``advance``. ``expect.events`` sees ``process.step_*`` — projected
from every step by :func:`process_steps.step_events`, as ``take()`` projects
them — and the engine's ``process.sla_*``, with the attempt the core adds.
"""

import copy
import dataclasses
from typing import Any

from control_plane.domain import process_definition as pd
from control_plane.domain import process_sandbox as sb
from control_plane.domain.calendar import Calendar
from control_plane.domain.process_engine import Definition
from tests.unit.test_package_test import REVIEW, _live, opened
from tests.unit.test_process_engine import AUTHOR, CALENDARS, CATALOG, spec
from tests.unit.test_process_sla import RU_SPEC

# The calendar ru of the fixtures: 2026-05-01 (Friday) is a holiday, no working hours.
# From Thursday 2026-04-30 09:00Z two working days run over the holiday and the
# weekend to Tuesday 05-05 09:00Z; one working day before it is Monday 05-04 09:00Z.
# Three working days for the process: Wednesday 05-06 09:00Z.
BODY = """
due: {workdays: 3}
stages:
  - id: s
    steps:
      - id: ask
        human:
          taskType: review
          assign: [{role: lead}]
          due: {workdays: 2, warnBefore: {workdays: 1}}
      - {id: done, complete: {outcome: ok}}
"""
THURSDAY = {"clock": "2026-04-30T09:00:00Z", "principals": {"lead": ["alice"]}}
# The same calendar without the holiday: two working days end on Monday.
FLAT = Calendar.from_spec(
    {
        **copy.deepcopy(RU_SPEC),
        "years": [{"year": 2026, "holidays": [], "workdays": []}],
    }
)


def world(body: str = BODY) -> sb.World:
    definition = Definition.build("test", pd.normalized_spec(spec(body)), CATALOG)
    return sb.World(
        definitions={"test": definition},
        skills=CATALOG.skills,
        task_types=CATALOG.task_types,
        agents=CATALOG.agents,
        roles=frozenset({"lead"}),
        calendars={**CALENDARS, "flat": FLAT},
    )


def scenario(steps: list[dict[str, Any]], **given: Any) -> dict[str, Any]:
    return {"process": "test", "name": "t", "given": {**THURSDAY, **given}, "steps": steps}


def run(steps: list[dict[str, Any]], **given: Any) -> sb.TestResult:
    return sb.run_test(world(), "tests/t.test.yaml", scenario(steps, **given))


def sandbox(steps: list[dict[str, Any]]) -> tuple[sb.Sandbox, list[sb.Failure]]:
    """The sandbox after the steps, for what a test result does not carry."""
    box = sb.Sandbox(world(), scenario(steps), seed="t")
    box.start_given()
    failures = [f for i, step in enumerate(steps) for f in sb._run_step(box, i, step)]
    return box, failures


def test_expect_sla_follows_the_deadline_over_the_holiday_and_the_weekend() -> None:
    steps = [
        opened(),
        {
            "expect": {
                "sla": {"ask": "ok", "process": "ok"},
                "events": ["process.started", "process.step_entered"],
            }
        },
        # Two days of the clock: astronomically due, by the calendar not even warned.
        {"advance": "P2D"},
        {"expect": {"sla": {"ask": "ok", "process": "ok"}}},
        {"advance": "P2D"},
        {"expect": {"sla": {"ask": "warning", "process": "ok"}, "events": ["process.sla_warning"]}},
        {"advance": "P1D"},
        {
            "expect": {
                "sla": {"ask": "breached", "process": "ok"},
                "events": ["process.sla_breached"],
            }
        },
        {"complete": {"step": "ask", "by": "alice", "output": {"decision": "late"}}},
        {
            "expect": {
                "status": "completed",
                "sla": {"process": "ok"},
                "events": ["process.step_exited", "process.completed"],
            }
        },
        # A closed instance is read at its close: done in time stays in time.
        {"advance": "P7D"},
        {"expect": {"sla": {"process": "ok"}}},
    ]
    result = run(steps)
    assert result.status == "passed", result.failures


def test_the_process_deadline_is_breached_by_the_clock_of_the_test() -> None:
    result = run([opened(), {"advance": "P6D"}, {"expect": {"sla": {"process": "breached"}}}])
    assert result.status == "passed", result.failures


def test_the_calendar_the_test_runs_with_counts_the_deadline() -> None:
    # Without the holiday two working days from Thursday end on Monday 05-04,
    # warned on Friday 05-01.
    steps = [
        opened(),
        {"advance": "P1D"},
        {"expect": {"sla": {"ask": "warning"}, "events": ["process.sla_warning"]}},
        {"advance": "P3D"},
        {"expect": {"sla": {"ask": "breached"}, "events": ["process.sla_breached"]}},
    ]
    assert run(steps, calendar="flat").status == "passed"
    held = run(steps)
    assert held.status == "failed"
    assert [(f.step, f.expected, f.actual) for f in held.failures] == [
        (2, "warning", "ok"),
        (
            2,
            "process.sla_warning",
            ["process.started", "process.stage_entered", "process.step_entered"],
        ),
        (4, "breached", "warning"),
        (4, "process.sla_breached", ["process.sla_warning"]),
    ]


def test_a_mismatch_names_the_step_the_expected_and_the_actual_state() -> None:
    result = run(
        [
            opened(),
            {"expect": {"sla": {"ask": "breached", "process": "warning", "nothing": "ok"}}},
            {"complete": {"step": "ask", "by": "alice", "output": {"decision": "yes"}}},
            {"expect": {"sla": {"ask": "ok"}}},
        ]
    )
    assert result.status == "failed"
    ask, process, nothing, closed = result.failures
    assert (ask.step, ask.expected, ask.actual) == (1, "breached", "ok")
    assert ask.message == (
        "SLA of step 'ask' at 2026-04-30T09:00:00Z"
        " (due 2026-05-05T09:00:00Z, warning 2026-05-04T09:00:00Z): ok, expected breached"
    )
    assert (process.expected, process.actual) == ("warning", "ok")
    assert process.message.startswith("SLA of the process at 2026-04-30T09:00:00Z")
    assert "(due 2026-05-06T09:00:00Z, warning -)" in process.message
    assert (nothing.expected, nothing.actual) == ("ok", None)
    assert nothing.message == "SLA: the process has no step 'nothing'"
    assert (closed.step, closed.expected, closed.actual) == (3, "ok", None)
    assert closed.message == "SLA of step 'ask': the step has no open attempt"


def test_a_step_without_a_deadline_shows_none() -> None:
    result = sb.run_test(
        world(BODY.replace("          due: {workdays: 2, warnBefore: {workdays: 1}}\n", "")),
        "tests/t.test.yaml",
        scenario([opened(), {"expect": {"sla": {"ask": "ok"}}}]),
    )
    assert [(f.expected, f.actual) for f in result.failures] == [("ok", "none")]


def test_step_and_sla_events_carry_what_the_core_records() -> None:
    box, failures = sandbox(
        [
            opened(),
            {"advance": "P5D"},
            {"complete": {"step": "ask", "by": "alice", "output": {"decision": "late"}}},
        ]
    )
    assert failures == []
    events = [e for e in box.events if e["type"].startswith(("process.step_", "process.sla_"))]
    assert [e["type"] for e in events] == [
        "process.step_entered",
        "process.sla_warning",
        "process.sla_breached",
        "process.step_exited",
    ]
    entered, warning, breached, exited = (e["payload"] for e in events)
    (task,) = box.tasks
    assert (entered["element"], entered["attempt"], entered["taskId"]) == ("ask", 1, task.id)
    assert (entered["due"], entered["warnAt"]) == ("2026-05-05T09:00:00Z", "2026-05-04T09:00:00Z")
    # The attempt of an SLA fact is the core's count, not the engine's null.
    assert (warning["scope"], warning["attempt"]) == ("step", 1)
    assert (breached["attempt"], breached["dueAt"]) == (1, "2026-05-05T09:00:00Z")
    assert (exited["outcome"], exited["breached"], exited["attempt"]) == ("completed", True, 1)
    assert exited["activityId"] == entered["activityId"] == warning["activityId"]


def test_a_trial_run_counts_the_attempts_on_from_the_live_instance() -> None:
    live, _ = _live(REVIEW, until="review")
    activity = next(iter(live.state["activities"]))
    # The live instance entered review twice; the open activity is its second attempt.
    live = dataclasses.replace(live, attempts={"review": 2}, entered={activity: 2})
    here = dataclasses.replace(world(REVIEW), live={live.id: live})
    steps = [{"complete": {"step": "review", "by": "bob", "output": {"decision": "go"}}}]
    given = {"fromInstance": live.id, "principals": {"lead": [AUTHOR, "bob"]}}
    box = sb.Sandbox(
        here, {"process": "test", "name": "t", "given": given, "steps": steps}, seed="t"
    )
    box.start_given()
    assert sb._run_step(box, 0, steps[0]) == []
    exited, entered = (e for e in box.events if e["type"].startswith("process.step_"))
    assert (exited["type"], exited["payload"]["element"]) == ("process.step_exited", "review")
    assert (exited["payload"]["attempt"], exited["payload"]["activityId"]) == (2, activity)
    assert (entered["type"], entered["payload"]["element"]) == ("process.step_entered", "sign")
    assert entered["payload"]["attempt"] == 1
    assert set(entered["payload"]["approvalIds"]) == {a.id for a in box.approvals}
