"""Deadlines counted again by ``packages:apply`` when instances migrate (P016, SC-007).

The acceptance of P016 on the live core: three instances wait on a step with
``due: {workdays: 5}`` for 0.5, 1.5 and 3 working days when a version with
``workdays: 1`` migrates them. All three are counted again from the step's
entry; the two already late get one ``process.sla_breached`` each with
``detectedBy: migration``, no escalation level that has passed fires, the
step's task gets the new due. An instance pinned to the old version keeps
its deadlines (FR-025). The replay of a migrated instance from its journal
has no discrepancy.

The core's clock stands at moments the test sets (the working-time control
calendar ``ru``, 09:00-18:00 Moscow time); the worker does not run, so no
timer fires behind the test's back.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.commands import package_plan, process_instances
from control_plane.infrastructure.db.engine import transaction
from control_plane.infrastructure.db.models import ProcessInstance
from control_plane.worker.main import Worker
from tests.integration.test_package_plan import _apply, _plan, _start
from tests.integration.test_package_test import API_VERSION
from tests.integration.test_process_instances import (
    AGENT,
    _events,
    _instance,
    _journal,
    _open,
    _setup,
    _task,
    _time,
    worker,
)

__all__ = ["worker"]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"
CALENDAR = (FIXTURES / "ru-2025-2027.calendar.yaml").read_text(encoding="utf-8")
PROCESS = "sample-deadlines"

# Thursday 5 March 2026, 13:30 Moscow time: the apply of version 2.
MIGRATED_AT = datetime(2026, 3, 5, 10, 30, tzinfo=UTC)
# The instances entered the step 0.5, 1.5 and 3 working days before it:
# Thursday 09:00, Wednesday 09:00, Monday 13:30.
OPENED = {
    "HALF": datetime(2026, 3, 5, 6, 0, tzinfo=UTC),
    "DAY-AND-HALF": datetime(2026, 3, 4, 6, 0, tzinfo=UTC),
    "THREE": datetime(2026, 3, 2, 10, 30, tzinfo=UTC),
}
# Their deadlines by version 2: one working day from the entry.
DUE = {
    "HALF": "2026-03-06T06:00:00Z",
    "DAY-AND-HALF": "2026-03-05T06:00:00Z",
    "THREE": "2026-03-03T10:30:00Z",
}


class Still:
    """The core's clock standing at a moment the test sets."""

    def __init__(self) -> None:
        self.at = OPENED["THREE"]

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def still(monkeypatch: pytest.MonkeyPatch) -> Iterator[Still]:
    stopped = Still()
    monkeypatch.setattr(process_instances, "utcnow", stopped)
    monkeypatch.setattr(package_plan, "utcnow", stopped)
    yield stopped


def _spec(admin: str, *, version: int, workdays: int, policy: str | None = None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "version": version,
        "displayName": "Sample deadlines",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "calendar": "ru",
        "data": {"type": "object", "properties": {"decision": {"type": "string"}}},
        "start": {"on": {"observation": "sample.opened"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "review",
                        "human": {
                            "taskType": "review",
                            "assign": [{"principal": admin}],
                            "due": {"workdays": workdays, "warnBefore": {"workhours": 4}},
                            "escalations": [
                                {"after": "due", "action": "notify", "to": [{"principal": admin}]},
                                {"after": "PT2H", "action": "notify", "to": [{"principal": admin}]},
                            ],
                        },
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {"id": "done", "complete": {"outcome": "reviewed"}},
                ],
            }
        ],
    }
    if policy is not None:
        spec["migrations"] = [{"from": 1, "to": 2, "policy": policy, "map": {}}]
    return spec


def _package(spec: dict[str, Any]) -> dict[str, Any]:
    manifest = {
        "apiVersion": API_VERSION,
        "kind": "Package",
        "key": "sample-deadlines",
        "spec": {"version": "1.0.0", "displayName": "Sample deadlines"},
    }
    process = {"apiVersion": API_VERSION, "kind": "Process", "key": PROCESS, "spec": spec}
    files = [
        ("package.yaml", yaml.safe_dump(manifest)),
        ("processes/sample.yaml", yaml.safe_dump(process, sort_keys=False)),
        ("calendars/ru.yaml", CALENDAR),
    ]
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def _plan_and_apply(client: httpx.AsyncClient, key: str, spec: dict[str, Any]) -> None:
    package = _package(spec)
    plan = await _plan(client, key, package)
    assert [p for p in plan["problems"] if p["severity"] == "error"] == []
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text


