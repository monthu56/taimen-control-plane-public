"""SLA clocks stop while an instance is suspended (CP-ADR-0078 §4; P012, FR-016, FR-019).

A suspension freezes the deadline timers ``sla``/``sla_warning`` and keeps
what is left of them in the unit of the deadline: wall-clock seconds for a
duration, working seconds for ``workhours`` (and ``workdays`` of a calendar
with working hours), whole working days and the time of day for ``workdays``
of a calendar without them. The resume counts the remainder on by the
calendar; a deadline from the data (``{at}``) does not move. A new calendar
version recounts a frozen remainder.

The calendar is the one of the working-time control set (P008,
``ru-2025-2027.calendar.yaml``); times in comments are Moscow wall-clock times
(UTC+3).
"""

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from control_plane.domain import process_sla as sla
from control_plane.domain.calendar import Calendar
from tests.unit.test_process_sla import HoursRun, _step

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
RU_SPEC: dict[str, Any] = yaml.safe_load(
    (FIXTURES / "ru-2025-2027.calendar.yaml").read_text("utf-8")
)["spec"]
RU = {"ru": Calendar.from_spec(RU_SPEC)}
WITHOUT_HOURS = {
    "ru": Calendar.from_spec({k: v for k, v in RU_SPEC.items() if k != "workingHours"})
}

# The pause of the control set: entered Wed 7 Oct 2026 10:00, paused Thu 8 Oct
# 12:00, resumed Sat 10 Oct 14:00.
ENTERED = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
PAUSED = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
RESUMED = datetime(2026, 10, 10, 11, 0, tzinfo=UTC)
H = 3600.0


def _time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _run(due: str, calendars: dict[str, Calendar] = RU) -> HoursRun:
    run = HoursRun(_step(f"          due: {due}"))
    run.calendars = calendars
    run.clock = ENTERED
    run.start()
    return run


def _row(run: HoursRun, timer_id: str) -> dict[str, Any]:
    """The last row the engine asked the core to keep for a timer."""
    return [i for i in run.intents("set_timer") if i["timerId"] == timer_id][-1]


# --- remainders -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recipe", "calendars", "due", "remaining", "unit", "resumed"),
    [
        pytest.param(
            # Thu 17:00: Thu 12-17 is 5h; from Sat 14:00, Mon 09-14.
            {"kind": "workhours", "hours": 16, "calendar": "ru"},
            RU,
            "2026-10-08T14:00:00Z",
            5 * H,
            sla.WORKING_SECONDS,
            "2026-10-12T11:00:00Z",
            id="workhours",
        ),
        pytest.param(
            # Fri 10:00: Thu 12-18 is 6h and Fri 09-10 1h; from Sat 14:00, Mon 09-16.
            {"kind": "workdays", "n": 2, "calendar": "ru"},
            RU,
            "2026-10-09T07:00:00Z",
            7 * H,
            sla.WORKING_SECONDS,
            "2026-10-12T13:00:00Z",
            id="workdays-with-hours",
        ),
        pytest.param(
            # Fri 10:00 is one working day after Thu and 10:00 of it; one working
            # day after Sat is Mon, 10:00.
            {"kind": "workdays", "n": 2, "calendar": "ru"},
            WITHOUT_HOURS,
            "2026-10-09T07:00:00Z",
            1 * sla.DAY + 10 * H,
            sla.WORKDAYS,
            "2026-10-12T07:00:00Z",
            id="workdays-without-hours",
        ),
        pytest.param(
            # Wall-clock time: the 27 hours left run on from the resume.
            {"kind": "duration", "value": "P2D"},
            RU,
            "2026-10-09T12:00:00Z",
            27 * H,
            sla.WALL,
            "2026-10-11T14:00:00Z",
            id="duration",
        ),
    ],
)
def test_a_remainder_is_kept_in_the_unit_of_the_deadline(
    recipe: dict[str, Any],
    calendars: dict[str, Calendar],
    due: str,
    remaining: float,
    unit: str,
    resumed: str,
) -> None:
    assert sla.remainder(recipe, _time(due), PAUSED, calendars) == (remaining, unit)
    if unit == sla.WALL:
        assert RESUMED + timedelta(seconds=remaining) == _time(resumed)
    else:
        assert sla.thaw(recipe, remaining, unit, RESUMED, calendars) == (_time(resumed), False)


