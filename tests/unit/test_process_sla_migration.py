"""Deadlines counted again when an instance migrates (CP-ADR-0074 §11 amendment; P016).

After the migration's journal entry the engine takes the input ``migrated``:
the deadline of every open waiting step is counted by the new version from
its activity's ``openedAt``, the process's from the start (FR-024). A moved
timer is ``process.timer_rescheduled`` with ``cause: migrated``; a deadline
already past is one ``process.sla_breached`` with ``detectedBy: migration``;
escalation levels already past are skipped, future ones set; the step's task
gets the new due (``update_task_due``). Revision 1 has nothing to recount
(FR-031).

The calendar is ``ru`` with working hours 09:00-18:00 (Moscow, UTC+3); times
in comments are Moscow wall-clock times.
"""

import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from control_plane.domain import process_definition as pd
from control_plane.domain import process_engine as pe
from control_plane.domain import process_sla
from control_plane.domain.calendar import Calendar
from control_plane.domain.process_engine import Definition, Input
from control_plane.domain.process_migration import (
    MIGRATION_INPUT,
    RECOUNT_INPUT,
    migrate_state,
    recount_body,
)
from control_plane.domain.process_replay import replay
from control_plane.domain.process_steps import exit_deadline
from tests.unit.test_process_engine import CATALOG, spec
from tests.unit.test_process_sla import HOURS_SPEC, WITH_HOURS, HoursRun

# Thursday 5 March 2026, 13:30: the moment of the migration.
MIGRATED_AT = datetime(2026, 3, 5, 10, 30, tzinfo=UTC)
# Openings 0.5, 1.5 and 3 working days (4.5, 13.5 and 27 working hours) before it:
# Thursday 09:00, Wednesday 09:00, Monday 13:30.
HALF_DAY = datetime(2026, 3, 5, 6, 0, tzinfo=UTC)
DAY_AND_HALF = datetime(2026, 3, 4, 6, 0, tzinfo=UTC)
THREE_DAYS = datetime(2026, 3, 2, 10, 30, tzinfo=UTC)


def _review(due: str, *, head: str = "") -> str:
    return f"""{head}
stages:
  - id: s
    steps:
      - id: ask
        human:
          taskType: review
          assign: [{{role: lead}}]
          due: {due}
          escalations:
            - {{after: due, action: notify, to: [{{role: boss}}]}}
            - {{after: PT2H, action: notify, to: [{{role: boss}}]}}
      - {{id: done, complete: {{outcome: ok}}}}
"""


MIGRATE_1_TO_2 = "migrations: [{from: 1, to: 2, policy: migrate, map: {}}]"
V1 = _review("{workdays: 5, warnBefore: {workhours: 4}}")
V2 = _review("{workdays: 1, warnBefore: {workhours: 4}}", head=MIGRATE_1_TO_2)


def _definition(body: str, version: int, revision: int = pe.ENGINE_REVISION) -> Definition:
    body_spec = pd.normalized_spec({**spec(body), "version": version})
    built = Definition.build("test", body_spec, CATALOG)
    return replace(built, engine_revision=revision)


def _time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class MigratingRun(HoursRun):
    """A run that keeps its journal, the migration's entry included."""

    def __init__(self, body: str, *, revision: int = pe.ENGINE_REVISION) -> None:
        super().__init__(body)
        self.definition = replace(self.definition, engine_revision=revision)
        self.journal: list[dict[str, Any]] = []

    def feed(self, kind: str, body: dict[str, Any] | None = None, **kw: Any) -> Any:
        decisions, intents = super().feed(kind, body, **kw)
        assert self.state is not None
        given = self.records[-1][0]
        self.journal.append(
            {
                "seq": self.state["seq"],
                "input": given.out(),
                "decisions": [d.out() for d in decisions],
                "intents": [i.out() for i in intents],
                "calendars": {"ru": 1},
            }
        )
        return decisions, intents

    def migrate(self, body: str, *, at: datetime = MIGRATED_AT, version: int = 2) -> Any:
        """Move to ``body`` as the apply does: the entry ``migrate``, then ``migrated``."""
        assert self.state is not None
        new = _definition(body, version)
        moved = migrate_state(self.definition, new, self.state, {})
        moved["seq"] = self.state["seq"] + 1
        self.journal.append(
            {
                "seq": moved["seq"],
                "input": Input(MIGRATION_INPUT, at, {"state": moved}, None).out(),
                "decisions": [{"kind": "migrated", "element": None}],
                "intents": [],
                "calendars": {},
            }
        )
        old_version = self.definition.version
        self.definition = new
        self.state = copy.deepcopy(moved)
        self.records.clear()
        return self.feed(RECOUNT_INPUT, recount_body(from_version=old_version, target=new), at=at)


def _opened(at: datetime, body: str = V1, **kw: Any) -> MigratingRun:
    run = MigratingRun(body, **kw)
    run.clock = at
    run.start()
    return run


