"""Processes and calendars out of use (CP-ADR-0074, amendment 2026-09-29, Zh1-Zh5; S012).

The acceptance of S012: a retired process starts no new instance — neither
by its start event, nor ``POST /process-instances``, nor ``call`` — while its
open instances run to the end; a calendar a process still needs is not
retired (``calendar_in_use``). A new version brings the key back.
"""

import copy
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from alembic import command as alembic_command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key
from tests.integration.test_calendars import DOCUMENT as CALENDAR
from tests.integration.test_process_instances import (
    GOAL,
    _events,
    _instance,
    _instances,
    _journal,
    _observe,
    _publish,
    _setup,
)

PROCESS = "sample-goal"
# The revision right before the retirements (the merge of process observability
# with the package links, CP-ADR-0078 and CP-ADR-0074).
BEFORE_RETIREMENTS = "16a16d12fe3f"


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _retire(
    client: httpx.AsyncClient, key: str, path: str, reason: str = "replaced", **params: Any
) -> httpx.Response:
    return await client.post(
        f"/api/v1/{path}:retire", json={"reason": reason}, params=params, headers=auth(key)
    )


async def _start(client: httpx.AsyncClient, key: str, instance_key: str) -> httpx.Response:
    return await client.post(
        "/api/v1/process-instances",
        json={"process": PROCESS, "key": instance_key, "data": {"open": 2}},
        headers=auth(key),
    )


