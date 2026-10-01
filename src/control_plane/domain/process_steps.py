"""Step events: the waiting steps of an instance as facts of the core journal (CP-ADR-0074 §13).

Amendment 2026-09-29: the entry of an instance into a waiting step and its
exit are ``process.step_entered`` and ``process.step_exited``. They are a
projection of one engine step, not decisions or intents of the engine: the
application compares ``state.activities`` before and after the step — an
activity that appeared is an entry, one that disappeared is an exit. An
activity opened and closed within one step (an instant step: ``set``,
``decide``, ``remember``, ``do``, ``complete``, ``raise``, or a wait that
ended at once) gives no event at all.

The outcome of an exit is read from the step, the first row that holds:

=========================================================  ===============
the migration input carried it away                        ``migrated``
the activity's own ``timeout`` timer fired                 ``timed_out``
its own input withdrew it: a cancelled task of the step,   ``withdrawn``
  or a cancelled approval of the step that left nobody to
  vote (a participant, not the process, cancelled the work)
the instance went to ``failed`` in this step               ``failed``
the core refused the intent that opened it                 ``failed``
an interrupting boundary timer ended its thread            ``interrupted``
``activity_cancelled`` (any other reason)                  ``cancelled``
the input of the activity closed it                        ``completed``
=========================================================  ===============

A rejected approval and a task done are the step's result: ``completed``.
So is an ``approve`` step the quorum decided after a cancelled approval
(``approved``, or ``rejected`` once the rest can no longer reach it): the
voting rule decided it, not the withdrawal. It is ``withdrawn`` only when no
approver is left (``no_approvers``). A cancelled approval that leaves the
activity open (other approvals still pending) gives no exit at all.

The actor of a ``withdrawn`` exit is the participant who cancelled the work —
the actor of the step's input; every other step event is the process's.

``attempt`` is the number of entries into the element in the instance,
kept by the application in ``process_instances.step_attempts`` — the engine
does not count them. The engine may hold several activities of one element at
once (``onEvent``, ``correlate``), so an activity's own number is kept from
its entry to its exit in ``refs["activity:<id>"]["attempt"]``; an activity
opened before step events existed has none and exits with attempt 1.

The deadline fields come from the activity's SLA deadline (``activity["sla"]``,
set by the engine under revision 2, CP-ADR-0078 §3): an entry carries
``due``, ``warnAt`` and ``provisional``; an exit ``due``, ``breached`` — the
step closed after its deadline, whether or not the timer fired yet — and
``overdueSeconds``. A step without a deadline, or whose deadline failed,
has them empty.

Pure functions over plain values; no I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from control_plane.domain import process_engine as engine
from control_plane.domain import process_sla as sla
from control_plane.domain.process_migration import MIGRATION_INPUT

STEP_ENTERED = "process.step_entered"
STEP_EXITED = "process.step_exited"

# Activity kind -> (stepKind, waitsFor). A ``retry`` activity is the pause of
# a ``try`` between attempts and ``retro_*`` the retrospective after the
# instance closed: neither is a waiting step of the process.
_WAITING: Mapping[str, tuple[str, str]] = {
    "task": ("human", "task"),
    "approval": ("approve", "approval"),
    "skill": ("call", "skill"),
    "agent": ("call", "agent"),
    "child": ("call", "child"),
    "recall": ("recall", "memory"),
    "listen": ("listen", "event"),
    "wait": ("wait", "time"),
}


ACTIVITY_REF = "activity:"


@dataclass(frozen=True)
class StepEvent:
    """One ``process.step_*`` event: its type and payload (the workspace is the caller's)."""

    type: str
    activity_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class StepProjection:
    """The step events of one engine step and the application's bookkeeping after it.

    ``attempts`` are the counters per element (``step_attempts``), ``refs``
    the instance's refs with the attempt of each open waiting activity.
    """

    events: list[StepEvent]
    attempts: dict[str, int]
    refs: dict[str, Any]


def waiting(activity: Mapping[str, Any]) -> bool:
    """Whether the activity is a waiting step that step events report."""
    return str(activity.get("kind")) in _WAITING


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rfc3339(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _deadline(activity: Mapping[str, Any]) -> Mapping[str, Any]:
    found = activity.get("sla")
    return found if isinstance(found, Mapping) else {}


def exit_deadline(activity: Mapping[str, Any], at: datetime) -> dict[str, Any]:
    """``due``, ``breached`` and ``overdueSeconds`` of an activity that closed at ``at``."""
    record = _deadline(activity)
    due = record.get("dueAt")
    if not due:
        return {"due": None, "breached": False, "overdueSeconds": None}
    breached = record.get("state") == sla.BREACHED or at > _time(str(due))
    return {
        "due": due,
        "breached": breached,
        "overdueSeconds": (
            sla.overdue_seconds(_time(str(due)), at, record.get("overdueStops"))
            if breached
            else None
        ),
    }


def _decided(decisions: Sequence[Mapping[str, Any]], kind: str) -> list[Mapping[str, Any]]:
    return [d for d in decisions if d.get("kind") == kind]


def exit_outcome(
    activity: Mapping[str, Any],
    *,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    given: Mapping[str, Any],
) -> str:
    """The ``outcome`` of an activity that disappeared in this step (see the module table).

    ``given`` is the step's input as the journal keeps it (``Input.out()``).
    """
    aid = activity["id"]
    input_kind = given.get("kind")
    if input_kind == MIGRATION_INPUT:
        return "migrated"
    timers = before.get("timers") or {}
    for fired in _decided(decisions, "timer_fired"):
        timer = timers.get(str(fired.get("timerId"))) or {}
        if fired.get("timerKind") == "timeout" and timer.get("activity") == aid:
            return "timed_out"
    if _withdrawn(activity, given):
        return "withdrawn"
    if after.get("status") == engine.FAILED and before.get("status") != engine.FAILED:
        return "failed"
    if input_kind == "intent_failed" and any(
        d.get("element") == activity.get("element") for d in _decided(decisions, "intent_failed")
    ):
        return "failed"
    for cancelled in _decided(decisions, "activity_cancelled"):
        if cancelled.get("activity") == aid:
            return "interrupted" if cancelled.get("reason") == "interrupted" else "cancelled"
    return "completed"


def _withdrawn(activity: Mapping[str, Any], given: Mapping[str, Any]) -> bool:
    """Whether the input is a participant's cancellation of this activity's work."""
    body = given.get("body") or {}
    if not isinstance(body, Mapping) or body.get("activityId") != activity["id"]:
        return False
    kind = given.get("kind")
    if kind == "task":
        return body.get("status") == "cancelled"
    if kind != "approval" or body.get("outcome") != "cancelled":
        return False
    # The voting rule decided the step unless nobody is left to vote: with
    # approvers remaining the quorum's approved/rejected is the step's result.
    remaining = body.get("total")
    if remaining is None:
        remaining = activity.get("total")
    return remaining is not None and int(remaining) <= 0


def withdrawn(event: StepEvent) -> bool:
    """Whether the event is the exit of a step a participant withdrew.

    Its actor is the participant who cancelled the work (the actor of the
    step's input), not the process.
    """
    return event.type == STEP_EXITED and event.payload.get("outcome") == "withdrawn"


def sla_attempt(
    payload: Mapping[str, Any], *, refs: Mapping[str, Any], attempts: Mapping[str, int]
) -> int | None:
    """The attempt of the step a ``process.sla_*`` event is about: the application counts it.

    The engine does not know attempts (``step_attempts``). An open activity
    keeps the number it entered with in ``refs["activity:<id>"]``; one opened
    in this very step (a deadline that failed at once) is not counted yet
    and gets the next number of its element. The process's deadline has none.
    """
    if payload.get("scope") != sla.STEP or not payload.get("activityId"):
        return None
    record = refs.get(f"{ACTIVITY_REF}{payload['activityId']}")
    if isinstance(record, Mapping) and "attempt" in record:
        return int(record["attempt"])
    return int(attempts.get(str(payload.get("element")), 0)) + 1


def _opened_by(refs: Mapping[str, Any], aid: str, prefix: str) -> list[str]:
    return sorted(
        ref.split(":", 1)[1]
        for ref, target in refs.items()
        if ref.startswith(prefix) and isinstance(target, Mapping) and target.get("activity") == aid
    )


def step_events(
    definition: engine.Definition,
    *,
    instance_id: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    given: Mapping[str, Any],
    at: datetime,
    attempts: Mapping[str, int],
    refs: Mapping[str, Any],
) -> StepProjection:
    """The step events of one engine step, the attempt counters and refs after it.

    ``before``/``after`` are the instance's state around the step (``None``
    before its first one), ``decisions`` the step's decisions as the journal
    keeps them, ``given`` its input as the journal keeps it (``Input.out()``:
    ``kind``, ``body``), ``at`` its time, ``refs`` the instance's refs after its
    intents ran — the tasks, approvals, calls and nested instances the new
    activities opened. Exits come first, in the order the activities opened,
    then entries: a step closing one attempt of an element and opening the
    next reports them in that order.
    """
    prior = (before or {}).get("activities") or {}
    current = after.get("activities") or {}
    counters = dict(attempts)
    kept = dict(refs)
    common = {
        "instanceId": instance_id,
        "definitionKey": definition.key,
        "version": after.get("version", definition.version),
        "instanceKey": after.get("key"),
    }
    exited_at = _rfc3339(at)
    events: list[StepEvent] = []

    def base(activity: Mapping[str, Any], attempt: int) -> dict[str, Any]:
        element = str(activity["element"])
        step = definition.steps.get(element)
        return {
            **common,
            "element": element,
            "stage": step.stage if step is not None else None,
            "stepKind": _WAITING[str(activity["kind"])][0],
            "attempt": attempt,
            "activityId": activity["id"],
            "enteredAt": activity["openedAt"],
        }

    closed = [a for aid, a in prior.items() if aid not in current and waiting(a)]
    for activity in sorted(closed, key=lambda a: a["n"]):
        ref = f"{ACTIVITY_REF}{activity['id']}"
        record = kept.get(ref)
        attempt = 1
        if isinstance(record, Mapping) and "attempt" in record:
            attempt = int(record["attempt"])
            rest = {k: v for k, v in record.items() if k != "attempt"}
            if rest:
                kept[ref] = rest
            else:
                del kept[ref]
        seconds = int((at - _time(str(activity["openedAt"]))).total_seconds())
        payload = {
            **base(activity, attempt),
            "exitedAt": exited_at,
            "outcome": exit_outcome(
                activity,
                before=before or {},
                after=after,
                decisions=decisions,
                given=given,
            ),
            "durationSeconds": max(seconds, 0),
            **exit_deadline(activity, at),
        }
        events.append(StepEvent(STEP_EXITED, str(activity["id"]), payload))

    opened = [a for aid, a in current.items() if aid not in prior and waiting(a)]
    for activity in sorted(opened, key=lambda a: a["n"]):
        aid = str(activity["id"])
        element = str(activity["element"])
        counters[element] = counters.get(element, 0) + 1
        ref = f"{ACTIVITY_REF}{aid}"
        found = kept.get(ref)
        entry: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
        kept[ref] = {**entry, "attempt": counters[element]}
        tasks = _opened_by(refs, aid, "task:")
        skills = _opened_by(refs, aid, "skill:")
        children = _opened_by(refs, aid, "child:")
        payload = {
            **base(activity, counters[element]),
            "waitsFor": _WAITING[str(activity["kind"])][1],
            "taskId": tasks[-1] if tasks else None,
            "approvalIds": _opened_by(refs, aid, "approval:"),
            "skillInvocationId": skills[-1] if skills else None,
            "childInstanceId": children[-1] if children else None,
            "due": _deadline(activity).get("dueAt"),
            "warnAt": _deadline(activity).get("warnAt"),
            "provisional": bool(_deadline(activity).get("provisional")),
        }
        events.append(StepEvent(STEP_ENTERED, aid, payload))
    return StepProjection(events, counters, kept)