# --- SC-007 --------------------------------------------------------------------------------


def test_three_open_steps_are_counted_again_by_the_new_version() -> None:
    """SC-007: 0.5, 1.5 and 3 working days in, ``workdays: 1`` — one in time, two breached."""
    runs = [_opened(at) for at in (HALF_DAY, DAY_AND_HALF, THREE_DAYS)]
    for run in runs:
        run.migrate(V2)
    in_time, late, later = runs

    # 0.5 days in: due Friday 09:00, the warning 4 working hours before it,
    # the escalations at the due and two hours after it.
    record = in_time.activity("ask")["sla"]
    assert (record["state"], record["dueAt"], record["warnAt"]) == (
        "pending",
        "2026-03-06T06:00:00Z",
        "2026-03-05T11:00:00Z",
    )
    moved = in_time.events("process.timer_rescheduled")
    assert {e["cause"] for e in moved} == {"migrated"}
    assert sorted(e["dueAt"] for e in moved) == [
        "2026-03-05T11:00:00Z",
        "2026-03-06T06:00:00Z",
        "2026-03-06T06:00:00Z",
        "2026-03-06T08:00:00Z",
    ]
    assert in_time.timer("ask", "sla")["dueAt"] == "2026-03-06T06:00:00Z"
    assert not in_time.events("process.sla_breached")
    [update] = in_time.intents("update_task_due")
    assert update == {
        "kind": "update_task_due",
        "activityId": in_time.activity("ask")["id"],
        "element": "ask",
        "due": "2026-03-06T06:00:00Z",
    }
    assert in_time.activity("ask")["due"] == "2026-03-06T06:00:00Z"

    # 1.5 and 3 days in: the due is past — one breach each, found by the migration.
    for run, due, overdue in (
        (late, "2026-03-05T06:00:00Z", 4.5 * 3600),
        (later, "2026-03-03T10:30:00Z", 48 * 3600),
    ):
        [breached] = run.events("process.sla_breached")
        assert breached["detectedBy"] == "migration"
        assert (breached["dueAt"], breached["detectedAt"]) == (due, _time(MIGRATED_AT))
        assert breached["overdueSeconds"] == overdue
        assert (breached["scope"], breached["element"]) == ("step", "ask")
        assert breached["activityId"] == run.activity("ask")["id"]
        assert run.activity("ask")["sla"]["state"] == "breached"
        assert run.activity("ask")["sla"]["dueAt"] == due
        # No timer of the deadline and none of the past escalation levels is left.
        assert run.state is not None and not run.state["timers"]
        skipped = run.decisions("escalation_skipped")
        assert [(d["level"], d["reason"]) for d in skipped] == [(1, "migrated"), (2, "migrated")]
        assert not run.events("process.escalated")
        assert not run.events("process.sla_warning")
        [update] = run.intents("update_task_due")
        assert update["due"] == due
        assert run.status == "running"

    # Two breaches over the three instances, no escalation fired.
    assert sum(len(r.events("process.sla_breached")) for r in runs) == 2
    assert not any(r.decisions("escalated") for r in runs)


def test_the_migrated_step_records_what_it_changed() -> None:
    run = _opened(DAY_AND_HALF)
    previous = run.activity("ask")["sla"]["dueAt"]
    run.migrate(V2)
    [changed] = run.decisions("deadline_migrated")
    assert changed == {
        "kind": "deadline_migrated",
        "element": "ask",
        "scope": "step",
        "activity": run.activity("ask")["id"],
        "previousDueAt": previous,
        "dueAt": "2026-03-05T06:00:00Z",
        "breached": True,
    }
    [breach] = run.decisions("sla_breached")
    assert breach["detectedBy"] == "migration"


def test_a_future_escalation_level_is_moved_and_fires_once_later() -> None:
    run = _opened(HALF_DAY)
    run.migrate(V2)
    assert run.state is not None
    levels = sorted(
        (t["level"], t["dueAt"]) for t in run.state["timers"].values() if t["kind"] == "escalation"
    )
    assert levels == [(1, "2026-03-06T06:00:00Z"), (2, "2026-03-06T08:00:00Z")]
    run.fire("ask", "sla")
    [breached] = run.events("process.sla_breached")
    assert breached["detectedBy"] == "timer"


def test_a_replay_after_the_migration_has_no_discrepancy() -> None:
    for at in (HALF_DAY, DAY_AND_HALF, THREE_DAYS):
        run = _opened(at)
        run.migrate(V2)
        if at == HALF_DAY:
            run.fire("ask", "sla_warning")
        run.complete_task("ask", {"decision": "yes"}, at=MIGRATED_AT + timedelta(days=2))
        result = replay(run.definition, run.journal, lambda named: WITH_HOURS)
        assert result.discrepancies == []
        assert result.state == run.state
        # The replay starts after the migration: the input migrated is its first step.
        assert result.steps == len(run.records)


# --- what else the step recounts ---------------------------------------------------------