def test_a_deadline_from_the_data_keeps_no_remainder() -> None:
    at = {"kind": "at", "path": "/x/at"}
    assert sla.remainder(at, _time("2026-10-09T07:00:00Z"), PAUSED, RU) == (None, sla.WALL)
    warning = {"kind": "before", "due": at, "span": {"kind": "duration", "value": "PT1H"}}
    assert sla.remainder(warning, _time("2026-10-09T06:00:00Z"), PAUSED, RU) == (None, sla.WALL)


def test_a_warning_keeps_its_remainder_in_the_unit_of_its_deadline() -> None:
    due = {"kind": "workhours", "hours": 16, "calendar": "ru"}
    warning = {"kind": "before", "due": due, "span": {"kind": "duration", "value": "PT1H"}}
    # Thu 16:00: Thu 12-16 is 4h of working time.
    assert sla.remainder(warning, _time("2026-10-08T13:00:00Z"), PAUSED, RU) == (
        4 * H,
        sla.WORKING_SECONDS,
    )


def test_a_timer_already_due_keeps_nothing() -> None:
    recipe = {"kind": "workhours", "hours": 16, "calendar": "ru"}
    assert sla.remainder(recipe, PAUSED - timedelta(minutes=1), PAUSED, RU) == (0.0, sla.WALL)


def test_overdue_time_leaves_out_the_stops_after_the_due() -> None:
    due = PAUSED - timedelta(hours=1)
    stops = [
        # Before the due: not overdue time anyway.
        {"from": "2026-10-08T06:00:00Z", "to": "2026-10-08T07:00:00Z"},
        # Across the due: only its part after the due is left out.
        {"from": "2026-10-08T07:30:00Z", "to": "2026-10-08T08:30:00Z"},
        # Wholly after the due: left out.
        {"from": "2026-10-08T09:00:00Z", "to": "2026-10-08T10:00:00Z"},
    ]
    at = PAUSED + timedelta(hours=2)
    assert sla.overdue_seconds(due, at) == int(3 * H)
    assert sla.overdue_seconds(due, at, stops) == int(1.5 * H)
    # A stop still going on at ``at`` counts up to it.
    assert sla.overdue_seconds(due, due + timedelta(minutes=15), stops) == 0
    assert sla.overdue_seconds(due, PAUSED + timedelta(minutes=30), stops) == int(0.5 * H)


def test_workdays_with_no_whole_day_left_resume_on_the_next_working_day() -> None:
    recipe = {"kind": "workdays", "n": 2, "calendar": "ru"}
    # Paused Fri 9 Oct 08:00 before its deadline at 10:00 the same day.
    remaining, unit = sla.remainder(
        recipe, _time("2026-10-09T07:00:00Z"), _time("2026-10-09T05:00:00Z"), WITHOUT_HOURS
    )
    assert (remaining, unit) == (10 * H, sla.WORKDAYS)
    # Resumed on Saturday: the rest of the day is Monday's, 10:00.
    assert sla.thaw(recipe, remaining, unit, RESUMED, WITHOUT_HOURS)[0] == _time(
        "2026-10-12T07:00:00Z"
    )
    # Resumed on a working day before 10:00: that day.
    monday = _time("2026-10-12T05:30:00Z")
    assert sla.thaw(recipe, remaining, unit, monday, WITHOUT_HOURS)[0] == _time(
        "2026-10-12T07:00:00Z"
    )


def test_a_remainder_without_its_calendar_is_a_deadline_error() -> None:
    recipe = {"kind": "workhours", "hours": 16, "calendar": "ru"}
    with pytest.raises(sla.DeadlineError) as raised:
        sla.thaw(recipe, 5 * H, sla.WORKING_SECONDS, RESUMED, {})
    assert raised.value.code == "calendar_missing"