async def _waiting(
    client: httpx.AsyncClient, still: Still
) -> tuple[dict[str, Any], dict[str, str]]:
    """Version 1 applied and one instance per opening, each waiting on its review."""
    s = await _setup(client)
    await _plan_and_apply(client, s["key"], _spec(s["admin"], version=1, workdays=5))
    instances: dict[str, str] = {}
    for name, at in sorted(OPENED.items(), key=lambda item: item[1]):
        still.at = at
        instances[name] = await _start(client, s["key"], name, PROCESS)
    return s, instances


def _timers(sync_engine: Engine, instance_id: str) -> list[tuple[str, str, datetime | None]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT timer_kind, state, due_at FROM process_timers"
                " WHERE instance_id = :i ORDER BY timer_kind, due_at"
            ),
            {"i": instance_id},
        ).all()
    return [tuple(row) for row in rows]  # type: ignore[misc]


async def _of(
    client: httpx.AsyncClient, key: str, event_type: str, instance_id: str
) -> list[dict[str, Any]]:
    return [
        e
        for e in await _events(client, key, event_type)
        if e["payload"]["instanceId"] == instance_id
    ]


async def test_sc007_a_shorter_due_counts_every_open_step_again(
    client: httpx.AsyncClient, sync_engine: Engine, worker: Worker, still: Still
) -> None:
    s, instances = await _waiting(client, still)
    key = s["key"]
    tasks = {
        name: _open(await _instance(client, key, iid), "review")["taskId"]
        for name, iid in instances.items()
    }
    for name in instances:
        # Five working days from the entry, as version 1 says.
        assert (await _task(client, key, tasks[name]))["dueDate"] is not None

    still.at = MIGRATED_AT
    await _plan_and_apply(client, key, _spec(s["admin"], version=2, workdays=1, policy="migrate"))

    for name, iid in instances.items():
        instance = await _instance(client, key, iid)
        assert (instance["definitionVersion"], instance["status"]) == (2, "running")
        # The step's task has the new due.
        task = await _task(client, key, tasks[name])
        assert _time(task["dueDate"]) == _time(DUE[name])
        # Counted again: the journal records the new deadline of the step.
        journal = await _journal(client, key, iid)
        [changed] = [e for e in journal if e["data"].get("decision") == "deadline_migrated"]
        assert changed["kind"] == "migration"
        assert (changed["element"], changed["data"]["dueAt"]) == ("review", DUE[name])
        assert changed["data"]["breached"] is (name != "HALF")

    # 0.5 days in: in time — the deadline and the escalations moved, nothing breached.
    half = instances["HALF"]
    moved = await _of(client, key, "process.timer_rescheduled", half)
    assert moved and {e["payload"]["cause"] for e in moved} == {"migrated"}
    assert not await _of(client, key, "process.sla_breached", half)
    pending = [(kind, due) for kind, state, due in _timers(sync_engine, half) if state == "pending"]
    assert pending == [
        ("escalation", _time("2026-03-06T06:00:00Z")),
        ("escalation", _time("2026-03-06T08:00:00Z")),
        ("sla", _time("2026-03-06T06:00:00Z")),
        ("sla_warning", _time("2026-03-05T11:00:00Z")),
    ]

    # 1.5 and 3 days in: one breach each, found by the migration; no timer left.
    breaches = []
    for name in ("DAY-AND-HALF", "THREE"):
        iid = instances[name]
        [breached] = await _of(client, key, "process.sla_breached", iid)
        payload = breached["payload"]
        assert payload["detectedBy"] == "migration"
        assert (payload["scope"], payload["element"], payload["attempt"]) == ("step", "review", 1)
        assert _time(payload["dueAt"]) == _time(DUE[name])
        assert _time(payload["detectedAt"]) == MIGRATED_AT
        assert payload["assignee"]["principalId"] == s["admin"]
        assert all(state != "pending" for _, state, _ in _timers(sync_engine, iid))
        breaches.append(breached)
    assert len(breaches) == 2
    # No escalation level that had passed fired.
    assert not await _events(client, key, "process.escalated")

    # The replay of every migrated instance from its journal: no discrepancy.
    for iid in instances.values():
        async with transaction(worker.session_factory) as session:
            row = await session.get(ProcessInstance, iid)
            assert row is not None
            result = await process_instances.replay_instance(session, row)
        assert [d.out() for d in result.discrepancies] == []
        assert result.steps == 1  # the input migrated, the first entry after the move