def test_a_deadline_already_breached_is_not_reported_again() -> None:
    run = _opened(THREE_DAYS, _review("{workdays: 2}"))
    run.fire("ask", "sla")
    assert len(run.events("process.sla_breached")) == 1
    run.migrate(V2)
    assert not run.events("process.sla_breached")  # the records since the migration
    assert run.activity("ask")["sla"]["state"] == "breached"
    assert run.activity("ask")["sla"]["dueAt"] == "2026-03-03T10:30:00Z"


def test_a_warning_threshold_already_past_is_not_reported_afterwards() -> None:
    # Due Friday 09:00, the warning 8 working hours before it: Thursday 10:00, past.
    run = _opened(HALF_DAY)
    run.migrate(_review("{workdays: 1, warnBefore: {workhours: 8}}", head=MIGRATE_1_TO_2))
    record = run.activity("ask")["sla"]
    assert (record["state"], record["warnAt"], record["warnTimer"]) == (
        "warning",
        "2026-03-05T07:00:00Z",
        None,
    )
    assert not run.events("process.sla_warning")
    run.fire("ask", "sla")
    assert len(run.events("process.sla_breached")) == 1


def test_a_due_the_new_version_drops_takes_its_timers_along() -> None:
    run = _opened(HALF_DAY)
    run.migrate(
        """
migrations: [{from: 1, to: 2, policy: migrate, map: {}}]
stages:
  - id: s
    steps:
      - {id: ask, human: {taskType: review, assign: [{role: lead}]}}
      - {id: done, complete: {outcome: ok}}
"""
    )
    assert run.state is not None and not run.state["timers"]
    assert "sla" not in run.activity("ask")
    assert len(run.intents("cancel_timer")) == 4
    [update] = run.intents("update_task_due")
    assert update["due"] is None
    [changed] = run.decisions("deadline_migrated")
    assert changed["dueAt"] is None


def test_a_due_from_an_expression_the_new_version_drops_is_counted_by_its_new_form() -> None:
    """The migration does not refuse an expression that only counted a due."""
    old = _review('{at: "cal.addWorkdays(data.deadline, -3)"}')
    run = _opened(HALF_DAY, old)
    run.migrate(V2)
    assert run.activity("ask")["sla"]["dueAt"] == "2026-03-06T06:00:00Z"
    assert run.timer("ask", "sla")["recipe"]["kind"] == "workdays"


def test_a_step_without_a_due_before_gets_one_from_its_opening() -> None:
    run = _opened(
        HALF_DAY,
        """
stages:
  - id: s
    steps:
      - {id: ask, human: {taskType: review, assign: [{role: lead}]}}
      - {id: done, complete: {outcome: ok}}
""",
    )
    run.migrate(V2)
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-06T06:00:00Z"
    assert [d["timerKind"] for d in run.decisions("timer_set")] == [
        "sla",
        "sla_warning",
        "escalation",
        "escalation",
    ]
    [changed] = run.decisions("deadline_migrated")
    assert (changed["previousDueAt"], changed["dueAt"]) == (None, "2026-03-06T06:00:00Z")


def test_a_version_of_revision_1_moves_to_deadlines_by_migration() -> None:
    """FR-031: revision 1 has no deadlines; the migration to revision 2 counts them."""
    run = _opened(HALF_DAY, V1, revision=1)
    assert "sla" not in run.activity("ask")
    task_due = run.activity("ask")["due"]
    run.migrate(V2)
    record = run.activity("ask")["sla"]
    assert record["dueAt"] == "2026-03-06T06:00:00Z"
    [changed] = run.decisions("deadline_migrated")
    assert changed["previousDueAt"] == task_due


def test_a_target_of_revision_1_recounts_nothing() -> None:
    run = _opened(HALF_DAY, V1, revision=1)
    assert run.state is not None
    before = copy.deepcopy(run.state)
    old = _definition(V2, 2, revision=1)
    state = migrate_state(run.definition, old, run.state, {})
    state["seq"] = before["seq"] + 1
    after, decisions, intents = pe.step(
        old, state, Input(RECOUNT_INPUT, MIGRATED_AT, {}, None, WITH_HOURS)
    )
    assert [d.out() for d in decisions] == [
        {"kind": "ignored", "element": None, "reason": "no_deadlines", "input": "migrated"}
    ]
    assert intents == []
    assert after["timers"] == before["timers"]