async def _definition(client: httpx.AsyncClient, key: str, ref: str = PROCESS) -> dict[str, Any]:
    response = await client.get(f"/api/v1/process-definitions/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _listed(client: httpx.AsyncClient, key: str, path: str, status: str) -> list[str]:
    response = await client.get(f"/api/v1/{path}", params={"status": status}, headers=auth(key))
    assert response.status_code == 200, response.text
    return [item["key"] for item in response.json()["items"]]


# --- processes (Zh2) -------------------------------------------------------------------------


async def test_a_retired_process_starts_nothing_and_its_open_instances_run_on(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, PROCESS, GOAL)
    explicit = await _start(client, key, "goal-1")
    assert explicit.status_code == 201, explicit.text
    await _observe(client, key, "sample.goal", goal="goal-2")
    await worker.run_once()
    assert {i["instanceKey"] for i in await _instances(client, key)} == {"goal-1", "goal-2"}

    _, writer = await create_agent_with_key(
        client, key, name="operator", permissions=["processes.read", "processes.operate"]
    )
    denied = await _retire(client, writer, f"process-definitions/{PROCESS}")
    assert denied.status_code == 403, denied.text
    unknown = await _retire(client, key, "process-definitions/nothing")
    assert unknown.status_code == 404, unknown.text

    # A dry run answers the same and writes nothing.
    dry = await _retire(client, key, f"process-definitions/{PROCESS}", dryRun="true")
    assert dry.status_code == 200, dry.text
    assert (dry.json()["openInstances"], dry.json()["status"]) == (2, "retired")
    assert (await _definition(client, key))["status"] == "active"
    assert await _events(client, key, "process.definition_retired") == []

    retired = await _retire(client, key, f"process-definitions/{PROCESS}", reason="goal moved")
    assert retired.status_code == 200, retired.text
    body = retired.json()
    assert body["key"] == PROCESS
    assert body["status"] == "retired"
    assert (body["retired"]["by"], body["retired"]["reason"]) == (s["admin"], "goal moved")
    assert body["openInstances"] == 2
    assert body["byVersion"] == [{"version": 1, "openInstances": 2}]
    [event] = await _events(client, key, "process.definition_retired")
    assert event["entityType"] == "process_definition"
    assert event["payload"] == {
        "key": PROCESS,
        "latestVersion": 1,
        "workspaceId": None,
        "reason": "goal moved",
        "openInstances": 2,
        "byVersion": [{"version": 1, "openInstances": 2}],
    }

    # Again: the first retirement, no second event.
    again = await _retire(client, key, f"process-definitions/{PROCESS}", reason="other")
    assert again.status_code == 200, again.text
    assert again.json()["retired"] == body["retired"]
    assert len(await _events(client, key, "process.definition_retired")) == 1

    # Every version shows it; the lists filter by it.
    definition = await _definition(client, key)
    assert (definition["status"], definition["retired"]) == ("retired", body["retired"])
    assert (await _definition(client, key, f"{PROCESS}@1"))["status"] == "retired"
    versions = await client.get(
        f"/api/v1/process-definitions/{PROCESS}/versions", headers=auth(key)
    )
    assert [v["status"] for v in versions.json()["items"]] == ["retired"]
    assert await _listed(client, key, "process-definitions", "retired") == [PROCESS]
    assert await _listed(client, key, "process-definitions", "active") == []

    # No new instance: neither by the route nor by the start event.
    refused = await _start(client, key, "goal-3")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "process_retired"
    assert refused.json()["error"]["details"] == {"process": PROCESS}
    await _observe(client, key, "sample.goal", goal="goal-4")
    await worker.run_once()
    assert {i["instanceKey"] for i in await _instances(client, key)} == {"goal-1", "goal-2"}

    # The open ones run on: their correlations still reach them.
    for goal in ("goal-1", "goal-2"):
        await _observe(client, key, "sample.count", goal=goal, open=0)
    await worker.run_once()
    reached = await _events(client, key, "process.milestone_reached")
    assert sorted(e["payload"]["instanceKey"] for e in reached) == ["goal-1", "goal-2"]
    instance = await _instance(client, key, explicit.json()["id"])
    assert (instance["status"], instance["data"]) == ("running", {"open": 0})
    # The operator's commands still work on them.
    cancelled = await client.post(
        f"/api/v1/process-instances/{instance['id']}:cancel",
        json={"reason": "done by hand"},
        headers=auth(key),
    )
    assert cancelled.status_code == 200, cancelled.text

    # A new version brings the key back.
    await _publish(client, key, PROCESS, {**GOAL, "version": 2})
    definition = await _definition(client, key)
    assert (definition["status"], definition["retired"]) == ("active", None)
    assert (await _start(client, key, "goal-3")).status_code == 201


def _parent(**step: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "displayName": "Sample parent",
        "identity": {"agent": "sample-process"},
        "owner": [{"role": "lead"}],
        "data": {"type": "object", "properties": {}},
        "start": {"on": {"observation": "sample.parent"}, "key": "event.payload.data.id"},
        "stages": [
            {
                "id": "run",
                "steps": [
                    {"id": "child", "call": {"process": PROCESS}, **step},
                    {"id": "done", "complete": {"outcome": "done"}},
                ],
            }
        ],
    }


async def test_a_call_of_a_retired_process_fails_the_parent_at_run_time(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, PROCESS, GOAL)
    assert (await _retire(client, key, f"process-definitions/{PROCESS}")).status_code == 200

    # The check warns, the version is published.
    published = await client.post(
        "/api/v1/process-definitions",
        json={"key": "sample-parent", "spec": _parent()},
        headers=auth(key),
    )
    assert published.status_code == 201, published.text
    assert [(w["code"], w["path"], w["severity"]) for w in published.json()["warnings"]] == [
        ("process_retired", "/spec/stages/0/steps/0/call/process", "warning")
    ]

    started = await client.post(
        "/api/v1/process-instances",
        json={"process": "sample-parent", "key": "p-1"},
        headers=auth(key),
    )
    assert started.status_code == 201, started.text
    parent = await _instance(client, key, started.json()["id"])
    assert parent["status"] == "failed"
    [failed] = [
        e["data"]["error"]
        for e in await _journal(client, key, parent["id"], kind="error")
        if e["reason"] == "failed"
    ]
    assert failed == {"type": "intent_failed", "detail": "process_retired", "status": 409}
    assert [i["instanceKey"] for i in await _instances(client, key, definitionKey=PROCESS)] == []


# --- calendars (Zh3) -------------------------------------------------------------------------


async def test_a_calendar_a_process_needs_is_not_retired(client: httpx.AsyncClient) -> None:
    s = await _setup(client)
    key = s["key"]
    published = await client.post(
        "/api/v1/calendars", json={"key": "ru", "spec": CALENDAR["spec"]}, headers=auth(key)
    )
    assert published.status_code == 201, published.text
    await _publish(client, key, PROCESS, {**GOAL, "calendar": "ru"})
    started = await _start(client, key, "goal-1")
    assert started.status_code == 201, started.text

    _, writer = await create_agent_with_key(
        client, key, name="author", permissions=["processes.read", "processes.write"]
    )
    assert (await _retire(client, writer, "calendars/ru")).status_code == 403
    assert (await _retire(client, key, "calendars/nothing")).status_code == 404

    # The latest version of a process in use needs it.
    in_use = await _retire(client, key, "calendars/ru")
    assert in_use.status_code == 409, in_use.text
    error = in_use.json()["error"]
    assert error["code"] == "calendar_in_use"
    assert error["details"] == {
        "calendar": "ru",
        "processes": [{"key": PROCESS, "version": 1, "openInstances": 1}],
        "total": 1,
    }
    # So does an open instance of a retired process.
    assert (await _retire(client, key, f"process-definitions/{PROCESS}")).status_code == 200
    still = await _retire(client, key, "calendars/ru", dryRun="true")
    assert still.status_code == 409, still.text
    assert still.json()["error"]["details"]["processes"] == [
        {"key": PROCESS, "version": 1, "openInstances": 1}
    ]

    cancelled = await client.post(
        f"/api/v1/process-instances/{started.json()['id']}:cancel",
        json={"reason": "closing"},
        headers=auth(key),
    )
    assert cancelled.status_code == 200, cancelled.text
    dry = await _retire(client, key, "calendars/ru", dryRun="true")
    assert dry.status_code == 200, dry.text
    assert await _events(client, key, "calendar.retired") == []

    retired = await _retire(client, key, "calendars/ru", reason="old year")
    assert retired.status_code == 200, retired.text
    body = retired.json()
    assert (body["key"], body["status"], body["retired"]["reason"]) == ("ru", "retired", "old year")
    [event] = await _events(client, key, "calendar.retired")
    assert (event["entityType"], event["entityId"]) == ("calendar", published.json()["id"])
    assert event["payload"] == {"key": "ru", "latestVersion": 1, "reason": "old year"}
    again = await _retire(client, key, "calendars/ru")
    assert again.json()["retired"] == body["retired"]

    shown = await client.get("/api/v1/calendars/ru@1", headers=auth(key))
    assert (shown.json()["status"], shown.json()["retired"]) == ("retired", body["retired"])
    assert await _listed(client, key, "calendars", "retired") == ["ru"]
    assert await _listed(client, key, "calendars", "active") == []

    # A new process version may not use it again.
    refused = await client.post(
        "/api/v1/process-definitions",
        json={"key": "sample-other", "spec": {**GOAL, "calendar": "ru"}},
        headers=auth(key),
    )
    assert refused.status_code == 422, refused.text
    problems = refused.json()["error"]["details"]["problems"]
    assert [(p["code"], p["path"]) for p in problems] == [("calendar_retired", "/spec/calendar")]

    # A new calendar version brings it back.
    years = copy.deepcopy(CALENDAR["spec"]["years"])
    years[0]["holidays"] = [*years[0]["holidays"], "2024-06-13"]
    republished = await client.post(
        "/api/v1/calendars",
        json={"key": "ru", "spec": {**CALENDAR["spec"], "years": years}},
        headers=auth(key),
    )
    assert republished.status_code == 201, republished.text
    assert (republished.json()["status"], republished.json()["retired"]) == ("active", None)


async def test_calendar_in_use_hides_processes_the_caller_may_not_read(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    key = s["key"]
    assert (
        await client.post(
            "/api/v1/calendars", json={"key": "ru", "spec": CALENDAR["spec"]}, headers=auth(key)
        )
    ).status_code == 201
    await _publish(client, key, PROCESS, {**GOAL, "calendar": "ru"})
    _, keeper = await create_agent_with_key(
        client, key, name="calendars", permissions=["calendars.write"]
    )
    in_use = await _retire(client, keeper, "calendars/ru")
    assert in_use.status_code == 409, in_use.text
    assert in_use.json()["error"]["details"] == {"calendar": "ru", "processes": [], "total": 1}


# --- the migration ---------------------------------------------------------------------------


@pytest.fixture
def alembic_config(migrated_database: str) -> Any:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migrated_database.replace("+psycopg", ""))
    yield config
    alembic_command.upgrade(config, "head")


async def test_the_migration_moves_the_renamed_mark_and_goes_back(
    client: httpx.AsyncClient, sync_engine: Engine, alembic_config: Config
) -> None:
    s = await _setup(client)
    await _publish(client, s["key"], PROCESS, GOAL)
    with sync_engine.connect() as conn:
        tenant = conn.execute(text("SELECT id FROM tenants LIMIT 1")).scalar_one()
    alembic_command.downgrade(alembic_config, BEFORE_RETIREMENTS)
    assert "catalog_retirements" not in inspect(sync_engine).get_table_names()
    with sync_engine.begin() as conn:
        for name, retired in (("old-name", True), ("kept", False)):
            conn.execute(
                text(
                    "INSERT INTO package_objects (id, tenant_id, kind, key, package_key, version,"
                    " spec_hash, spec, plan_hash, applied_by, applied_at, retired_at)"
                    " VALUES (:id, :tenant, 'Process', :key, 'pkg', 1, 'sha256:x', '{}',"
                    " 'sha256:p', :by, now(), CASE WHEN :retired THEN now() END)"
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant": tenant,
                    "key": name,
                    "by": s["admin"],
                    "retired": retired,
                },
            )

    alembic_command.upgrade(alembic_config, "head")
    columns = {c["name"] for c in inspect(sync_engine).get_columns("package_objects")}
    assert "retired_at" not in columns
    with sync_engine.begin() as conn:
        rows = conn.execute(
            text("SELECT kind, key, retired_by, reason FROM catalog_retirements")
        ).all()
        assert [(r.kind, r.key, str(r.retired_by), r.reason) for r in rows] == [
            ("Process", "old-name", s["admin"], "renamed by package pkg")
        ]
        conn.execute(
            text(
                "INSERT INTO catalog_retirements VALUES"
                " (:tenant, 'Process', :key, now(), :by, 'goal moved')"
            ),
            {"tenant": tenant, "key": PROCESS, "by": s["admin"]},
        )

    # Down: the rename mark comes back to the column, a :retire is forgotten.
    alembic_command.downgrade(alembic_config, BEFORE_RETIREMENTS)
    with sync_engine.connect() as conn:
        marked = conn.execute(
            text("SELECT key FROM package_objects WHERE retired_at IS NOT NULL")
        ).all()
    assert [r.key for r in marked] == ["old-name"]
    alembic_command.upgrade(alembic_config, "head")
    started = await _start(client, s["key"], "goal-1")
    assert started.status_code == 201, started.text