# --- the engine ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("due", "calendars", "declared", "remaining", "unit", "resumed"),
    [
        (
            "{workhours: 16}",
            RU,
            "2026-10-08T14:00:00Z",
            5 * H,
            "working_seconds",
            "2026-10-12T11:00:00Z",
        ),
        (
            "{workdays: 2}",
            RU,
            "2026-10-09T07:00:00Z",
            7 * H,
            "working_seconds",
            "2026-10-12T13:00:00Z",
        ),
        (
            "{workdays: 2}",
            WITHOUT_HOURS,
            "2026-10-09T07:00:00Z",
            sla.DAY + 10 * H,
            "workdays",
            "2026-10-12T07:00:00Z",
        ),
    ],
    ids=["workhours", "workdays-with-hours", "workdays-without-hours"],
)
def test_an_operator_pause_over_the_weekend_moves_the_deadline_by_working_time(
    due: str,
    calendars: dict[str, Calendar],
    declared: str,
    remaining: float,
    unit: str,
    resumed: str,
) -> None:
    run = _run(due, calendars)
    timer = run.timer("ask", "sla")
    assert timer["dueAt"] == declared

    run.clock = PAUSED
    run.command("suspend", reason="supplier sent a new invoice")
    frozen = run.timer("ask", "sla")
    assert (frozen["state"], frozen["dueAt"]) == ("frozen", None)
    assert (frozen["remaining"], frozen["remainingUnit"]) == (remaining, unit)
    row = _row(run, timer["id"])
    assert (row["state"], row["remainingSeconds"], row["remainingUnit"]) == (
        "frozen",
        remaining,
        unit,
    )
    # The record of the deadline keeps its moment while frozen.
    assert run.activity("ask")["sla"]["dueAt"] == declared

    # The declared deadline passes during the pause: a late timer input is stale.
    run.feed("timer", {"timerId": timer["id"]}, at=_time(declared) + timedelta(hours=1))
    assert not run.events("process.sla_breached")

    run.clock = RESUMED
    run.command("resume")
    moved = run.timer("ask", "sla")
    assert (moved["state"], moved["dueAt"]) == ("pending", resumed)
    assert run.activity("ask")["sla"]["dueAt"] == resumed
    assert "remainingUnit" not in _row(run, timer["id"])
    [rescheduled] = run.events("process.timer_rescheduled")
    assert (rescheduled["previousDueAt"], rescheduled["dueAt"], rescheduled["cause"]) == (
        declared,
        resumed,
        "resumed",
    )
    # The deadline breaches at its new moment.
    run.feed("timer", {"timerId": timer["id"]}, at=_time(resumed))
    [breached] = run.events("process.sla_breached")
    assert breached["dueAt"] == resumed


def test_a_deadline_from_the_data_does_not_move_with_a_pause() -> None:
    run = HoursRun(_step('          due: {at: "data.deadline"}'))
    run.calendars = RU
    run.clock = ENTERED
    run.start(deadline="2026-10-09T07:00:00Z")
    run.clock = PAUSED
    run.command("suspend")
    frozen = run.timer("ask", "sla")
    assert (frozen["remaining"], frozen["remainingUnit"]) == (None, "wall")
    run.clock = RESUMED
    run.command("resume")
    assert run.timer("ask", "sla")["dueAt"] == "2026-10-09T07:00:00Z"
    assert run.activity("ask")["sla"]["dueAt"] == "2026-10-09T07:00:00Z"