def test_the_process_deadline_counts_from_the_start() -> None:
    body = """
due: {workdays: 3}
stages:
  - id: s
    steps:
      - {id: hold, listen: {any: [{on: {observation: case.go}}]}}
      - {id: done, complete: {outcome: ok}}
"""
    run = _opened(THREE_DAYS, body)
    assert run.state is not None
    assert run.state["sla"]["dueAt"] == "2026-03-05T10:30:00Z"
    later = MIGRATED_AT - timedelta(hours=1)
    run.migrate(MIGRATE_1_TO_2 + body.replace("{workdays: 3}", "{workdays: 4}"), at=later)
    assert run.state["sla"]["dueAt"] == "2026-03-06T10:30:00Z"
    [moved] = run.events("process.timer_rescheduled")
    assert (moved["element"], moved["cause"]) == ("process", "migrated")
    assert moved["previousDueAt"] == "2026-03-05T10:30:00Z"

    run = _opened(THREE_DAYS, body)
    run.migrate(MIGRATE_1_TO_2 + body.replace("{workdays: 3}", "{workdays: 2}"))
    [breached] = run.events("process.sla_breached")
    assert (breached["scope"], breached["element"], breached["detectedBy"]) == (
        "process",
        None,
        "migration",
    )
    assert run.state is not None and run.state["sla"]["state"] == "breached"


def test_a_frozen_deadline_keeps_what_is_left_of_its_new_moment() -> None:
    """Suspended: the deadline moves while frozen, its clock stopped at the suspension."""
    run = _opened(HALF_DAY)
    paused = HALF_DAY + timedelta(hours=1)  # Thursday 10:00
    run.clock = paused
    run.command("suspend", reason="hold")
    run.migrate(V2)
    timer = run.timer("ask", "sla")
    assert timer["state"] == "frozen"
    assert timer["frozenFrom"] == "2026-03-06T06:00:00Z"
    # From Thursday 10:00 to Friday 09:00: 8 working hours are left.
    assert (timer["remaining"], timer["remainingUnit"]) == (8 * 3600.0, "working_seconds")
    assert not run.events("process.timer_rescheduled")  # it comes on resume
    [moved] = [d for d in run.decisions("timer_rescheduled") if d["timerId"] == timer["id"]]
    assert (moved["cause"], moved["dueAt"]) == ("migrated", None)
    assert not run.events("process.sla_breached")


# Revision 1 freezes a timer without ``frozenAt`` (P018): its moment is made out from what it keeps.
DAY_REVIEW = _review("PT24H")
THREE_HOURS_REVIEW = _review("PT3H", head=MIGRATE_1_TO_2)
SUSPENDED_AT = HALF_DAY + timedelta(hours=1)
LATER_MIGRATED = HALF_DAY + timedelta(hours=5)


def _kinds(run: MigratingRun, kind: str) -> dict[Any, dict[str, Any]]:
    assert run.state is not None
    return {t.get("level"): t for t in run.state["timers"].values() if t["kind"] == kind}


def _suspended_under_revision_1(body: str, at: datetime = SUSPENDED_AT) -> MigratingRun:
    run = _opened(HALF_DAY, body, revision=1)
    run.clock = at
    run.command("suspend", reason="hold")
    assert all("frozenAt" not in t for t in _kinds(run, "escalation").values())
    return run


def test_an_escalation_frozen_under_revision_1_keeps_its_freeze_on_migration() -> None:
    """Frozen at +1h, migrated at +5h, ``due: PT3H``: the level waits 2h, not skipped.

    The new moment (+3h) lies between the freeze and the migration: the
    clock stands at the freeze, so what is left of it is kept (CP-ADR-0078 §4).
    """
    run = _suspended_under_revision_1(DAY_REVIEW)
    run.migrate(THREE_HOURS_REVIEW, at=LATER_MIGRATED)
    assert not [d for d in run.decisions("escalation_skipped")]
    levels = _kinds(run, "escalation")
    assert {level: t["state"] for level, t in levels.items()} == {1: "frozen", 2: "frozen"}
    assert (levels[1]["remaining"], levels[1]["remainingUnit"]) == (7200.0, "wall")
    assert levels[1]["frozenFrom"] == _time(HALF_DAY + timedelta(hours=3))
    assert levels[2]["remaining"] == 4 * 3600.0
    # The moment made out stays with the timer: a second migration counts from it too.
    assert {t["frozenAt"] for t in levels.values()} == {_time(SUSPENDED_AT)}

    run.clock = LATER_MIGRATED + timedelta(hours=1)
    run.command("resume")
    assert _kinds(run, "escalation")[1]["dueAt"] == _time(run.clock + timedelta(hours=2))


def test_an_on_due_frozen_under_revision_1_keeps_its_freeze_on_migration() -> None:
    """``onDue`` of an approval is kept like an escalation level."""
    body = """
stages:
  - id: s
    steps:
      - id: sign
        approve:
          approvers: [{role: finance}]
          quorum: any
          due: %s
          onDue: reject
      - {id: done, complete: {outcome: ok}}
"""
    run = _suspended_under_revision_1(body % "PT24H")
    run.migrate(MIGRATE_1_TO_2 + body % "PT3H", at=LATER_MIGRATED)
    assert not run.decisions("escalation_skipped")
    [on_due] = _kinds(run, "due").values()
    assert (on_due["state"], on_due["onDue"], on_due["remaining"]) == ("frozen", "reject", 7200.0)


