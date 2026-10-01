"""The SLA of the projection, computed on read (CP-ADR-0078 §6; P014).

``slaState`` follows ``dueAt``, ``warnAt`` and the time of reading, whether
or not the worker fired the timers (FR-021); a deadline whose timer is frozen
is ``paused`` with the remainder the timer keeps and its unit — clocks stand
per thread, so a suspended instance may have deadlines that run on; a
deadline that was not computed is ``unknown``. The denormalized
``sla_due_at`` and ``sla_warn_at`` are the earliest of the running deadlines.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from control_plane.domain import process_sla as sla

DUE = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
WARN = DUE - timedelta(minutes=15)
FROZEN = {"t1": {"state": "frozen", "remaining": 18000.0, "remainingUnit": "working_seconds"}}


def _record(**changes: Any) -> dict[str, Any]:
    found = sla.record(
        due_at="2026-10-07T12:00:00Z",
        warn_at="2026-10-07T11:45:00Z",
        provisional=False,
        timer="t1",
        warn_timer="t2",
    )
    return {**found, **changes}


def _shown(
    found: Mapping[str, Any] | None,
    at: datetime,
    timers: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, str, int | None]:
    return sla.shown(found, now=at, timers=timers or {})


def test_the_state_follows_the_clock_not_the_worker() -> None:
    due, state, overdue = _shown(_record(), WARN - timedelta(minutes=1))
    assert (state, overdue) == ("ok", None)
    assert due == {
        "dueAt": "2026-10-07T12:00:00Z",
        "warnAt": "2026-10-07T11:45:00Z",
        "provisional": False,
        "remainingSeconds": 16 * 60,
        "remainingUnit": "wall",
    }
    # The timers are still pending: the record says pending, the clock says otherwise.
    assert _shown(_record(), WARN)[1] == "warning"
    due, state, overdue = _shown(_record(), DUE + timedelta(seconds=90))
    assert (state, overdue, due["remainingSeconds"], due["remainingUnit"]) == (
        "breached",
        90,
        None,
        None,
    )
    assert _shown(_record(), DUE)[1] == "breached"


def test_a_recorded_fact_stands() -> None:
    assert _shown(_record(state=sla.WARNING), WARN - timedelta(hours=1))[1] == "warning"
    assert _shown(_record(state=sla.BREACHED), DUE - timedelta(hours=1))[1:] == ("breached", 0)


def test_a_frozen_deadline_is_paused_with_its_remainder_and_unit() -> None:
    due, state, overdue = _shown(_record(), DUE + timedelta(days=2), FROZEN)
    assert (state, overdue) == ("paused", None)
    assert due == {
        "dueAt": None,
        "warnAt": None,
        "provisional": False,
        "remainingSeconds": 18000,
        "remainingUnit": "working_seconds",
    }
    # Workdays are kept encoded; the unit says so.
    workdays = {"t1": {"state": "frozen", "remaining": 122400, "remainingUnit": "workdays"}}
    assert _shown(_record(), DUE, workdays)[0]["remainingUnit"] == "workdays"
    # A deadline from the data keeps no remainder, and so no unit.
    frozen = {"t1": {"state": "frozen", "remaining": None, "remainingUnit": "wall"}}
    assert _shown(_record(), DUE, frozen)[0] == {
        "dueAt": None,
        "warnAt": None,
        "provisional": False,
        "remainingSeconds": None,
        "remainingUnit": None,
    }


def test_a_deadline_whose_timer_runs_is_read_by_the_clock() -> None:
    """A suspended instance: the timer of an onEvent step runs on (CP-ADR-0078 §4)."""
    pending = {"t1": {"state": "pending", "dueAt": "2026-10-07T12:00:00Z"}}
    due, state, overdue = _shown(_record(), DUE + timedelta(minutes=5), pending)
    assert (state, overdue, due["dueAt"]) == ("breached", 300, "2026-10-07T12:00:00Z")
    # A breach recorded before the pause keeps its overdue time, whatever the timer says.
    breached = _record(state=sla.BREACHED)
    assert _shown(breached, DUE + timedelta(minutes=5), FROZEN)[1:] == ("breached", 300)


def test_a_failed_deadline_is_unknown_and_no_deadline_is_none() -> None:
    failed = sla.record(due_at=None, warn_at=None, provisional=False, error={"type": "x"})
    due, state, _ = _shown(failed, DUE)
    assert state == "unknown"
    assert due == {
        "dueAt": None,
        "warnAt": None,
        "provisional": False,
        "remainingSeconds": None,
        "remainingUnit": None,
    }
    assert _shown({**failed, "timer": "t1"}, DUE, FROZEN)[1] == "unknown"
    assert _shown(None, DUE) == (None, "none", None)


def test_the_instance_shows_the_worst_state() -> None:
    assert sla.worst(["ok", "paused", "warning", "none"]) == "warning"
    assert sla.worst(["ok", "unknown", "breached"]) == "breached"
    assert sla.worst(["none", "ok"]) == "ok"
    assert sla.worst([]) == "none"


def test_the_filter_columns_are_the_earliest_running_deadline_and_warning() -> None:
    later = _record(dueAt="2026-10-07T11:00:00Z", warnAt=None, timer="t3")
    failed = sla.record(due_at=None, warn_at=None, provisional=False, error={"type": "x"})
    records = [None, _record(), later, failed]
    assert sla.open_moments(records, {}) == (DUE - timedelta(hours=1), WARN)
    assert sla.open_moments([None, failed], {}) == (None, None)
    # A frozen deadline does not count; the one that runs on does.
    assert sla.open_moments(records, FROZEN) == (DUE - timedelta(hours=1), None)
    assert sla.open_moments([_record()], FROZEN) == (None, None)
    # A breach recorded before the pause stays in the columns.
    assert sla.open_moments([_record(state=sla.BREACHED)], FROZEN) == (DUE, WARN)


def test_a_deadline_due_before_its_timer_froze_is_breached_by_the_clock() -> None:
    """FR-021: passed by the clock before the pause, not yet fired by the worker."""
    late = {
        "t1": {
            "state": "frozen",
            "remaining": 0.0,
            "remainingUnit": "wall",
            "frozenFrom": "2026-10-07T12:00:00Z",
            "frozenAt": "2026-10-07T12:30:00Z",
        }
    }
    # The suspension opened a stop at the freeze: the overdue clock stands with the thread.
    standing = _record(overdueStops=[{"from": "2026-10-07T12:30:00Z", "to": None}])
    due, state, overdue = _shown(standing, DUE + timedelta(hours=1), late)
    assert (state, overdue, due["dueAt"]) == ("breached", 1800, "2026-10-07T12:00:00Z")
    assert _shown(standing, DUE + timedelta(days=1), late)[2] == 1800
    assert due["remainingSeconds"] is None
    assert sla.open_moments([_record()], late) == (DUE, WARN)
    # Frozen before its moment it stands.
    early = {"t1": {**late["t1"], "remaining": 60.0, "frozenAt": "2026-10-07T11:59:00Z"}}
    assert _shown(_record(), DUE + timedelta(hours=1), early)[1] == "paused"


def test_the_overdue_time_leaves_out_the_suspensions_of_the_record() -> None:
    """``overdueStops``: suspensions a past deadline sat through, closed and open."""
    stops = [
        {"from": "2026-10-07T12:30:00Z", "to": "2026-10-07T16:00:00Z"},
        {"from": "2026-10-07T17:00:00Z", "to": "2026-10-07T18:00:00Z"},
    ]
    found = _record(state="breached", overdueStops=stops)
    # 8 hours past less 3.5 and 1 hours of suspension.
    assert _shown(found, DUE + timedelta(hours=8))[1:] == ("breached", 12600)
    # Inside the second suspension its part up to now is left out.
    assert _shown(found, DUE + timedelta(hours=5, minutes=30))[1:] == ("breached", 5400)
    # A suspension still going on has no end.
    going = _record(state="breached", overdueStops=[{"from": "2026-10-07T12:30:00Z", "to": None}])
    assert _shown(going, DUE + timedelta(days=1))[1:] == ("breached", 1800)
