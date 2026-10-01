"""A retirement in flight and what it guards (CP-ADR-0074, amendment Zh2, Zh3).

A retirement holds the key lock while it counts what still needs the key. A
start of the process and a publication naming the calendar take that lock
shared before they read whether the key is retired: they wait for the
retirement and then see it, instead of slipping in after it has counted.
A package restoring a process shares its calendars the same way.
"""

import asyncio
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from tests.concurrency.test_principal_disable_races import _wait_until_blocked_by
from tests.helpers import auth
from tests.integration.test_calendars import DOCUMENT as CALENDAR
from tests.integration.test_package_plan import PROCESS as PLANNED
from tests.integration.test_package_plan import (
    _apply,
    _errors,
    _package,
    _plan,
    _plan_and_apply,
    _spec,
)
from tests.integration.test_process_instances import GOAL, _publish, _setup

PROCESS = "sample-goal"


def _retire_in_flight(conn: Any, table: str, kind: str, key: str, lock: str, by: str) -> int:
    """Take the key lock and write the retirement, uncommitted; the holder's pid."""
    tenant = conn.execute(
        text(f"SELECT tenant_id FROM {table} WHERE key = :key LIMIT 1"), {"key": key}
    ).scalar_one()
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))"),
        {"lock": f"{lock}:{tenant}:{key}"},
    )
    conn.execute(
        text(
            "INSERT INTO catalog_retirements (tenant_id, kind, key, retired_at, retired_by, reason)"
            " VALUES (:tenant, :kind, :key, now(), :by, 'replaced')"
        ),
        {"tenant": tenant, "kind": kind, "key": key, "by": by},
    )
    pid: int = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
    return pid


async def test_a_start_waits_for_a_retirement_in_flight_and_is_refused(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    await _publish(client, key, PROCESS, GOAL)

    with sync_engine.connect() as conn:
        holder = _retire_in_flight(
            conn, "process_definitions", "Process", PROCESS, "cp:process", s["admin"]
        )
        start = asyncio.create_task(
            client.post(
                "/api/v1/process-instances",
                json={"process": PROCESS, "key": "goal-1", "data": {"open": 2}},
                headers=auth(key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await start

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "process_retired"


async def test_a_process_naming_a_calendar_waits_for_its_retirement_and_is_refused(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key = s["key"]
    published = await client.post(
        "/api/v1/calendars", json={"key": "ru", "spec": CALENDAR["spec"]}, headers=auth(key)
    )
    assert published.status_code == 201, published.text

    with sync_engine.connect() as conn:
        holder = _retire_in_flight(conn, "calendars", "Calendar", "ru", "cp:calendar", s["admin"])
        publish = asyncio.create_task(
            client.post(
                "/api/v1/process-definitions",
                json={"key": PROCESS, "spec": {**GOAL, "calendar": "ru"}},
                headers=auth(key),
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await publish

    assert response.status_code == 422, response.text
    codes = [p["code"] for p in response.json()["error"]["details"]["problems"]]
    assert "calendar_retired" in codes


async def test_a_restore_of_a_process_waits_for_a_retirement_of_its_calendar_and_is_refused(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, _package({**_spec(admin), "calendar": "ru"}, calendar=True))
    retired = await client.post(
        f"/api/v1/process-definitions/{PLANNED}:retire",
        json={"reason": "replaced"},
        headers=auth(key),
    )
    assert retired.status_code == 200, retired.text
    # The process alone, as it is: its calendar is in use when the plan is built.
    package = _package({**_spec(admin), "calendar": "ru"})
    plan = await _plan(client, key, package)
    assert {(c["key"], c["action"]) for c in plan["changes"]} == {(PLANNED, "restore")}
    assert _errors(plan) == []

    with sync_engine.connect() as conn:
        # The retired process needs the calendar no more: its retirement goes on.
        holder = _retire_in_flight(conn, "calendars", "Calendar", "ru", "cp:calendar", admin)
        apply = asyncio.create_task(_apply(client, key, package, plan["planHash"]))
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await apply

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "calendar_retired"
    assert response.json()["error"]["details"] == {"process": PLANNED, "calendars": ["ru"]}
    shown = await client.get(f"/api/v1/process-definitions/{PLANNED}", headers=auth(key))
    assert shown.json()["status"] == "retired"