def test_a_level_past_at_the_freeze_under_revision_1_is_skipped() -> None:
    """A new moment before the freeze is past: the level is skipped as ``migrated``."""
    run = _suspended_under_revision_1(DAY_REVIEW, at=HALF_DAY + timedelta(hours=4))
    run.migrate(THREE_HOURS_REVIEW, at=LATER_MIGRATED)
    [skipped] = run.decisions("escalation_skipped")
    assert (skipped["level"], skipped["reason"]) == (1, "migrated")
    assert _kinds(run, "escalation")[2]["remaining"] == 3600.0


def test_a_timer_due_at_a_freeze_under_revision_1_is_taken_frozen_at_its_moment() -> None:
    """Due at the freeze, it keeps ``0``: the freeze comes out as its moment, the earliest.

    Frozen at +26h with the level of +24h not yet fired; a new moment at +25h
    keeps an hour it did not have (CP-ADR-0078 §4, P018).
    """
    run = _suspended_under_revision_1(DAY_REVIEW, at=HALF_DAY + timedelta(hours=26))
    assert _kinds(run, "escalation")[1]["remaining"] == 0.0
    run.migrate(_review("PT25H", head=MIGRATE_1_TO_2), at=HALF_DAY + timedelta(hours=27))
    level = _kinds(run, "escalation")[1]
    assert (level["state"], level["remaining"]) == ("frozen", 3600.0)
    assert level["frozenAt"] == _time(HALF_DAY + timedelta(hours=24))


# --- past pauses (FR-016) ----------------------------------------------------------------

# Five working days from Monday 2 March 09:00, paused at 10:00 until Tuesday
# 10 March 09:00: due Monday 16 March 17:00 (53 working hours of pause added).
FIVE_DAYS = _review("{workdays: 5, warnBefore: {workhours: 4}}")
ENTERED = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)
RESUMED = datetime(2026, 3, 10, 6, 0, tzinfo=UTC)
PAUSED_DUE = "2026-03-16T14:00:00Z"


def _paused_and_resumed(body: str = FIVE_DAYS) -> MigratingRun:
    run = _opened(ENTERED, body)
    run.clock = ENTERED + timedelta(hours=1)
    run.command("suspend", reason="hold")
    run.clock = RESUMED
    run.command("resume")
    assert run.timer("ask", "sla")["dueAt"] == PAUSED_DUE
    return run


def _levels(run: MigratingRun) -> list[tuple[int, str]]:
    assert run.state is not None
    return sorted(
        (t["level"], t["dueAt"]) for t in run.state["timers"].values() if t["kind"] == "escalation"
    )


def test_a_migration_to_the_same_due_keeps_a_past_pause() -> None:
    """The deadline counted again from the opening adds back the pause it stood still."""
    run = _paused_and_resumed()
    warn_at = run.timer("ask", "sla_warning")["dueAt"]
    levels = _levels(run)
    run.migrate(
        _review("{workdays: 5, warnBefore: {workhours: 4}}", head=MIGRATE_1_TO_2),
        at=RESUMED + timedelta(days=1),
    )

    record = run.activity("ask")["sla"]
    assert (record["state"], record["dueAt"], record["warnAt"]) == ("pending", PAUSED_DUE, warn_at)
    assert run.timer("ask", "sla")["dueAt"] == PAUSED_DUE
    assert not run.events("process.sla_breached")
    assert not run.events("process.timer_rescheduled")
    assert not run.decisions("deadline_migrated")
    # The escalation levels moved by the resume are neither skipped nor moved.
    assert not run.decisions("escalation_skipped")
    assert _levels(run) == levels
    # The task catches up with the deadline the resume moved.
    [update] = run.intents("update_task_due")
    assert update["due"] == PAUSED_DUE

    run.complete_task("ask", {"decision": "yes"}, at=RESUMED + timedelta(days=2))
    result = replay(run.definition, run.journal, lambda named: WITH_HOURS)
    assert result.discrepancies == []
    assert result.state == run.state


def test_a_migration_to_another_due_counts_it_with_the_past_pause() -> None:
    run = _paused_and_resumed()
    levels = _levels(run)
    run.migrate(_review("{workdays: 6}", head=MIGRATE_1_TO_2), at=RESUMED + timedelta(days=1))
    # One working day more than the paused deadline, not six from the opening.
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-17T14:00:00Z"
    assert not run.events("process.sla_breached")
    [moved] = [
        e for e in run.events("process.timer_rescheduled") if e["previousDueAt"] == PAUSED_DUE
    ]
    assert (moved["dueAt"], moved["cause"]) == ("2026-03-17T14:00:00Z", "migrated")
    assert not run.decisions("escalation_skipped")
    # Each level is its due and the wall-clock pause of its own timer on from it.
    assert [at for _, at in _levels(run)] == [
        "2026-03-18T05:00:00Z",
        "2026-03-18T07:00:00Z",
    ]
    assert [at for _, at in levels] == ["2026-03-17T05:00:00Z", "2026-03-17T07:00:00Z"]