async def test_an_instance_pinned_to_its_version_keeps_its_deadlines(
    client: httpx.AsyncClient, sync_engine: Engine, still: Still
) -> None:
    s, instances = await _waiting(client, still)
    key = s["key"]
    iid = instances["DAY-AND-HALF"]
    task_id = _open(await _instance(client, key, iid), "review")["taskId"]
    due_before = (await _task(client, key, task_id))["dueDate"]
    timers_before = _timers(sync_engine, iid)
    journal_before = await _journal(client, key, iid)

    still.at = MIGRATED_AT
    await _plan_and_apply(client, key, _spec(s["admin"], version=2, workdays=1, policy="pin"))

    instance = await _instance(client, key, iid)
    assert instance["definitionVersion"] == 1
    assert (await _task(client, key, task_id))["dueDate"] == due_before
    assert _timers(sync_engine, iid) == timers_before
    assert await _journal(client, key, iid) == journal_before
    assert not await _events(client, key, "process.timer_rescheduled")
    assert not await _events(client, key, "process.sla_breached")


# --- the section ``deadlines`` of the plan (P017, FR-023) ------------------------------------


def _changed(journal: list[dict[str, Any]], instance_id: str) -> list[dict[str, Any]]:
    """What the apply's step ``migrated`` recorded, in the shape of ``PlanDeadlineOut``."""
    return [
        {
            "instanceId": instance_id,
            "element": entry["element"],
            "previousDueAt": entry["data"]["previousDueAt"],
            "dueAt": entry["data"]["dueAt"],
            "breached": entry["data"]["breached"],
        }
        for entry in journal
        if entry["data"].get("decision") == "deadline_migrated"
    ]


def _section(plan: dict[str, Any]) -> list[dict[str, Any]]:
    [process] = [p for p in plan["processes"] if p["key"] == PROCESS]
    deadlines: list[dict[str, Any]] = process["deadlines"]
    return deadlines


def _instant(value: str | None) -> datetime | None:
    return _time(value) if value is not None else None


