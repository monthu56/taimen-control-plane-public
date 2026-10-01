"""The replay gate on Postgres: ``scripts/process_replay_all.py`` over journals ``main`` wrote.

process-observability P018 (SC-009). Each fixture of
``tests/fixtures/process_journals`` is restored row by row into a tenant —
calendar versions, process versions (without an engine revision: the column
takes its default, 1, as the migration gives the rows of before), instances
and their journals — and the script replays the whole tenant through ``POST
/process-definitions/{key}:replay``: zero divergences, every instance
replayed. A journal changed by hand diverges; a key that may not replay
leaves the report failed, not silent.
"""

import copy
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from scripts.process_replay_all import BATCH, exit_code, replay_tenant, summary
from sqlalchemy import insert, text
from sqlalchemy.engine import Engine

from control_plane.infrastructure.db.models import (
    CalendarVersion,
    ProcessDefinition,
    ProcessInstance,
    ProcessInstanceEvent,
)
from tests.helpers import auth, create_agent_with_key
from tests.integration.test_process_instances import _setup

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "process_journals"
FILES = sorted(FIXTURES.glob("*.json"))

_TIMES = ("at", "created_at", "started_at", "updated_at", "completed_at")
_IDS = ("id", "definition_id", "instance_id", "event_id", "actor_id")


def _row(record: dict[str, Any], **extra: Any) -> dict[str, Any]:
    row = dict(record)
    for name in _TIMES:
        if row.get(name) is not None:
            row[name] = datetime.fromisoformat(str(row[name]).replace("Z", "+00:00"))
    for name in _IDS:
        if row.get(name) is not None:
            row[name] = uuid.UUID(str(row[name]))
    return {**row, **extra}


def _restore(sync_engine: Engine, fixture: dict[str, Any], admin: str) -> None:
    """Write the fixture's rows as ``main`` stored them, into the one tenant of the test."""
    admin_id = uuid.UUID(admin)
    with sync_engine.begin() as conn:
        tenant_id = conn.execute(text("SELECT id FROM tenants")).scalar_one()
        for calendar in fixture["calendars"]:
            conn.execute(
                insert(CalendarVersion).values(
                    _row(calendar, id=uuid.uuid4(), tenant_id=tenant_id, created_by=admin_id)
                )
            )
        for definition in fixture["definitions"]:
            conn.execute(
                insert(ProcessDefinition).values(
                    _row(definition, tenant_id=tenant_id, workspace_id=None, created_by=admin_id)
                )
            )
        for instance in fixture["instances"]:
            record = {k: v for k, v in instance.items() if k not in ("journal", "story")}
            conn.execute(
                insert(ProcessInstance).values(
                    _row(record, tenant_id=tenant_id, workspace_id=None, started_by=None)
                )
            )
            for entry in instance["journal"]:
                conn.execute(
                    insert(ProcessInstanceEvent).values(
                        _row(entry, tenant_id=tenant_id, instance_id=uuid.UUID(instance["id"]))
                    )
                )


def _fixture(path: Path) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return body


@pytest.mark.parametrize("path", FILES, ids=[p.stem for p in FILES])
async def test_the_script_replays_every_recorded_instance_with_no_divergence(
    client: httpx.AsyncClient, sync_engine: Engine, path: Path
) -> None:
    s = await _setup(client)
    fixture = _fixture(path)
    _restore(sync_engine, fixture, s["admin"])

    # Batches of three: more than one call per version of the larger fixtures.
    report = await replay_tenant(client, auth(s["key"]), batch=3)

    assert report.ok, summary(report)
    assert exit_code(report) == 0
    totals = report.totals()
    versions = {i["definition_version"] for i in fixture["instances"]}
    assert totals == {
        "processes": 1,
        "versions": len(versions),
        "instances": len(fixture["instances"]),
        "replayed": len(fixture["instances"]),
        "diverged": 0,
        "refusedVersions": 0,
        "errors": 0,
    }
    out = report.out()
    assert out["ok"] is True
    assert [(v["key"], v["version"]) for v in out["versions"]] == [
        (fixture["process"], version) for version in sorted(versions)
    ]
    assert s["key"] not in json.dumps(out), "the report holds no credentials"


async def test_a_journal_changed_by_hand_is_reported_as_a_divergence(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    fixture = copy.deepcopy(_fixture(FIXTURES / "due-and-escalations.json"))
    [overdue] = [i for i in fixture["instances"] if i["instance_key"] == "D-overdue"]
    fired = next(e for e in overdue["journal"] if e["input"]["kind"] == "timer")
    next(d for d in fired["decisions"] if d["kind"] == "escalated")["level"] = 7
    _restore(sync_engine, fixture, s["admin"])

    report = await replay_tenant(client, auth(s["key"]))

    assert not report.ok
    assert exit_code(report) == 1
    assert report.totals()["replayed"] == len(fixture["instances"])
    [version] = report.versions
    [item] = version.diverged
    assert (item["instanceKey"], item["instanceId"]) == ("D-overdue", overdue["id"])
    [divergence] = item["divergences"]
    assert (divergence["journalSeq"], divergence["kind"], divergence["element"]) == (
        fired["seq"],
        "decision",
        "sign",
    )
    assert "D-overdue" in summary(report)


async def test_a_key_that_may_not_replay_fails_the_report(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    fixture = _fixture(FIXTURES / "duration-due.json")
    _restore(sync_engine, fixture, s["admin"])
    _, reader = await create_agent_with_key(
        client, s["key"], name="reader", permissions=["processes.read"]
    )

    report = await replay_tenant(client, auth(reader))

    assert not report.ok
    assert exit_code(report) == 2
    [version] = report.versions
    assert version.replayed == 0
    assert {(e["status"], e["code"]) for e in version.errors} == {(403, "permission_denied")}


async def test_a_tenant_without_processes_is_an_empty_report(client: httpx.AsyncClient) -> None:
    s = await _setup(client)
    report = await replay_tenant(client, auth(s["key"]))
    assert report.ok
    assert exit_code(report) == 0
    assert report.totals()["instances"] == 0


async def test_a_process_asked_for_by_key_alone_and_one_unknown(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    fixture = _fixture(FIXTURES / "migration.json")
    _restore(sync_engine, fixture, s["admin"])

    report = await replay_tenant(client, auth(s["key"]), processes=[fixture["process"]] * 2)
    assert report.ok, summary(report)
    assert {v.key for v in report.versions} == {fixture["process"]}

    unknown = await replay_tenant(
        client, auth(s["key"]), processes=[fixture["process"], "no-such-process"]
    )
    assert not unknown.ok, "a process that is not there is not one with nothing to diverge"
    assert exit_code(unknown) == 2
    [error] = unknown.errors
    assert (error["process"], error["status"]) == ("no-such-process", 404)
    assert {v.key for v in unknown.versions} == {fixture["process"]}


async def test_a_batch_out_of_bounds_is_refused(client: httpx.AsyncClient) -> None:
    for batch in (0, BATCH + 1):
        with pytest.raises(ValueError):
            await replay_tenant(client, {}, batch=batch)