def test_pauses_add_up() -> None:
    run = _paused_and_resumed()
    # Paused again Wednesday 11 March 09:00 till Thursday 12 March 09:00: one
    # working day more.
    run.clock = RESUMED + timedelta(days=1)
    run.command("suspend")
    run.clock = RESUMED + timedelta(days=2)
    run.command("resume")
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-17T14:00:00Z"
    run.migrate(
        _review("{workdays: 5}", head=MIGRATE_1_TO_2),
        at=RESUMED + timedelta(days=3),
    )
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-17T14:00:00Z"
    assert not run.events("process.sla_breached")


def test_a_new_calendar_version_keeps_a_past_pause() -> None:
    run = _paused_and_resumed()
    # The same calendar again moves nothing.
    run.feed("calendar", {"key": "ru"}, at=RESUMED + timedelta(hours=1))
    assert not run.decisions("timer_rescheduled")
    assert run.timer("ask", "sla")["dueAt"] == PAUSED_DUE

    # Thursday 12 March becomes a holiday: the paused deadline is a working day later.
    moved = copy.deepcopy(HOURS_SPEC)
    moved["years"][0]["holidays"].append("2026-03-12")
    run.calendars = {"ru": Calendar.from_spec(moved)}
    run.feed("calendar", {"key": "ru"})
    assert run.timer("ask", "sla")["dueAt"] == "2026-03-17T14:00:00Z"
    assert run.activity("ask")["sla"]["state"] == "pending"
    # The levels count from the due by the new version and keep their own pauses.
    assert [at for _, at in _levels(run)] == ["2026-03-17T05:00:00Z", "2026-03-17T07:00:00Z"]


def test_a_new_calendar_version_keeps_a_past_pause_of_a_frozen_deadline() -> None:
    run = _paused_and_resumed()
    # Suspended again Wednesday 11 March 09:00: 35 working hours are left.
    run.clock = RESUMED + timedelta(days=1)
    run.command("suspend")
    assert run.timer("ask", "sla")["remaining"] == 35 * 3600.0
    moved = copy.deepcopy(HOURS_SPEC)
    moved["years"][0]["holidays"].append("2026-03-12")
    run.calendars = {"ru": Calendar.from_spec(moved)}
    run.feed("calendar", {"key": "ru"})
    frozen = run.timer("ask", "sla")
    # The holiday falls inside what is left: the remainder stays, the moment moves.
    assert (frozen["remaining"], frozen["frozenFrom"]) == (35 * 3600.0, "2026-03-17T14:00:00Z")
    run.clock = RESUMED + timedelta(days=2)
    run.command("resume")
    assert not run.events("process.sla_breached")


# --- a deadline already past at the suspension (SC-003, FR-015) ------------------------

# One working hour from Thursday 09:00: due 10:00. The timer is not taken by
# 10:30, when the instance is suspended; resumed at 14:00.
ONE_HOUR = _review("{workhours: 1}")
LATE_DUE = "2026-03-05T07:00:00Z"
LATE_PAUSED = datetime(2026, 3, 5, 7, 30, tzinfo=UTC)
LATE_RESUMED = datetime(2026, 3, 5, 11, 0, tzinfo=UTC)
# Detected at 15:00: half an hour before the suspension and an hour after it.
LATE_DETECTED = datetime(2026, 3, 5, 12, 0, tzinfo=UTC)
LATE_OVERDUE = 5400


def _suspended_past_due(*, resume: bool = True) -> MigratingRun:
    run = _opened(HALF_DAY, ONE_HOUR)
    assert run.timer("ask", "sla")["dueAt"] == LATE_DUE
    run.clock = LATE_PAUSED
    run.command("suspend")
    if resume:
        run.clock = LATE_RESUMED
        run.command("resume")
    return run


def test_a_deadline_past_at_the_suspension_keeps_its_moment_on_resume() -> None:
    run = _suspended_past_due()
    assert run.timer("ask", "sla")["dueAt"] == LATE_DUE
    record = run.activity("ask")["sla"]
    assert record["dueAt"] == LATE_DUE
    assert record["overdueStops"] == [{"from": _time(LATE_PAUSED), "to": _time(LATE_RESUMED)}]
    # The level due with the deadline keeps it too; the later one is moved by the pause.
    assert [at for _, at in _levels(run)] == [LATE_DUE, "2026-03-05T12:30:00Z"]

    run.clock = LATE_DETECTED
    run.fire("ask", "sla")
    [breached] = run.events("process.sla_breached")
    assert (breached["dueAt"], breached["detectedBy"]) == (LATE_DUE, "timer")
    assert breached["overdueSeconds"] == LATE_OVERDUE
    # The step closing later is overdue by the same measure.
    closed_at = LATE_DETECTED + timedelta(hours=1)
    exit_ = exit_deadline(run.activity("ask"), closed_at)
    assert (exit_["breached"], exit_["overdueSeconds"]) == (True, LATE_OVERDUE + 3600)

    result = replay(run.definition, run.journal, lambda named: WITH_HOURS)
    assert result.discrepancies == []
    assert result.state == run.state