def test_the_warning_is_counted_back_from_the_resumed_deadline() -> None:
    run = _run("{workhours: 16, warnBefore: {workhours: 4}}")
    # Thu 13:00: four working hours before Thu 17:00.
    assert run.timer("ask", "sla_warning")["dueAt"] == "2026-10-08T10:00:00Z"
    run.clock = PAUSED
    run.command("suspend")
    warning = run.timer("ask", "sla_warning")
    assert (warning["remaining"], warning["remainingUnit"]) == (1 * H, "working_seconds")
    run.clock = RESUMED
    run.command("resume")
    # Mon 14:00 is the deadline; four working hours before it, Mon 10:00.
    assert run.timer("ask", "sla")["dueAt"] == "2026-10-12T11:00:00Z"
    assert run.timer("ask", "sla_warning")["dueAt"] == "2026-10-12T07:00:00Z"
    record = run.activity("ask")["sla"]
    assert (record["dueAt"], record["warnAt"]) == ("2026-10-12T11:00:00Z", "2026-10-12T07:00:00Z")


PAUSED_BY_STEP = """
onEvent:
  - on: {observation: case.hold}
    do: [{id: pause, suspend: {reason: "'complaint'"}}]
  - on: {observation: case.release}
    do: [{id: unpause, resume: {}}]
due: {workhours: 16}
stages:
  - id: s
    steps:
      - id: ask
        human:
          taskType: review
          assign: [{role: lead}]
          due: {workdays: 2}
      - {id: done, complete: {outcome: ok}}
"""


def test_a_step_of_the_process_pauses_the_step_and_the_process_deadlines() -> None:
    run = HoursRun(PAUSED_BY_STEP)
    run.calendars = RU
    run.clock = ENTERED
    run.start()
    run.clock = PAUSED
    run.observe("case.hold", number="N-1")
    assert run.status == "suspended"
    for element, remaining in (("ask", 7 * H), ("process", 5 * H)):
        frozen = run.timer(element, "sla")
        assert (frozen["state"], frozen["remaining"], frozen["remainingUnit"]) == (
            "frozen",
            remaining,
            "working_seconds",
        )
    run.clock = RESUMED
    run.observe("case.release", number="N-1")
    assert run.status == "running"
    assert run.timer("ask", "sla")["dueAt"] == "2026-10-12T13:00:00Z"
    assert run.timer("process", "sla")["dueAt"] == "2026-10-12T11:00:00Z"
    assert run.state is not None
    assert run.state["sla"]["dueAt"] == "2026-10-12T11:00:00Z"


def test_the_deadline_of_a_step_the_suspension_does_not_stop_runs_on() -> None:
    run = HoursRun(
        """
onEvent:
  - on: {observation: case.ping}
    do:
      - id: ask
        human: {taskType: review, assign: [{role: lead}], due: {workhours: 16}}
stages:
  - id: s
    steps:
      - {id: hold, listen: {any: [{on: {observation: case.never}}]}}
"""
    )
    run.calendars = RU
    run.clock = ENTERED
    run.start()
    run.command("suspend")
    # The step of an event block runs on while the instance is suspended, and
    # so does its deadline (as its timers always did).
    run.observe("case.ping", number="N-1")
    assert run.timer("ask", "sla")["state"] == "pending"
    assert run.timer("ask", "sla")["dueAt"] == "2026-10-08T14:00:00Z"


def test_a_new_calendar_version_recounts_a_frozen_remainder() -> None:
    run = _run("{workhours: 16}")
    run.clock = PAUSED
    run.command("suspend")
    timer = run.timer("ask", "sla")
    assert timer["remaining"] == 5 * H
    # Wed 7 Oct becomes a day off: the 16h run Thu 09-18 and Fri 09-16, and
    # Thu 12-18 and Fri 09-16 are the 13h left.
    moved = copy.deepcopy(RU_SPEC)
    [year] = [y for y in moved["years"] if y["year"] == 2026]
    year["holidays"].append("2026-10-07")
    run.calendars = {"ru": Calendar.from_spec(moved)}
    run.feed("calendar", {"key": "ru"}, at=PAUSED + timedelta(hours=1))
    frozen = run.timer("ask", "sla")
    assert (frozen["state"], frozen["dueAt"]) == ("frozen", None)
    assert (frozen["remaining"], frozen["frozenFrom"]) == (13 * H, "2026-10-09T13:00:00Z")
    [decision] = run.decisions("timer_rescheduled")
    assert (decision["previousDueAt"], decision["cause"]) == (
        "2026-10-08T14:00:00Z",
        "calendar_changed",
    )
    assert _row(run, timer["id"])["remainingSeconds"] == 13 * H
    # No process.timer_rescheduled while frozen: the new moment comes on resume.
    assert not run.events("process.timer_rescheduled")
    # Another calendar recounts nothing.
    run.feed("calendar", {"key": "other"})
    assert run.timer("ask", "sla")["remaining"] == 13 * H

    run.clock = RESUMED
    run.command("resume")
    # From Sat 14:00: Mon 09-18 is 9h, Tue 09-13 the last 4h.
    assert run.timer("ask", "sla")["dueAt"] == "2026-10-13T10:00:00Z"
    [rescheduled] = run.events("process.timer_rescheduled")
    assert rescheduled["previousDueAt"] == "2026-10-09T13:00:00Z"


