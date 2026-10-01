"""SLA deadlines of a live instance with the engine's clock (CP-ADR-0078 §3; P011).

A step with ``due`` in a version of engine revision 2 sets the deadline
timers of its activity; the worker's timer loop fires them. The acceptance
of P011 on the live core: the ``dueAt`` of ``process.sla_breached`` is the
declared deadline to the second, and the breach comes in the first cycle of
``process_timers()`` after the deadline (SC-003); the events pass the event
catalog and carry the step's attempt; a deadline that cannot be computed is
``process.sla_failed`` and the instance lives on; a version of revision 1
with ``due`` gets no SLA facts (FR-031). A pause (P012) stops the clocks and
keeps the remainder in the deadline's unit in the database, so a worker
restarted during it loses nothing; a deadline from the data does not move.

The addressees of the SLA facts (P013): ``owner`` is the first resolvable
candidate of ``spec.owner`` and ``assignee`` the step's, as ``{principalId,
roleId, workspaceId}``; a role is the one of the instance's workspace, and a
role that is not there leaves the field empty without failing the event.
The assignee of a step with a task is who the task is assigned to now, not
the step's chain resolved anew. The targets of an escalation level get their
addressees the same way, one per target, beside their text in ``to``
(amendment 2026-09-30).
"""

import copy
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.commands import process_instances
from control_plane.config import Settings
from control_plane.domain.event_catalog import current_version, schema_for
from control_plane.worker.main import Worker
from tests.helpers import auth, create_role, create_workspace
from tests.integration.test_process_instances import (
    AGENT,
    _complete,
    _events,
    _instance,
    _observe,
    _open,
    _publish,
    _setup,
    _time,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "processes"


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


class Clock:
    """The core's clock, moved by the test: what ``utcnow()`` of the process commands returns."""

    def __init__(self) -> None:
        self.shift = timedelta(0)

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.shift


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    moved = Clock()
    monkeypatch.setattr(process_instances, "utcnow", moved)
    yield moved


def _flow(admin: str, due: Any) -> dict[str, Any]:
    """One human step with a deadline and no escalations, then the end."""
    return {
        "version": 1,
        "displayName": "Sample deadline",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {"type": "object", "properties": {"note": {"type": "string"}}},
        "start": {"on": {"observation": "sample.deadline"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "review",
                        "human": {
                            "taskType": "review",
                            "assign": [{"principal": admin}],
                            "due": due,
                        },
                    },
                    {"id": "done", "complete": {"outcome": "ok"}},
                ],
            }
        ],
    }


async def _start(
    client: httpx.AsyncClient, key: str, process: str, workspace: str | None = None
) -> dict[str, Any]:
    started = await client.post(
        "/api/v1/process-instances",
        json={"process": process, "key": "D-1", "workspaceId": workspace},
        headers=auth(key),
    )
    assert started.status_code == 201, started.text
    body: dict[str, Any] = started.json()
    return body


async def _of(
    client: httpx.AsyncClient, key: str, event_type: str, instance_id: str
) -> list[dict[str, Any]]:
    found = [e for e in await _events(client, key, event_type) if e["entityId"] == instance_id]
    for event in found:
        schema = schema_for(event_type, current_version(event_type))
        assert schema is not None
        errors = Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(
            event["payload"]
        )
        assert [e.message for e in errors] == [], event_type
    return found