def test_a_migration_after_the_resume_reports_the_same_breach() -> None:
    run = _suspended_past_due()
    run.migrate(_review("{workhours: 1}", head=MIGRATE_1_TO_2), at=LATE_DETECTED)
    [breached] = run.events("process.sla_breached")
    assert (breached["dueAt"], breached["detectedBy"]) == (LATE_DUE, "migration")
    assert breached["overdueSeconds"] == LATE_OVERDUE
    record = run.activity("ask")["sla"]
    assert (record["state"], record["dueAt"]) == ("breached", LATE_DUE)
    assert record["overdueStops"] == [{"from": _time(LATE_PAUSED), "to": _time(LATE_RESUMED)}]

    result = replay(run.definition, run.journal, lambda named: WITH_HOURS)
    assert result.discrepancies == []
    assert result.state == run.state


def test_a_migration_during_the_suspension_counts_the_breach_to_the_stop() -> None:
    run = _suspended_past_due(resume=False)
    run.migrate(_review("{workhours: 1}", head=MIGRATE_1_TO_2), at=LATE_RESUMED)
    [breached] = run.events("process.sla_breached")
    assert (breached["dueAt"], breached["detectedBy"]) == (LATE_DUE, "migration")
    # From the due to the suspension: the clock has stood still since.
    assert breached["overdueSeconds"] == 1800


# --- the projection agrees with the event (CP-ADR-0078 §6) -----------------------------


def _projected(run: MigratingRun, at: datetime) -> tuple[str, int | None]:
    """``(slaState, overdueSeconds)`` of the step's deadline as the projection shows it."""
    assert run.state is not None
    _, state, overdue = process_sla.shown(
        run.activity("ask")["sla"], now=at, timers=run.state["timers"]
    )
    return state, overdue


def test_the_projection_leaves_out_a_past_suspension_as_the_event_does() -> None:
    run = _suspended_past_due()
    # Before the worker takes the timer, the projection already shows the breach.
    assert _projected(run, LATE_DETECTED) == ("breached", LATE_OVERDUE)
    run.clock = LATE_DETECTED
    run.fire("ask", "sla")
    [breached] = run.events("process.sla_breached")
    assert breached["overdueSeconds"] == LATE_OVERDUE
    assert _projected(run, LATE_DETECTED) == ("breached", LATE_OVERDUE)
    # Later the projection and the step's exit go on by the same measure.
    later = LATE_DETECTED + timedelta(hours=1)
    assert _projected(run, later) == ("breached", LATE_OVERDUE + 3600)
    assert exit_deadline(run.activity("ask"), later)["overdueSeconds"] == LATE_OVERDUE + 3600


def test_the_projection_leaves_out_two_suspensions_as_the_event_does() -> None:
    run = _suspended_past_due()
    # Suspended again 15:00-16:00: one more hour the overdue clock stands still.
    run.clock = LATE_DETECTED
    run.command("suspend")
    run.clock = LATE_DETECTED + timedelta(hours=1)
    run.command("resume")
    assert len(run.activity("ask")["sla"]["overdueStops"]) == 2
    detected = LATE_DETECTED + timedelta(hours=2)
    # From 10:00 to 17:00 less 3.5 and 1 hours of suspension.
    overdue = 7 * 3600 - 12600 - 3600
    assert _projected(run, detected) == ("breached", overdue)
    run.clock = detected
    run.fire("ask", "sla")
    [breached] = run.events("process.sla_breached")
    assert (breached["dueAt"], breached["overdueSeconds"]) == (LATE_DUE, overdue)
    assert _projected(run, detected) == ("breached", overdue)


def test_the_projection_of_a_suspended_past_deadline_stands_at_the_stop() -> None:
    run = _suspended_past_due(resume=False)
    # The clock stood still at 10:30: half an hour past, however long the suspension.
    assert _projected(run, LATE_RESUMED) == ("breached", 1800)
    assert _projected(run, LATE_RESUMED + timedelta(days=1)) == ("breached", 1800)
    # The migration during the suspension reports the same.
    run.migrate(_review("{workhours: 1}", head=MIGRATE_1_TO_2), at=LATE_RESUMED)
    [breached] = run.events("process.sla_breached")
    assert breached["overdueSeconds"] == 1800
    assert _projected(run, LATE_RESUMED) == ("breached", 1800)
    # The suspension stays open with the breached record; the resume closes it, and
    # the step is overdue as if the timer had fired after the resume.
    run.clock = LATE_RESUMED
    run.command("resume")
    record = run.activity("ask")["sla"]
    assert record["overdueStops"] == [{"from": _time(LATE_PAUSED), "to": _time(LATE_RESUMED)}]
    assert _projected(run, LATE_DETECTED) == ("breached", LATE_OVERDUE)
    assert exit_deadline(run.activity("ask"), LATE_DETECTED)["overdueSeconds"] == LATE_OVERDUE

    result = replay(run.definition, run.journal, lambda named: WITH_HOURS)
    assert result.discrepancies == []
    assert result.state == run.state