def test_a_frozen_deadline_whose_calendar_is_gone_fails_on_resume_and_the_instance_goes_on() -> (
    None
):
    run = _run("{workhours: 16, warnBefore: PT1H}")
    run.clock = PAUSED
    run.command("suspend")
    ids = {run.timer("ask", "sla")["id"], run.timer("ask", "sla_warning")["id"]}
    run.calendars = {}
    run.clock = RESUMED
    run.command("resume")
    assert run.status == "running"
    record = run.activity("ask")["sla"]
    assert (record["state"], record["error"]["type"]) == ("failed", "calendar_missing")
    assert ids <= {i["timerId"] for i in run.intents("cancel_timer")}
    [failed] = run.events("process.sla_failed")
    assert (failed["scope"], failed["element"]) == ("step", "ask")


# The data turn unreadable during a pause (the scenario of the P011 review): a
# pending deadline from the data would be ``timer_kept``, as its old moment
# still holds. A frozen one has nothing to keep: its remainder, if any, belongs
# to the old moment. So on resume both deadlines fail and the instance goes on.
SPOILED_DURING_PAUSE = """
onEvent:
  - on: {observation: case.hold}
    do: [{id: pause, suspend: {reason: "'complaint'"}}]
  - on: {observation: case.release}
    do: [{id: unpause, resume: {}}]
  - on: {observation: case.spoil}
    do: [{id: spoil, set: {deadline: event.payload.deadline}}]
due: {at: "data.deadline"}
stages:
  - id: s
    steps:
      - id: ask
        human:
          taskType: review
          assign: [{role: lead}]
          due: {at: "data.deadline"}
      - {id: done, complete: {outcome: ok}}
"""


@pytest.mark.parametrize("value", ["garbage", "2026-13-45T00:00:00Z"])
@pytest.mark.parametrize("pause", ["operator", "step"])
def test_a_deadline_from_data_spoiled_during_a_pause_fails_on_resume(
    pause: str, value: str
) -> None:
    run = HoursRun(SPOILED_DURING_PAUSE)
    run.calendars = RU
    run.clock = ENTERED
    run.start(deadline="2026-10-09T07:00:00Z")
    ids = {run.timer("ask", "sla")["id"], run.timer("process", "sla")["id"]}
    run.clock = PAUSED
    if pause == "operator":
        run.command("suspend")
    else:
        run.observe("case.hold", number="N-1")
    assert run.status == "suspended"
    run.observe("case.spoil", number="N-1", deadline=value)
    assert run.data["deadline"] == value
    assert not run.events("process.sla_failed")

    run.clock = RESUMED
    if pause == "operator":
        run.command("resume")
    else:
        run.observe("case.release", number="N-1")
    assert run.status == "running"
    failed = run.events("process.sla_failed")
    assert sorted((e["scope"], e["element"]) for e in failed) == [
        ("process", None),
        ("step", "ask"),
    ]
    # The error is the expression's (``compute``), not the calendar's.
    assert {e["error"]["type"] for e in failed} == {"expression_error"}
    assert run.state is not None
    assert run.activity("ask")["sla"]["state"] == "failed"
    assert run.state["sla"]["state"] == "failed"
    assert ids <= {i["timerId"] for i in run.intents("cancel_timer")}
    assert not [t for t in run.state["timers"].values() if t["kind"] in sla.SLA_TIMERS]
    assert not run.decisions("timer_kept")

    run.complete_task("ask", {})
    assert run.status == "completed"