def _normal(deadlines: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Deadlines comparable whatever the form of their timestamps, in a stable order."""
    return sorted(
        (
            d["instanceId"],
            d["element"] or "",
            _instant(d["previousDueAt"]),
            _instant(d["dueAt"]),
            d["breached"],
        )
        for d in deadlines
    )


async def _plan_then_apply(
    client: httpx.AsyncClient, key: str, spec: dict[str, Any], instances: dict[str, str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The section of the plan and what the apply of that plan recorded."""
    package = _package(spec)
    plan = await _plan(client, key, package)
    assert [p for p in plan["problems"] if p["severity"] == "error"] == []
    applied = await _apply(client, key, package, plan["planHash"])
    assert applied.status_code == 200, applied.text
    recorded = [
        d for iid in instances.values() for d in _changed(await _journal(client, key, iid), iid)
    ]
    return _section(plan), recorded


async def test_sc007_the_plan_lists_the_deadlines_the_apply_counts(
    client: httpx.AsyncClient, still: Still
) -> None:
    s, instances = await _waiting(client, still)
    key = s["key"]
    journals = {iid: await _journal(client, key, iid) for iid in instances.values()}

    still.at = MIGRATED_AT
    spec = _spec(s["admin"], version=2, workdays=1, policy="migrate")
    planned = _section(await _plan(client, key, _package(spec)))
    # The plan wrote nothing: the journals are as they were.
    for iid, journal in journals.items():
        assert await _journal(client, key, iid) == journal
    assert {(d["instanceId"], d["element"]) for d in planned} == {
        (iid, "review") for iid in instances.values()
    }
    for name, iid in instances.items():
        [deadline] = [d for d in planned if d["instanceId"] == iid]
        assert _time(deadline["dueAt"]) == _time(DUE[name])
        assert deadline["previousDueAt"] is not None
        assert deadline["breached"] is (name != "HALF")

    section, recorded = await _plan_then_apply(client, key, spec, instances)
    assert _normal(section) == _normal(planned)
    assert _normal(section) == _normal(recorded)


async def test_a_lifted_step_due_and_a_new_process_due_are_listed(
    client: httpx.AsyncClient, still: Still
) -> None:
    s, instances = await _waiting(client, still)
    key = s["key"]
    spec = _spec(s["admin"], version=2, workdays=1, policy="migrate")
    human = spec["stages"][0]["steps"][0]["human"]
    del human["due"], human["escalations"]
    spec["due"] = {"workdays": 10}

    still.at = MIGRATED_AT
    section, recorded = await _plan_then_apply(client, key, spec, instances)
    assert _normal(section) == _normal(recorded)
    for iid in instances.values():
        mine = {d["element"]: d for d in section if d["instanceId"] == iid}
        assert set(mine) == {"review", None}
        # The step's deadline is lifted.
        assert mine["review"]["previousDueAt"] is not None
        assert (mine["review"]["dueAt"], mine["review"]["breached"]) == (None, False)
        # The process's deadline appears, ten working days from the start: not past.
        assert mine[None]["previousDueAt"] is None
        assert mine[None]["dueAt"] is not None
        assert mine[None]["breached"] is False


@pytest.mark.parametrize(
    ("workdays", "policy"),
    [(5, "migrate"), (1, "pin")],
    ids=["same-due", "pinned"],
)
async def test_a_plan_that_changes_no_deadline_has_an_empty_section(
    client: httpx.AsyncClient, still: Still, workdays: int, policy: str
) -> None:
    s, instances = await _waiting(client, still)
    key = s["key"]
    still.at = MIGRATED_AT
    spec = _spec(s["admin"], version=2, workdays=workdays, policy=policy)
    section, recorded = await _plan_then_apply(client, key, spec, instances)
    assert section == []
    assert recorded == []


async def test_a_process_without_open_instances_has_an_empty_section(
    client: httpx.AsyncClient, still: Still
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _plan_and_apply(client, key, _spec(s["admin"], version=1, workdays=5))
    plan = await _plan(client, key, _package(_spec(s["admin"], version=2, workdays=1)))
    assert _section(plan) == []
    [process] = plan["processes"]
    assert process["instances"] == []


async def test_a_section_over_its_limit_is_cut_and_counts_all(
    client: httpx.AsyncClient, still: Still, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The section is capped like ``instanceIds`` of ``behaviour``; the hash does not see it."""
    s, instances = await _waiting(client, still)
    key = s["key"]
    still.at = MIGRATED_AT
    package = _package(_spec(s["admin"], version=2, workdays=1, policy="migrate"))
    full = await _plan(client, key, package)
    [process] = full["processes"]
    assert process["deadlinesTotal"] == len(process["deadlines"]) == len(instances)

    monkeypatch.setattr(package_plan, "MAX_PLAN_DEADLINES", 2)
    cut = await _plan(client, key, package)
    [process] = cut["processes"]
    assert process["deadlinesTotal"] == len(instances)
    assert process["deadlines"] == _section(full)[:2]
    assert cut["planHash"] == full["planHash"]

    monkeypatch.setattr(package_plan, "MAX_PLAN_DEADLINES", len(instances))
    [process] = (await _plan(client, key, package))["processes"]
    assert process["deadlines"] == _section(full)
    assert process["deadlinesTotal"] == len(instances)


async def test_a_plan_without_deadlines_to_move_counts_none(
    client: httpx.AsyncClient, still: Still
) -> None:
    s, _ = await _waiting(client, still)
    still.at = MIGRATED_AT
    package = _package(_spec(s["admin"], version=2, workdays=5, policy="migrate"))
    [process] = (await _plan(client, s["key"], package))["processes"]
    assert (process["deadlines"], process["deadlinesTotal"]) == ([], 0)


async def test_the_plan_moves_each_instance_once(
    client: httpx.AsyncClient, still: Still, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_instances`` checks the move and ``_deadlines`` steps on the same copy."""
    s, instances = await _waiting(client, still)
    still.at = MIGRATED_AT
    moved: list[Any] = []
    migrate = package_plan.migrate_state

    def counted(old: Any, new: Any, state: Any, mapping: Any) -> dict[str, Any]:
        moved.append(state)
        return migrate(old, new, state, mapping)

    monkeypatch.setattr(package_plan, "migrate_state", counted)
    package = _package(_spec(s["admin"], version=2, workdays=1, policy="migrate"))
    plan = await _plan(client, s["key"], package)
    assert len(_section(plan)) == len(instances)
    assert len(moved) == len(instances)
