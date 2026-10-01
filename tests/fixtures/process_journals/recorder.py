"""Recorder of the replay fixtures (process-observability P018): runs on ``main``, not here.

The fixtures of this directory are journals of process instances as a core
without the feature wrote them: versions of engine revision 1 with ``due``
and escalations, a suspension, a migration by a package apply. The feature's
code must replay them with zero divergences (FR-028, FR-031, SC-009) —
``tests/unit/test_process_replay_fixtures.py`` and
``tests/integration/test_process_replay_gate.py``.

This module is not a test of this branch (pytest does not collect it here):
copy it into ``tests/integration/`` of a checkout of ``main`` and run it
there against the test database — README.md of this directory. Every test
drives one process through the public API and the worker, as in production,
and writes what the core stored of it — calendars, process versions,
instances and their journals, row by row — into ``$PROCESS_JOURNALS_OUT``.
Only the helpers ``main`` already has are used.
"""

import asyncio
import copy
import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.engine import Engine
from tests.helpers import auth
from tests.integration.test_process_instances import (
    AGENT,
    _complete,
    _instance,
    _observe,
    _open,
    _publish,
    _setup,
    worker,
)

from control_plane.worker.main import Worker

__all__ = ["worker"]

OUT = os.environ.get("PROCESS_JOURNALS_OUT")
COMMIT = os.environ.get("PROCESS_JOURNALS_COMMIT", "unknown")

pytestmark = pytest.mark.skipif(not OUT, reason="PROCESS_JOURNALS_OUT is not set")

# The catalog format of the fixtures, as main's own tests take it.
API_VERSION = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / "fixtures" / "processes" / "ru.calendar.yaml").read_text(
        encoding="utf-8"
    )
)["apiVersion"]

# A working-day calendar for the years the recording runs in; 2027 is provisional.
CALENDAR: dict[str, Any] = {
    "displayName": "Production calendar (replay fixtures)",
    "timezone": "Europe/Moscow",
    "weekend": [6, 7],
    "years": [
        {
            "year": 2026,
            "source": "test data of the replay fixtures",
            "holidays": [
                "2026-01-01",
                "2026-01-02",
                "2026-01-05",
                "2026-01-06",
                "2026-01-07",
                "2026-01-08",
                "2026-01-09",
                "2026-02-23",
                "2026-03-09",
                "2026-05-01",
                "2026-05-11",
                "2026-06-12",
                "2026-11-04",
                "2026-12-31",
            ],
            "workdays": [],
            "shortDays": ["2026-11-03"],
        },
        {"year": 2027, "provisional": True, "holidays": ["2027-01-01"]},
    ],
}


def _person(admin: str) -> list[dict[str, Any]]:
    return [{"principal": admin}]


def _due_spec(admin: str) -> dict[str, Any]:
    """A review due two working days before the deadline, then a signature due by it.

    The review escalates at its due (``notify``); the signature notifies at
    the deadline and raises a second later, caught to close ``overdue``.
    """
    person = _person(admin)
    return {
        "version": 1,
        "displayName": "Replay: due and escalations",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "calendar": "ru",
        "data": {
            "type": "object",
            "properties": {
                "number": {"type": "string"},
                "deadline": {"type": "string", "format": "date-time"},
                "decision": {"type": "string"},
            },
        },
        "start": {
            "on": {"observation": "sample.opened"},
            "key": "event.payload.data.number",
            "set": {
                "number": "string(event.payload.data.number)",
                "deadline": "timestamp(event.payload.data.deadline)",
            },
        },
        "correlate": [
            {
                "on": {"observation": "sample.moved"},
                "key": "event.payload.data.number",
                "set": {"deadline": "timestamp(event.payload.data.deadline)"},
            }
        ],
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "review",
                        "human": {
                            "taskType": "review",
                            "assign": person,
                            "due": {"at": "cal.addWorkdays(data.deadline, -2)"},
                            "escalations": [{"after": "due", "action": "notify", "to": person}],
                        },
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {
                        "id": "guard",
                        "try": {
                            "do": [
                                {
                                    "id": "sign",
                                    "human": {
                                        "taskType": "review",
                                        "assign": person,
                                        "due": {"at": "data.deadline"},
                                        "escalations": [
                                            {"after": "due", "action": "notify", "to": person},
                                            {
                                                "after": "PT1S",
                                                "action": "raise",
                                                "error": {"type": "overdue"},
                                            },
                                        ],
                                    },
                                }
                            ],
                            "catch": [
                                {
                                    "errors": {"type": "overdue"},
                                    "do": [
                                        {"id": "closed-overdue", "complete": {"outcome": "overdue"}}
                                    ],
                                }
                            ],
                        },
                    },
                    {"id": "done", "complete": {"outcome": "signed"}},
                ],
            }
        ],
    }