def test_a_deadline_failed_on_resume_is_addressed_to_the_owner() -> None:
    """``process.sla_failed`` of the resume carries the owner's chain, as when set (P013)."""
    run = HoursRun("owner: [{role: lead}]\n" + SPOILED_DURING_PAUSE)
    run.calendars = RU
    run.clock = ENTERED
    run.start(deadline="2026-10-09T07:00:00Z")
    run.clock = PAUSED
    run.command("suspend")
    run.observe("case.spoil", number="N-1", deadline="garbage")
    run.clock = RESUMED
    run.command("resume")
    failed = [e for e in run.intents("emit_event") if e["type"] == "process.sla_failed"]
    assert len(failed) == 2
    for event in failed:
        assert event["addressees"] == {"owner": [{"role": "lead"}]}
        assert event["payload"]["owner"] is None


def test_a_warning_that_cannot_be_counted_on_resume_takes_its_deadline_with_it() -> None:
    # The deadline is wall-clock time and thaws; its warning needs the calendar,
    # gone by the resume. As when the deadline was set (P011), the failed
    # warning fails the whole deadline.
    run = _run("{duration: P2D, warnBefore: {workhours: 4}}")
    ids = {run.timer("ask", "sla")["id"], run.timer("ask", "sla_warning")["id"]}
    run.clock = PAUSED
    run.command("suspend")
    run.calendars = {}
    run.clock = RESUMED
    run.command("resume")
    assert run.status == "running"
    record = run.activity("ask")["sla"]
    assert (record["state"], record["error"]["type"]) == ("failed", "calendar_missing")
    assert (record["timer"], record["warnTimer"]) == (None, None)
    assert ids <= {i["timerId"] for i in run.intents("cancel_timer")}
    assert run.state is not None
    assert not [t for t in run.state["timers"].values() if t["kind"] in sla.SLA_TIMERS]
    [failed] = run.events("process.sla_failed")
    assert (failed["scope"], failed["element"]) == ("step", "ask")


@pytest.mark.parametrize(
    ("due", "calendars", "resumed", "pause"),
    [
        ("{workhours: 16}", RU, "2026-10-12T11:00:00Z", 15 * H),
        ("{workdays: 2}", RU, "2026-10-12T13:00:00Z", 15 * H),
        ("{workdays: 2}", WITHOUT_HOURS, "2026-10-12T07:00:00Z", 1 * sla.DAY),
        ("{duration: P2D}", RU, "2026-10-11T09:00:00Z", 50 * H),
    ],
    ids=["workhours", "workdays-with-hours", "workdays-without-hours", "duration"],
)
def test_a_resume_keeps_the_pause_a_recount_adds_back(
    due: str, calendars: dict[str, Calendar], resumed: str, pause: float
) -> None:
    """A recount from the base (here the same calendar again) keeps the resumed moment.

    The pause is kept in the unit of the deadline: Thu 12:00 to Sat 14:00 is 15
    working hours (Thu 12-18, Fri 09-18), one working day, 50 wall-clock hours.
    """
    run = _run(due, calendars)
    run.clock = PAUSED
    run.command("suspend")
    run.clock = RESUMED
    run.command("resume")
    timer = run.timer("ask", "sla")
    assert timer["dueAt"] == resumed
    [kept] = timer["paused"]
    assert kept["amount"] == pause
    run.feed("calendar", {"key": "ru"}, at=RESUMED + timedelta(hours=1))
    assert run.timer("ask", "sla")["dueAt"] == resumed
    assert not run.decisions("timer_rescheduled")