def test_the_projection_of_a_second_suspension_stands_at_its_stop() -> None:
    run = _suspended_past_due()
    # Suspended again at 15:00, after the first suspension of 3.5 hours.
    run.clock = LATE_DETECTED
    run.command("suspend")
    projected = _projected(run, LATE_DETECTED + timedelta(hours=5))
    assert projected == ("breached", LATE_OVERDUE)


# --- the overdue clock stands whether the worker was on time or late (FR-021) ----------

ON_TIME = datetime(2026, 3, 5, 7, 0, 5, tzinfo=UTC)
PROCESS_DUE = _review("{workhours: 8}", head="due: {workhours: 1}")


def _fired_on_time_then_suspended() -> MigratingRun:
    run = _opened(HALF_DAY, ONE_HOUR)
    run.clock = ON_TIME
    run.fire("ask", "sla")
    [breached] = run.events("process.sla_breached")
    assert breached["overdueSeconds"] == 5
    run.clock = LATE_PAUSED
    run.command("suspend")
    return run


def test_a_breach_fired_before_the_suspension_stands_like_a_late_one() -> None:
    on_time = _fired_on_time_then_suspended()
    late = _suspended_past_due(resume=False)
    during = LATE_RESUMED - timedelta(minutes=1)
    assert _projected(on_time, during) == _projected(late, during) == ("breached", 1800)
    for run in (on_time, late):
        run.clock = LATE_RESUMED
        run.command("resume")
        record = run.activity("ask")["sla"]
        assert record["overdueStops"] == [{"from": _time(LATE_PAUSED), "to": _time(LATE_RESUMED)}]
        assert _projected(run, LATE_DETECTED) == ("breached", LATE_OVERDUE)
        assert exit_deadline(run.activity("ask"), LATE_DETECTED)["overdueSeconds"] == LATE_OVERDUE
        result = replay(run.definition, run.journal, lambda named: WITH_HOURS)
        assert result.discrepancies == []
        assert result.state == run.state


def test_the_process_deadline_stands_like_a_step_deadline() -> None:
    for fired in (True, False):
        run = _opened(HALF_DAY, PROCESS_DUE)
        assert run.state is not None
        [timer] = [t for t in run.state["timers"].values() if t.get("sla") == "process"]
        assert timer["dueAt"] == LATE_DUE
        if fired:
            run.clock = ON_TIME
            run.feed("timer", {"timerId": timer["id"]})
        run.clock = LATE_PAUSED
        run.command("suspend")
        during = process_sla.shown(
            run.state["sla"], now=LATE_RESUMED - timedelta(minutes=1), timers=run.state["timers"]
        )
        assert during[1:] == ("breached", 1800)
        run.clock = LATE_RESUMED
        run.command("resume")
        if not fired:
            run.clock = LATE_DETECTED
            run.feed("timer", {"timerId": timer["id"]})
            [breached] = run.events("process.sla_breached")
            assert breached["overdueSeconds"] == LATE_OVERDUE
        record = run.state["sla"]
        assert record["overdueStops"] == [{"from": _time(LATE_PAUSED), "to": _time(LATE_RESUMED)}]
        shown = process_sla.shown(record, now=LATE_DETECTED, timers=run.state["timers"])
        assert shown[1:] == ("breached", LATE_OVERDUE)


def test_a_step_leaving_during_the_suspension_is_overdue_as_projected() -> None:
    cancelled_at = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    for migrated in (False, True):
        run = _suspended_past_due(resume=False)
        if migrated:
            run.migrate(
                _review("{workhours: 1}", head=MIGRATE_1_TO_2),
                at=datetime(2026, 3, 5, 9, 0, tzinfo=UTC),
            )
        # Half an hour past at the suspension, and the clock has stood since.
        exit_ = exit_deadline(run.activity("ask"), cancelled_at)
        assert exit_["overdueSeconds"] == 1800
        assert _projected(run, cancelled_at) == ("breached", 1800)


def test_the_cancel_ends_the_suspension_of_the_process_deadline() -> None:
    run = _opened(HALF_DAY, PROCESS_DUE)
    run.clock = LATE_PAUSED
    run.command("suspend")
    assert run.state is not None
    assert run.state["sla"]["overdueStops"] == [{"from": _time(LATE_PAUSED), "to": None}]
    cancelled_at = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
    run.clock = cancelled_at
    run.command("cancel", reason="stop")
    assert run.state["status"] == "cancelled"
    stops = run.state["sla"]["overdueStops"]
    assert stops == [{"from": _time(LATE_PAUSED), "to": _time(cancelled_at)}]