def _duration_spec(admin: str) -> dict[str, Any]:
    """A step due in two seconds with a notify escalation: a timer the worker fires live."""
    person = _person(admin)
    return {
        "version": 1,
        "displayName": "Replay: a duration due",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "data": {"type": "object", "properties": {"decision": {"type": "string"}}},
        "start": {"on": {"observation": "sample.quick"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": "ack",
                        "human": {
                            "taskType": "review",
                            "assign": person,
                            "due": "PT2S",
                            "escalations": [
                                {"after": "due", "action": "notify", "to": person},
                                {"after": "PT1H", "action": "notify", "to": person},
                            ],
                        },
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {"id": "done", "complete": {"outcome": "acknowledged"}},
                ],
            }
        ],
    }


def _migrate_spec(admin: str, version: int) -> dict[str, Any]:
    """Version 1 reviews in ``P3D``; version 2 renames the step and makes it ``P1D``."""
    person = _person(admin)
    step = "review" if version == 1 else "check"
    escalations: list[dict[str, Any]] = [{"after": "due", "action": "notify", "to": person}]
    if version == 2:
        escalations.append({"after": "PT4H", "action": "notify", "to": person})
    spec: dict[str, Any] = {
        "version": version,
        "displayName": "Replay: migration",
        "identity": {"agent": AGENT},
        "owner": [{"role": "lead"}],
        "calendar": "ru",
        "data": {"type": "object", "properties": {"decision": {"type": "string"}}},
        "start": {"on": {"observation": "sample.migrated"}, "key": "event.payload.data.number"},
        "stages": [
            {
                "id": "work",
                "steps": [
                    {
                        "id": step,
                        "human": {
                            "taskType": "review",
                            "assign": person,
                            "due": "P3D" if version == 1 else "P1D",
                            "escalations": escalations,
                        },
                        "output": {"as": {"decision": "step.result.decision"}},
                    },
                    {"id": "done", "complete": {"outcome": "reviewed"}},
                ],
            }
        ],
    }
    if version == 2:
        spec["migrations"] = [{"from": 1, "to": 2, "policy": "migrate", "map": {"review": "check"}}]
    return spec


def _package(spec: dict[str, Any]) -> dict[str, Any]:
    manifest = {
        "apiVersion": API_VERSION,
        "kind": "Package",
        "key": "replay-fixtures",
        "spec": {"version": f"1.0.{spec['version']}", "displayName": "Replay fixtures"},
    }
    process = {"apiVersion": API_VERSION, "kind": "Process", "key": "replay-migrate", "spec": spec}
    calendar = {"apiVersion": API_VERSION, "kind": "Calendar", "key": "ru", "spec": CALENDAR}
    files = [
        ("package.yaml", manifest),
        ("processes/replay-migrate.yaml", process),
        ("calendars/ru.yaml", calendar),
    ]
    return {
        "files": [
            {"path": path, "content": yaml.safe_dump(body, sort_keys=False)} for path, body in files
        ]
    }


# --- driving -------------------------------------------------------------------------------


async def _post(client: httpx.AsyncClient, key: str, path: str, body: Any) -> dict[str, Any]:
    response = await client.post(f"/api/v1{path}", json=body, headers=auth(key))
    assert response.status_code in (200, 201), response.text
    out: dict[str, Any] = response.json()
    return out