async def test_the_breach_is_the_declared_deadline_in_the_first_timer_cycle_after_it(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    due = {"duration": "PT1H", "warnBefore": "PT15M"}
    await _publish(client, key, "sample-deadline", _flow(s["admin"], due))
    instance = await _start(client, key, "sample-deadline")
    iid = instance["id"]
    [entered] = await _of(client, key, "process.step_entered", iid)
    declared = _time(entered["payload"]["enteredAt"]) + timedelta(hours=1)
    assert _time(entered["payload"]["due"]) == declared
    assert _time(entered["payload"]["warnAt"]) == declared - timedelta(minutes=15)
    assert entered["payload"]["provisional"] is False

    # Before the threshold nothing is due; past it, the first cycle warns.
    assert await worker.process_timers() == 0
    clock.shift = timedelta(minutes=46)
    assert await worker.process_timers() == 1
    [warning] = await _of(client, key, "process.sla_warning", iid)
    assert warning["payload"]["attempt"] == 1
    assert not await _of(client, key, "process.sla_breached", iid)

    # The first cycle after the deadline records the breach, once.
    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 1
    assert await worker.process_timers() == 0
    [breached] = await _of(client, key, "process.sla_breached", iid)
    payload = breached["payload"]
    assert abs(_time(payload["dueAt"]) - declared) < timedelta(seconds=1)
    assert _time(payload["dueAt"]) == _time(entered["payload"]["due"])
    detected = _time(payload["detectedAt"])
    assert timedelta(0) <= detected - _time(payload["dueAt"]) < timedelta(seconds=30)
    assert payload["overdueSeconds"] == int((detected - _time(payload["dueAt"])).total_seconds())
    assert (payload["scope"], payload["element"], payload["attempt"]) == ("step", "review", 1)
    assert payload["activityId"] == entered["payload"]["activityId"]
    assert (payload["detectedBy"], payload["provisional"]) == ("timer", False)
    assert breached["actorId"] == s["agent"]
    # The fact is the SLA event: the deadline timers give no process.timer_fired.
    assert not await _of(client, key, "process.timer_fired", iid)

    # The step closes late: its exit says so. The engine's time of the close is
    # its clock (the breach's input), as the test moved only the timer loop's.
    live = await _instance(client, key, iid)
    await _complete(client, key, _open(live, "review")["taskId"], {"decision": "yes"})
    await worker.run_once()
    live = await _instance(client, key, iid)
    assert live["status"] == "completed"
    [exited] = await _of(client, key, "process.step_exited", iid)
    assert exited["payload"]["breached"] is True
    assert exited["payload"]["due"] == entered["payload"]["due"]
    assert exited["payload"]["overdueSeconds"] is not None


async def test_a_deadline_that_fails_leaves_the_instance_running(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    due = {"at": "timestamp(data.note)"}
    await _publish(client, key, "sample-deadline", _flow(s["admin"], due))
    instance = await _start(client, key, "sample-deadline")
    assert instance["status"] == "running"
    [failed] = await _of(client, key, "process.sla_failed", instance["id"])
    assert (failed["payload"]["scope"], failed["payload"]["element"]) == ("step", "review")
    assert failed["payload"]["attempt"] == 1
    assert failed["payload"]["error"]["status"] == 422
    review = _open(instance, "review")
    await _complete(client, key, review["taskId"], {"decision": "yes"})
    await worker.run_once()
    assert (await _instance(client, key, instance["id"]))["status"] == "completed"


async def test_a_version_of_revision_1_with_a_due_gets_no_sla_facts(
    client: httpx.AsyncClient, worker: Worker, clock: Clock, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-deadline", _flow(s["admin"], "PT1H"))
    # A version published before SLA deadlines: past the immutability trigger.
    with sync_engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            text("UPDATE process_definitions SET engine_revision = 1 WHERE key = 'sample-deadline'")
        )
    instance = await _start(client, key, "sample-deadline")
    with sync_engine.begin() as conn:
        kinds = conn.execute(
            text("SELECT timer_kind FROM process_timers WHERE instance_id = :i"),
            {"i": instance["id"]},
        ).scalars()
        assert not {"sla", "sla_warning"} & set(kinds)
    clock.shift = timedelta(hours=2)
    await worker.process_timers()
    for event_type in ("process.sla_warning", "process.sla_breached", "process.sla_failed"):
        assert not await _of(client, key, event_type, instance["id"])
    [entered] = await _of(client, key, "process.step_entered", instance["id"])
    assert entered["payload"]["due"] is None


def _address(
    principal: str | None = None, role: str | None = None, workspace: str | None = None
) -> dict[str, Any]:
    return {"principalId": principal, "roleId": role, "workspaceId": workspace}


async def test_the_owner_role_is_the_role_of_the_process_workspace(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    workspace = (await create_workspace(client, key, "deadlines"))["id"]
    await create_role(client, key, "lead")
    lead = await create_role(client, key, "lead", workspace_id=workspace)
    flow = _flow(s["admin"], {"duration": "PT1H", "warnBefore": "PT15M"})
    await _publish(client, key, "sample-deadline", {**flow, "workspaceId": workspace})
    iid = (await _start(client, key, "sample-deadline"))["id"]

    clock.shift = timedelta(minutes=46)
    assert await worker.process_timers() == 1
    [warning] = await _of(client, key, "process.sla_warning", iid)
    # The role of the workspace before the tenant-wide one of the same slug.
    assert warning["payload"]["owner"] == _address(role=lead["id"], workspace=workspace)
    assert warning["payload"]["assignee"] == _address(principal=s["admin"], workspace=workspace)
    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 1
    [breached] = await _of(client, key, "process.sla_breached", iid)
    assert breached["payload"]["owner"] == warning["payload"]["owner"]
    assert breached["payload"]["assignee"] == warning["payload"]["assignee"]


async def test_a_tenant_process_resolves_the_owner_in_the_workspace_of_the_instance(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    workspace = (await create_workspace(client, key, "deadlines"))["id"]
    lead = await create_role(client, key, "lead", workspace_id=workspace)
    await _publish(client, key, "sample-deadline", _flow(s["admin"], "PT1H"))
    iid = (await _start(client, key, "sample-deadline", workspace))["id"]

    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 1
    [breached] = await _of(client, key, "process.sla_breached", iid)
    assert breached["payload"]["owner"] == _address(role=lead["id"], workspace=workspace)
    assert breached["payload"]["assignee"] == _address(principal=s["admin"], workspace=workspace)


async def test_a_principal_owner_after_an_unresolvable_role(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    flow = _flow(s["admin"], "PT1H")
    flow["owner"] = [{"role": "nobody"}, {"expr": "data.note"}, {"principal": s["admin"]}]
    await _publish(client, key, "sample-deadline", flow)
    iid = (await _start(client, key, "sample-deadline"))["id"]

    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 1
    [breached] = await _of(client, key, "process.sla_breached", iid)
    # The expression has no data.note to read: that candidate is skipped too.
    assert breached["payload"]["owner"] == _address(principal=s["admin"])


async def test_a_role_that_is_not_there_leaves_the_addressee_empty(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    # No role "lead" in the tenant: the owner does not resolve.
    await _publish(client, key, "sample-deadline", _flow(s["admin"], "PT1H"))
    iid = (await _start(client, key, "sample-deadline"))["id"]
    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 1
    [breached] = await _of(client, key, "process.sla_breached", iid)
    assert breached["payload"]["owner"] is None
    assert breached["payload"]["assignee"] == _address(principal=s["admin"])

    await _publish(client, key, "sample-failed", _flow(s["admin"], {"at": "timestamp(data.note)"}))
    failed_iid = (await _start(client, key, "sample-failed"))["id"]
    [failed] = await _of(client, key, "process.sla_failed", failed_iid)
    assert failed["payload"]["owner"] is None
    assert "assignee" not in failed["payload"]


async def test_the_assignee_of_an_approve_step_is_its_pending_approver(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    approver = await create_role(client, key, "approver")
    flow = _flow(s["admin"], None)
    flow["stages"][0]["steps"][0] = {
        "id": "sign",
        "approve": {"approvers": [{"role": "approver"}], "quorum": "all", "due": "PT1H"},
    }
    await _publish(client, key, "sample-deadline", flow)
    iid = (await _start(client, key, "sample-deadline"))["id"]
    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 1
    [breached] = await _of(client, key, "process.sla_breached", iid)
    assert breached["payload"]["element"] == "sign"
    assert breached["payload"]["assignee"] == _address(role=approver["id"])


async def test_the_assignee_of_a_human_step_is_the_one_its_task_was_reassigned_to(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-deadline", _flow(s["admin"], "PT1H"))
    iid = (await _start(client, key, "sample-deadline"))["id"]
    task_id = _open(await _instance(client, key, iid), "review")["taskId"]
    task = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(key))
    assert task.json()["assigneeId"] == s["admin"]
    # Reassigned past the process: the step's chain still names the admin.
    patched = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"assigneeId": s["agent"]},
        headers={**auth(key), "If-Match": f'"task-{task.json()["version"]}"'},
    )
    assert patched.status_code == 200, patched.text

    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 1
    [breached] = await _of(client, key, "process.sla_breached", iid)
    assert breached["payload"]["assignee"] == _address(principal=s["agent"])


async def test_an_agent_owner_is_the_principal_of_the_agent(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    flow = _flow(s["admin"], "PT1H")
    flow["owner"] = [{"role": "nobody"}, {"agent": AGENT}, {"principal": s["admin"]}]
    await _publish(client, key, "sample-deadline", flow)
    iid = (await _start(client, key, "sample-deadline"))["id"]

    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 1
    [breached] = await _of(client, key, "process.sla_breached", iid)
    assert breached["payload"]["owner"] == _address(principal=s["agent"])


# --- the addressees of escalation levels (CP-ADR-0078, amendment 2026-09-30) -------------


def _escalating(admin: str, to: list[dict[str, Any]]) -> dict[str, Any]:
    """The step of ``_flow`` with a level ``notify`` at its deadline, to ``to``."""
    flow = _flow(admin, "PT1H")
    step = flow["stages"][0]["steps"][0]["human"]
    step["escalations"] = [{"after": "due", "action": "notify", "to": to}]
    return flow


async def _escalated(
    client: httpx.AsyncClient, key: str, worker: Worker, clock: Clock, iid: str
) -> dict[str, Any]:
    clock.shift = timedelta(hours=1, seconds=2)
    assert await worker.process_timers() == 2, "the deadline and the level at it"
    [escalated] = await _of(client, key, "process.escalated", iid)
    assert escalated["schemaVersion"] == current_version("process.escalated") == 2
    payload: dict[str, Any] = escalated["payload"]
    return payload


async def test_a_notify_level_to_a_role_names_the_role_of_the_process_workspace(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    workspace = (await create_workspace(client, key, "deadlines"))["id"]
    await create_role(client, key, "general-director")
    director = await create_role(client, key, "general-director", workspace_id=workspace)
    flow = _escalating(s["admin"], [{"role": "general-director"}])
    await _publish(client, key, "sample-deadline", {**flow, "workspaceId": workspace})
    iid = (await _start(client, key, "sample-deadline"))["id"]

    payload = await _escalated(client, key, worker, clock, iid)
    assert (payload["level"], payload["action"]) == (1, "notify")
    # The text of the target stays as it was; its addressee stands beside it.
    assert payload["to"] == ["role:general-director"]
    # The role of the workspace before the tenant-wide one of the same slug.
    assert payload["addressees"] == [_address(role=director["id"], workspace=workspace)]
    assert payload["unresolved"] == []


async def test_every_target_of_a_level_is_resolved_on_its_own(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    workspace = (await create_workspace(client, key, "deadlines"))["id"]
    director = await create_role(client, key, "general-director")
    stranger = "00000000-0000-4000-8000-000000000001"
    to = [
        {"role": "nobody"},
        {"role": "general-director"},
        {"agent": AGENT},
        {"principal": stranger},
        {"principal": s["admin"]},
    ]
    await _publish(client, key, "sample-deadline", _escalating(s["admin"], to))
    # A tenant process started in a workspace: the addressees are in it.
    iid = (await _start(client, key, "sample-deadline", workspace))["id"]

    payload = await _escalated(client, key, worker, clock, iid)
    assert payload["to"] == [
        "role:nobody",
        "role:general-director",
        f"agent:{AGENT}",
        stranger,
        s["admin"],
    ]
    assert payload["addressees"] == [
        None,
        _address(role=director["id"], workspace=workspace),
        _address(principal=s["agent"], workspace=workspace),
        None,
        _address(principal=s["admin"], workspace=workspace),
    ]
    # A principal of no tenant or of another one reads the same: unknown.
    assert payload["unresolved"] == [
        {"index": 0, "target": "role:nobody", "reason": "unknown_role"},
        {"index": 3, "target": stranger, "reason": "unknown_principal"},
    ]


async def test_a_role_that_is_not_there_leaves_the_level_without_addressee_not_without_event(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    flow = _escalating(s["admin"], [{"role": "general-director"}])
    await _publish(client, key, "sample-deadline", flow)
    iid = (await _start(client, key, "sample-deadline"))["id"]

    payload = await _escalated(client, key, worker, clock, iid)
    assert payload["to"] == ["role:general-director"]
    assert payload["addressees"] == [None]
    assert payload["unresolved"] == [
        {"index": 0, "target": "role:general-director", "reason": "unknown_role"}
    ]
    assert (await _instance(client, key, iid))["status"] == "running"


async def test_a_level_without_to_addresses_the_assignee_of_the_step(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    flow = _flow(s["admin"], "PT1H")
    flow["stages"][0]["steps"][0]["human"]["escalations"] = [{"after": "due", "action": "remind"}]
    await _publish(client, key, "sample-deadline", flow)
    iid = (await _start(client, key, "sample-deadline"))["id"]

    payload = await _escalated(client, key, worker, clock, iid)
    assert payload["to"] == [s["admin"]]
    assert payload["addressees"] == [_address(principal=s["admin"])]
    assert payload["unresolved"] == []


async def test_the_addressees_stay_out_of_the_journal_the_replay_reads(
    client: httpx.AsyncClient, worker: Worker, clock: Clock
) -> None:
    s = await _setup(client)
    key = s["key"]
    director = await create_role(client, key, "general-director")
    flow = _escalating(s["admin"], [{"role": "general-director"}, {"role": "nobody"}])
    await _publish(client, key, "sample-deadline", flow)
    iid = (await _start(client, key, "sample-deadline"))["id"]
    payload = await _escalated(client, key, worker, clock, iid)
    assert payload["addressees"] == [_address(role=director["id"]), None]

    # The engine's intent carries the targets as text only: a replay of the
    # journal gives the recorded intents, again and again.
    for _ in range(2):
        replayed = await client.post(
            "/api/v1/process-definitions/sample-deadline:replay",
            json={"spec": flow},
            headers=auth(key),
        )
        assert replayed.status_code == 200, replayed.text
        body = replayed.json()
        assert (body["replayed"], body["diverged"]) == (1, 0), body


# --- a pause (CP-ADR-0078 §4; P012) ------------------------------------------------------

RU_SPEC: dict[str, Any] = yaml.safe_load(
    (FIXTURES / "ru-2025-2027.calendar.yaml").read_text("utf-8")
)["spec"]
# The pause of the working-time control set (P008): entered Wed 7 Oct 2026
# 10:00 Moscow time, paused Thu 8 Oct 12:00, resumed Sat 10 Oct 14:00.
ENTERED = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
PAUSED = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)
RESUMED = datetime(2026, 10, 10, 11, 0, tzinfo=UTC)


class Still:
    """The core's clock standing at a moment the test sets."""

    def __init__(self) -> None:
        self.at = ENTERED

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def still(monkeypatch: pytest.MonkeyPatch) -> Iterator[Still]:
    stopped = Still()
    monkeypatch.setattr(process_instances, "utcnow", stopped)
    yield stopped


def _sla_timer(sync_engine: Engine, instance_id: str) -> tuple[Any, ...]:
    with sync_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT state, due_at, remaining_seconds, remaining_unit FROM process_timers"
                " WHERE instance_id = :i AND timer_kind = 'sla'"
            ),
            {"i": instance_id},
        ).one()
    return tuple(row)


async def _calendar(client: httpx.AsyncClient, key: str, spec: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/calendars", json={"key": "ru", "spec": spec}, headers=auth(key)
    )
    assert response.status_code == 201, response.text


async def _command(client: httpx.AsyncClient, key: str, instance_id: str, action: str) -> None:
    response = await client.post(
        f"/api/v1/process-instances/{instance_id}:{action}",
        json={"reason": "a corrected invoice"} if action == "suspend" else {},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    ("due", "declared", "remaining", "resumed"),
    [
        # Thu 17:00; Thu 12-17 is 5h; from Sat 14:00, Mon 09-14.
        ({"workhours": 16, "calendar": "ru"}, "2026-10-08T14:00:00Z", 5, "2026-10-12T11:00:00Z"),
        # Fri 10:00; Thu 12-18 and Fri 09-10 are 7h; from Sat 14:00, Mon 09-16.
        ({"workdays": 2, "calendar": "ru"}, "2026-10-09T07:00:00Z", 7, "2026-10-12T13:00:00Z"),
    ],
    ids=["workhours", "workdays"],
)
async def test_a_pause_over_the_weekend_keeps_its_remainder_through_a_worker_restart(
    client: httpx.AsyncClient,
    settings: Settings,
    sync_engine: Engine,
    still: Still,
    due: dict[str, Any],
    declared: str,
    remaining: int,
    resumed: str,
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _calendar(client, key, RU_SPEC)
    await _publish(client, key, "sample-deadline", _flow(s["admin"], due))
    instance = await _start(client, key, "sample-deadline")
    iid = instance["id"]
    [entered] = await _of(client, key, "process.step_entered", iid)
    assert _time(entered["payload"]["due"]) == _time(declared)

    still.at = PAUSED
    await _command(client, key, iid, "suspend")
    assert _sla_timer(sync_engine, iid) == ("frozen", None, remaining * 3600, "working_seconds")

    # The declared deadline passes during the pause: nothing is due, nothing breached.
    still.at = _time(declared) + timedelta(hours=1)
    first = Worker(settings)
    assert await first.process_timers() == 0
    await first.engine.dispose()
    assert not await _of(client, key, "process.sla_breached", iid)

    # A new worker, and nothing the old one held in memory: the remainder is in the database.
    process_instances._definitions.clear()
    process_instances._calendars.clear()
    still.at = RESUMED
    await _command(client, key, iid, "resume")
    assert _sla_timer(sync_engine, iid) == ("pending", _time(resumed), None, "wall")
    [rescheduled] = await _of(client, key, "process.timer_rescheduled", iid)
    payload = rescheduled["payload"]
    assert (_time(payload["previousDueAt"]), _time(payload["dueAt"])) == (
        _time(declared),
        _time(resumed),
    )
    assert payload["cause"] == "resumed"

    still.at = _time(resumed) + timedelta(seconds=1)
    second = Worker(settings)
    assert await second.process_timers() == 1
    await second.engine.dispose()
    [breached] = await _of(client, key, "process.sla_breached", iid)
    assert _time(breached["payload"]["dueAt"]) == _time(resumed)


async def test_a_deadline_from_the_data_does_not_move_with_a_pause(
    client: httpx.AsyncClient, sync_engine: Engine, still: Still
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(
        client, key, "sample-deadline", _flow(s["admin"], {"at": "timestamp(data.note)"})
    )
    started = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-deadline", "key": "D-1", "data": {"note": "2026-10-09T07:00:00Z"}},
        headers=auth(key),
    )
    assert started.status_code == 201, started.text
    iid = started.json()["id"]
    declared = datetime(2026, 10, 9, 7, 0, tzinfo=UTC)
    assert _sla_timer(sync_engine, iid) == ("pending", declared, None, "wall")
    still.at = PAUSED
    await _command(client, key, iid, "suspend")
    assert _sla_timer(sync_engine, iid) == ("frozen", None, None, "wall")
    still.at = RESUMED
    await _command(client, key, iid, "resume")
    assert _sla_timer(sync_engine, iid) == ("pending", declared, None, "wall")


async def test_a_new_calendar_version_recounts_a_frozen_remainder(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine, still: Still
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _calendar(client, key, RU_SPEC)
    await _publish(
        client, key, "sample-deadline", _flow(s["admin"], {"workhours": 16, "calendar": "ru"})
    )
    iid = (await _start(client, key, "sample-deadline"))["id"]
    still.at = PAUSED
    await _command(client, key, iid, "suspend")
    await worker.run_once()
    # Wed 7 Oct becomes a day off: the 16h run Thu 09-18 and Fri 09-16, and
    # Thu 12-18 and Fri 09-16 are the 13h left.
    moved = copy.deepcopy(RU_SPEC)
    [year] = [y for y in moved["years"] if y["year"] == 2026]
    year["holidays"].append("2026-10-07")
    await _calendar(client, key, moved)
    await worker.run_once()
    assert _sla_timer(sync_engine, iid) == ("frozen", None, 13 * 3600, "working_seconds")
    still.at = RESUMED
    await _command(client, key, iid, "resume")
    # From Sat 14:00: Mon 09-18 is 9h, Tue 09-13 the last 4h.
    assert _sla_timer(sync_engine, iid)[:2] == ("pending", datetime(2026, 10, 13, 10, tzinfo=UTC))


# --- the projection and the slaState filter (CP-ADR-0078 §6; P014) ------------------


async def _conforms(client: httpx.AsyncClient, name: str, body: Any) -> None:
    """A response body against its schema in the service's own ``/openapi.json``."""
    document = (await client.get("/openapi.json")).json()
    schema = {"$ref": f"#/components/schemas/{name}", "components": document["components"]}
    errors = Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(body)
    assert [f"{list(e.absolute_path)}: {e.message}" for e in errors] == [], name


async def _found(client: httpx.AsyncClient, key: str, sla_state: str) -> list[str]:
    response = await client.get(
        "/api/v1/process-instances", params={"slaState": sla_state}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    page = response.json()
    for item in page["items"]:
        await _conforms(client, "ProcessInstanceOut", item)
    return [item["id"] for item in page["items"]]


def _columns(sync_engine: Engine, instance_id: str) -> tuple[Any, ...]:
    with sync_engine.connect() as conn:
        row = conn.execute(
            text("SELECT sla_due_at, sla_warn_at FROM process_instances WHERE id = :i"),
            {"i": instance_id},
        ).one()
    return tuple(row)


async def test_the_projection_is_breached_once_the_deadline_passed_without_the_worker(
    client: httpx.AsyncClient, clock: Clock, sync_engine: Engine
) -> None:
    """FR-021: no timer loop runs here; the projection and the filter read the clock."""
    s = await _setup(client)
    key = s["key"]
    spec = _flow(s["admin"], {"duration": "PT1H", "warnBefore": "PT15M"})
    spec["due"] = {"duration": "PT3H"}
    await _publish(client, key, "sample-deadline", spec)
    plain_spec = _flow(s["admin"], None)
    del plain_spec["stages"][0]["steps"][0]["human"]["due"]
    await _publish(client, key, "sample-plain", plain_spec)
    instance = await _start(client, key, "sample-deadline")
    iid = instance["id"]
    plain = await _start(client, key, "sample-plain")
    [entered] = await _of(client, key, "process.step_entered", iid)
    declared = _time(entered["payload"]["due"])
    started = _time(entered["payload"]["enteredAt"])

    await _conforms(client, "ProcessInstanceOut", instance)
    review = _open(instance, "review")
    assert (review["attempt"], review["slaState"], review["overdueSeconds"]) == (1, "ok", None)
    assert _time(review["due"]["dueAt"]) == declared
    assert _time(review["due"]["warnAt"]) == declared - timedelta(minutes=15)
    assert 3500 < review["due"]["remainingSeconds"] <= 3600
    assert instance["slaState"] == "ok"
    assert _time(instance["sla"]["dueAt"]) == started + timedelta(hours=3)
    assert instance["sla"]["warnAt"] is None
    # A step without a deadline, an instance without one.
    assert _open(plain, "review")["slaState"] == "none"
    assert (_open(plain, "review")["due"], plain["sla"], plain["slaState"]) == (None, None, "none")
    assert _columns(sync_engine, iid) == (declared, declared - timedelta(minutes=15))
    assert _columns(sync_engine, plain["id"]) == (None, None)
    assert await _found(client, key, "breached") == []
    assert await _found(client, key, "warning") == []

    clock.shift = timedelta(minutes=46)
    live = await _instance(client, key, iid)
    assert (_open(live, "review")["slaState"], live["slaState"]) == ("warning", "warning")
    assert await _found(client, key, "warning") == [iid]
    assert await _found(client, key, "breached") == []

    clock.shift = timedelta(hours=1, seconds=30)
    live = await _instance(client, key, iid)
    await _conforms(client, "ProcessInstanceOut", live)
    review = _open(live, "review")
    assert review["slaState"] == "breached"
    assert 30 <= review["overdueSeconds"] < 60
    assert review["due"]["remainingSeconds"] is None
    # The process deadline is still in time; the instance shows the worst.
    assert live["slaState"] == "breached"
    assert await _found(client, key, "breached") == [iid]
    assert await _found(client, key, "warning") == []
    # Nothing was fired: the breach is the projection's, not the worker's.
    assert not await _of(client, key, "process.sla_breached", iid)


async def test_a_suspended_instance_is_paused_and_out_of_the_filter(
    client: httpx.AsyncClient, clock: Clock, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    spec = _flow(s["admin"], {"duration": "PT1H", "warnBefore": "PT15M"})
    await _publish(client, key, "sample-deadline", spec)
    iid = (await _start(client, key, "sample-deadline"))["id"]
    clock.shift = timedelta(minutes=50)
    assert await _found(client, key, "warning") == [iid]

    await _command(client, key, iid, "suspend")
    assert _columns(sync_engine, iid) == (None, None)
    clock.shift = timedelta(hours=2)
    live = await _instance(client, key, iid)
    await _conforms(client, "ProcessInstanceOut", live)
    review = _open(live, "review")
    assert (review["slaState"], live["slaState"]) == ("paused", "paused")
    assert (review["due"]["dueAt"], review["due"]["warnAt"]) == (None, None)
    assert 590 <= review["due"]["remainingSeconds"] <= 600
    assert review["due"]["remainingUnit"] == "wall"
    assert await _found(client, key, "breached") == []
    assert await _found(client, key, "warning") == []

    await _command(client, key, iid, "resume")
    due_at, warn_at = _columns(sync_engine, iid)
    assert due_at is not None and warn_at is not None
    live = await _instance(client, key, iid)
    assert _open(live, "review")["slaState"] == "warning"
    assert await _found(client, key, "warning") == [iid]


async def test_a_deadline_passed_before_the_pause_stays_breached_while_suspended(
    client: httpx.AsyncClient, clock: Clock, sync_engine: Engine
) -> None:
    """FR-021: past ``dueAt`` by the clock, not fired by the worker, then suspended.

    The timer froze with nothing left: the deadline is read by the clock, not
    as ``paused`` (CP-ADR-0078 §6), and stays in the filter columns. Its
    overdue clock stands with the instance (``overdueStops``, §4 P016 attempt
    4): overdue up to the suspension, not growing during it, and running on
    after the resume.
    """
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, "sample-deadline", _flow(s["admin"], {"duration": "PT1H"}))
    iid = (await _start(client, key, "sample-deadline"))["id"]
    declared = _time(_open(await _instance(client, key, iid), "review")["due"]["dueAt"])
    clock.shift = timedelta(hours=1, minutes=30)
    await _command(client, key, iid, "suspend")
    assert _columns(sync_engine, iid) == (declared, None)

    clock.shift = timedelta(hours=2)
    live = await _instance(client, key, iid)
    await _conforms(client, "ProcessInstanceOut", live)
    assert live["status"] == "suspended"
    review = _open(live, "review")
    assert (review["slaState"], live["slaState"]) == ("breached", "breached")
    assert _time(review["due"]["dueAt"]) == declared
    assert 1800 - 60 <= review["overdueSeconds"] <= 1800
    assert await _found(client, key, "breached") == [iid]
    assert not await _of(client, key, "process.sla_breached", iid)

    clock.shift = timedelta(hours=3)
    later = _open(await _instance(client, key, iid), "review")
    assert later["overdueSeconds"] == review["overdueSeconds"]

    await _command(client, key, iid, "resume")
    clock.shift = timedelta(hours=3, minutes=30)
    resumed = _open(await _instance(client, key, iid), "review")
    assert resumed["slaState"] == "breached"
    assert 1800 <= resumed["overdueSeconds"] - review["overdueSeconds"] <= 1800 + 60


async def test_a_deadline_that_failed_is_unknown_in_the_projection(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(
        client, key, "sample-deadline", _flow(s["admin"], {"at": "timestamp(data.note)"})
    )
    instance = await _start(client, key, "sample-deadline")
    await _conforms(client, "ProcessInstanceOut", instance)
    review = _open(instance, "review")
    assert (review["slaState"], review["due"]["dueAt"]) == ("unknown", None)
    assert instance["slaState"] == "unknown"
    assert _columns(sync_engine, instance["id"]) == (None, None)


async def test_a_deadline_that_runs_on_while_suspended_is_read_by_the_clock(
    client: httpx.AsyncClient, worker: Worker, clock: Clock, sync_engine: Engine
) -> None:
    """Clocks stand per thread (CP-ADR-0078 §4): the step of a correlate block runs on.

    Its deadline passes while the instance is suspended: the projection shows
    it ``breached`` with the overdue time and the filter finds the instance,
    before and after the worker records the breach (FR-021). The deadline of
    the stage's step stands meanwhile and is ``paused``.
    """
    s = await _setup(client)
    key = s["key"]
    spec = _flow(s["admin"], {"duration": "PT3H"})
    spec["correlate"] = [
        {
            "on": {"observation": "sample.ping"},
            "key": "event.payload.data.number",
            "do": [
                {
                    "id": "ask",
                    "human": {
                        "taskType": "review",
                        "assign": [{"principal": s["admin"]}],
                        "due": {"duration": "PT1H"},
                    },
                }
            ],
        }
    ]
    await _publish(client, key, "sample-deadline", spec)
    iid = (await _start(client, key, "sample-deadline"))["id"]
    await _command(client, key, iid, "suspend")
    await _observe(client, key, "sample.ping", number="D-1")
    await worker.run_once()
    live = await _instance(client, key, iid)
    assert live["status"] == "suspended"
    ask, review = _open(live, "ask"), _open(live, "review")
    assert (ask["slaState"], review["slaState"]) == ("ok", "paused")
    assert ask["due"]["remainingUnit"] == "wall"
    assert (review["due"]["remainingSeconds"] > 3 * 3600 - 60, review["due"]["remainingUnit"]) == (
        True,
        "wall",
    )
    declared = _time(ask["due"]["dueAt"])
    # The running deadline is in the filter columns; the frozen one is not.
    assert _columns(sync_engine, iid) == (declared, None)

    clock.shift = timedelta(hours=1, seconds=30)
    for recorded in (False, True):
        live = await _instance(client, key, iid)
        await _conforms(client, "ProcessInstanceOut", live)
        ask = _open(live, "ask")
        assert (ask["slaState"], live["slaState"]) == ("breached", "breached")
        assert 30 <= ask["overdueSeconds"] < 90
        assert _open(live, "review")["slaState"] == "paused"
        assert await _found(client, key, "breached") == [iid]
        assert bool(await _of(client, key, "process.sla_breached", iid)) is recorded
        if not recorded:
            assert await worker.process_timers() == 1
    assert _columns(sync_engine, iid) == (declared, None)