async def _by_key(client: httpx.AsyncClient, key: str, process: str) -> dict[str, dict[str, Any]]:
    response = await client.get(
        "/api/v1/process-instances",
        params={"definitionKey": process, "limit": 200},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    return {i["instanceKey"]: i for i in response.json()["items"]}


async def _finish(
    client: httpx.AsyncClient, key: str, worker: Worker, instance_id: str, element: str
) -> dict[str, Any]:
    instance = await _instance(client, key, instance_id)
    await _complete(client, key, _open(instance, element)["taskId"], {"decision": "yes"})
    await worker.run_once()
    return await _instance(client, key, instance_id)


def _at(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


# --- what the core stored ------------------------------------------------------------------


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _rows(sync_engine: Engine, sql: str, **params: Any) -> list[dict[str, Any]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [{k: _plain(v) for k, v in row.items()} for row in rows]


def _dump(sync_engine: Engine, name: str, process: str, story: dict[str, str]) -> None:
    """Write what the core stored of ``process`` into ``$PROCESS_JOURNALS_OUT/<name>.json``."""
    assert OUT
    [tenant] = _rows(sync_engine, "SELECT id FROM tenants")
    tenant_id = tenant["id"]
    definitions = _rows(
        sync_engine,
        "SELECT id, key, version, display_name, definition_hash, identity_agent,"
        " expression_profile, spec, governed_by, warnings, created_at"
        " FROM process_definitions WHERE tenant_id = :t AND key = :k ORDER BY version",
        t=tenant_id,
        k=process,
    )
    instances = _rows(
        sync_engine,
        "SELECT id, definition_id, definition_key, definition_version, instance_key, status,"
        " outcome, error, data, state, refs, started_at, updated_at, completed_at"
        " FROM process_instances WHERE tenant_id = :t AND definition_key = :k"
        " ORDER BY instance_key",
        t=tenant_id,
        k=process,
    )
    for instance in instances:
        instance["story"] = story[instance["instance_key"]]
        instance["journal"] = _rows(
            sync_engine,
            "SELECT seq, at, kind, source_ref, event_id, actor_id, input, decisions, intents,"
            " calendars, created_at FROM process_instance_events"
            " WHERE instance_id = :i ORDER BY seq",
            i=instance["id"],
        )
    calendars = _rows(
        sync_engine,
        "SELECT key, version, calendar_hash, spec, created_at FROM calendars"
        " WHERE tenant_id = :t ORDER BY key, version",
        t=tenant_id,
    )
    task_types = _rows(
        sync_engine,
        "SELECT DISTINCT ON (key) key, version, display_name, field_schema FROM task_types"
        " WHERE tenant_id = :t ORDER BY key, version DESC",
        t=tenant_id,
    )
    fixture = {
        "recordedWith": {"ref": "main", "commit": COMMIT},
        "process": process,
        "catalog": {"agents": [AGENT], "taskTypes": task_types},
        "calendars": calendars,
        "definitions": definitions,
        "instances": instances,
    }
    path = Path(OUT) / f"{name}.json"
    path.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


# --- the recordings ------------------------------------------------------------------------


async def test_record_due_and_escalations(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    process = "replay-due"
    await _post(client, key, "/calendars", {"key": "ru", "spec": CALENDAR})
    await _publish(client, key, process, _due_spec(admin))
    story = {
        "D-on-time": "both steps done before their due",
        "D-overdue": "the deadline moved into the past: notify, raise, caught, closed overdue",
        "D-review-late": "the review is past its working-day due at start: notify, then done",
        "D-suspended": "suspended and resumed on the signature, then signed",
        "D-held": "suspended on the signature and left so",
        "D-cancelled": "cancelled by the operator on the review",
        "D-calendar": "a new calendar version moves the review's due, then done",
    }
    later = timedelta(days=30)
    for number in story:
        deadline = _at(-timedelta(days=10) if number == "D-review-late" else later)
        await _observe(client, key, "sample.opened", number=number, deadline=deadline)
    await worker.run_once()
    ids = {k: v["id"] for k, v in (await _by_key(client, key, process)).items()}
    assert set(ids) == set(story)

    # Done on time.
    await _finish(client, key, worker, ids["D-on-time"], "review")
    done = await _finish(client, key, worker, ids["D-on-time"], "sign")
    assert done["outcome"] == "signed"

    # The deadline moves into the past: both escalations of the signature fire.
    await _finish(client, key, worker, ids["D-overdue"], "review")
    await _observe(
        client, key, "sample.moved", number="D-overdue", deadline=_at(-timedelta(hours=1))
    )
    await worker.run_once()
    await worker.run_once()
    overdue = await _instance(client, key, ids["D-overdue"])
    assert (overdue["status"], overdue["outcome"]) == ("completed", "overdue")

    # The review's due is past from the start: its escalation fires, the review stays open.
    await worker.run_once()
    await _finish(client, key, worker, ids["D-review-late"], "review")
    await worker.run_once()
    await worker.run_once()

    # Suspended and resumed on the signature, then signed.
    path = f"/process-instances/{ids['D-suspended']}"
    await _finish(client, key, worker, ids["D-suspended"], "review")
    await _post(client, key, f"{path}:suspend", {"reason": "hold"})
    await asyncio.sleep(1)
    await _post(client, key, f"{path}:resume", {})
    await worker.run_once()
    signed = await _finish(client, key, worker, ids["D-suspended"], "sign")
    assert signed["outcome"] == "signed"

    # Suspended and left so: the timers stay frozen.
    await _finish(client, key, worker, ids["D-held"], "review")
    await _post(client, key, f"/process-instances/{ids['D-held']}:suspend", {"reason": "hold"})

    # Cancelled on the review.
    await _post(
        client,
        key,
        f"/process-instances/{ids['D-cancelled']}:cancel",
        {"reason": "withdrawn", "compensate": False},
    )

    # A new calendar version with holidays just before the deadline moves the
    # working-day due of the review (two working days before the deadline).
    changed = copy.deepcopy(CALENDAR)
    today = datetime.now(UTC).date()
    added = [(today + timedelta(days=n)).isoformat() for n in range(24, 30)]
    changed["years"] = [
        {
            **year,
            "holidays": sorted(
                {*year["holidays"], *(d for d in added if d[:4] == str(year["year"]))}
            ),
        }
        for year in changed["years"]
    ]
    await _post(client, key, "/calendars", {"key": "ru", "spec": changed})
    await worker.run_once()
    await worker.run_once()
    await _finish(client, key, worker, ids["D-calendar"], "review")

    _dump(sync_engine, "due-and-escalations", process, story)


async def test_record_a_duration_due_fired_live(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    process = "replay-duration"
    await _publish(client, key, process, _duration_spec(admin))
    story = {
        "Q-escalated": "the due of two seconds passes: notify, then acknowledged",
        "Q-quick": "acknowledged before the due",
    }
    for number in story:
        await _observe(client, key, "sample.quick", number=number)
    await worker.run_once()
    ids = {k: v["id"] for k, v in (await _by_key(client, key, process)).items()}
    await _finish(client, key, worker, ids["Q-quick"], "ack")
    await asyncio.sleep(3)
    await worker.run_once()
    await _finish(client, key, worker, ids["Q-escalated"], "ack")

    _dump(sync_engine, "duration-due", process, story)


async def test_record_a_migration_by_a_package_apply(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    process = "replay-migrate"

    async def apply(version: int) -> None:
        package = _package(_migrate_spec(admin, version))
        plan = await _post(client, key, "/packages:plan", {"package": package})
        await _post(
            client, key, "/packages:apply", {"package": package, "planHash": plan["planHash"]}
        )

    await apply(1)
    story = {
        "M-done-after": "migrated onto the renamed step, then done on version 2",
        "M-open": "migrated and left waiting on the renamed step",
        "M-suspended": "migrated, suspended and resumed on version 2",
        "M-done-before": "done on version 1 before the migration",
    }
    for number in story:
        await _post(client, key, "/process-instances", {"process": process, "key": number})
    await worker.run_once()
    ids = {k: v["id"] for k, v in (await _by_key(client, key, process)).items()}
    await _finish(client, key, worker, ids["M-done-before"], "review")

    await apply(2)
    await worker.run_once()
    for number in ("M-done-after", "M-open", "M-suspended"):
        assert (await _instance(client, key, ids[number]))["definitionVersion"] == 2
    await _finish(client, key, worker, ids["M-done-after"], "check")
    path = f"/process-instances/{ids['M-suspended']}"
    await _post(client, key, f"{path}:suspend", {"reason": "hold"})
    await _post(client, key, f"{path}:resume", {})
    await worker.run_once()

    _dump(sync_engine, "migration", process, story)
