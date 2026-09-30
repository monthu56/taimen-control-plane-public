"""A package apply and a publication racing it (CP-ADR-0074 §11, amendment 2026-09-29).

The apply takes the key locks of the commands it runs before it plans: a
publication of the same key still open when the apply starts either commits
before the plan is built — and the hash is stale — or waits for the apply. It
never lands between the plan and the command, where the apply would deprecate
the versions it saw and leave the new one active beside its own.

The locks come in one order: the trial of a plan takes the keys the way the
apply does, and the rows a foreign key points at (task types, rules) are
held ``FOR NO KEY UPDATE``, so the process engine inserting a task of the
type while it holds an instance the apply waits for does not wait back.

``packages:record`` takes the tenant's apply lock before it writes a link: an
apply holds the links of its plan and inserts the missing ones last, and a
record writing the same links one by one would otherwise wait for it holding
the one it inserted first (``40P01``).
"""

import asyncio
from typing import Any

import httpx
import yaml
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from tests.concurrency.test_principal_disable_races import (
    _blocked_backend,
    _wait_until_blocked_by,
)
from tests.helpers import auth, create_agent_with_key
from tests.integration import test_package_plan as process_plan
from tests.integration.test_package_plan import _apply, _errors, _plan, _plan_and_apply
from tests.integration.test_package_plan_catalog import (
    AGENT,
    RULE,
    TYPE,
    _admin,
    _full,
    _get,
    _rule,
    _types,
)
from tests.integration.test_package_test import API_VERSION
from tests.integration.test_process_instances import AGENT as PROCESS_AGENT
from tests.integration.test_process_instances import AGENT_PERMISSIONS, _setup


def _columns(conn: Any, table: str, *skip: str) -> str:
    names = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = :table ORDER BY ordinal_position"
        ),
        {"table": table},
    ).scalars()
    return ", ".join(name for name in names if name not in skip)


async def test_a_task_type_published_while_the_apply_waits_makes_the_plan_stale(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())
    changed = _full(task_type={"description": "Triage, carefully"})
    plan = await _plan(client, key, changed)
    assert [c["action"] for c in plan["changes"]] == ["update", "unchanged", "unchanged"]

    with sync_engine.connect() as conn:
        # A publication of the type in flight: its key lock, its new version.
        tenant = conn.execute(
            text("SELECT tenant_id FROM task_types WHERE key = :key"), {"key": TYPE}
        ).scalar_one()
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))"),
            {"lock": f"cp:tt:{tenant}:{TYPE}"},
        )
        columns = [
            name
            for name in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'task_types' ORDER BY ordinal_position"
                )
            ).scalars()
            if name not in ("id", "version")
        ]
        listed = ", ".join(columns)
        conn.execute(
            text(
                f"INSERT INTO task_types (id, version, {listed})"
                f" SELECT gen_random_uuid(), version + 1, {listed} FROM task_types"
                " WHERE key = :key AND version = 1"
            ),
            {"key": TYPE},
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()

        apply = asyncio.create_task(_apply(client, key, changed, plan["planHash"]))
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await apply

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "plan_stale"
    types = await _types(client, key)
    assert {v: t["status"] for v, t in types.items()} == {1: "active", 2: "active"}


async def test_an_agent_revised_while_the_apply_waits_makes_the_plan_stale(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())
    changed = _full(agent={"description": "Files triage, carefully"})
    plan = await _plan(client, key, changed)
    assert [c["action"] for c in plan["changes"]] == ["unchanged", "update", "unchanged"]

    with sync_engine.connect() as conn:
        # A revision of the agent in flight: its key lock, its new revision.
        agent_id, tenant = conn.execute(
            text("SELECT id, tenant_id FROM agents WHERE key = :key"), {"key": AGENT}
        ).one()
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))"),
            {"lock": f"cp:agent:{tenant}:{AGENT}"},
        )
        listed = _columns(conn, "agent_revisions", "id", "revision", "spec")
        conn.execute(
            text(
                f"INSERT INTO agent_revisions (id, revision, spec, {listed})"
                " SELECT gen_random_uuid(), 2,"
                " jsonb_set(spec, '{description}', '\"Revised by hand\"'),"
                f" {listed} FROM agent_revisions WHERE agent_id = :agent AND revision = 1"
            ),
            {"agent": agent_id},
        )
        conn.execute(
            text("UPDATE agents SET current_revision = 2 WHERE id = :agent"), {"agent": agent_id}
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()

        apply = asyncio.create_task(_apply(client, key, changed, plan["planHash"]))
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await apply

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "plan_stale"
    agent = await _get(client, key, f"/agents/{AGENT}")
    assert agent["currentRevision"] == 2


async def test_a_rule_disabled_while_the_apply_waits_makes_the_plan_stale(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())
    changed = _full(rule={"description": "Triage each appearance, carefully"})
    plan = await _plan(client, key, changed)
    assert [c["action"] for c in plan["changes"]] == ["unchanged", "unchanged", "update"]

    with sync_engine.connect() as conn:
        # A :disable of the rule in flight: the rule's row, its new status.
        conn.execute(
            text(
                "UPDATE work_rules SET status = 'disabled'"
                " WHERE key = :key AND status <> 'archived'"
            ),
            {"key": RULE},
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()

        apply = asyncio.create_task(_apply(client, key, changed, plan["planHash"]))
        await _wait_until_blocked_by(sync_engine, holder)
        conn.commit()
        response = await apply

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "plan_stale"
    rule = await _rule(client, key)
    assert (rule["status"], rule["description"]) == ("disabled", "Triage each appearance")


async def test_a_plan_takes_its_caller_first_and_is_refused_once_the_caller_is_disabled(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    """The trial of a plan runs writing commands: rule 1 of CP-ADR-0077 holds."""
    key = await _admin(client)
    planner, planner_key = await create_agent_with_key(
        client,
        key,
        name="planner",
        permissions=["packages.plan", "task_types.manage", "agents.manage", "rules.write"],
    )

    with sync_engine.connect() as conn:
        # A :disable of the planner in flight.
        conn.execute(
            text("SELECT id FROM principals WHERE id = :id FOR UPDATE"), {"id": planner["id"]}
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        plan = asyncio.create_task(
            client.post(
                "/api/v1/packages:plan", json={"package": _full()}, headers=auth(planner_key)
            )
        )
        await _wait_until_blocked_by(sync_engine, holder)
        conn.execute(
            text("UPDATE principals SET status = 'disabled' WHERE id = :id"),
            {"id": planner["id"]},
        )
        conn.commit()

    response = await plan
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "principal_not_active"


async def test_the_trial_of_a_plan_takes_the_keys_in_the_order_of_the_apply(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    key = await _admin(client)
    await _plan_and_apply(client, key, _full())
    changed = _full(task_type={"description": "Triage, carefully"}, agent={"description": "New"})

    with sync_engine.connect() as conn:
        # Someone holds the agent's key: the plan's trial stops on it holding
        # the key of the task type, which comes first.
        tenant = conn.execute(
            text("SELECT tenant_id FROM agents WHERE key = :key"), {"key": AGENT}
        ).scalar_one()
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock, 0))"),
            {"lock": f"cp:agent:{tenant}:{AGENT}"},
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()

        plan = asyncio.create_task(_plan(client, key, changed))
        await _wait_until_blocked_by(sync_engine, holder)
        planner = _blocked_backend(sync_engine, holder)
        # The apply asks for the task type first too: it waits behind the
        # plan, not behind the holder with the agent's key in hand.
        apply = asyncio.create_task(_apply(client, key, changed, "sha256:" + "0" * 64))
        await _wait_until_blocked_by(sync_engine, planner)
        applier = _blocked_backend(sync_engine, planner)
        with sync_engine.connect() as probe:
            blockers = probe.execute(
                text("SELECT pg_blocking_pids(:pid)"), {"pid": applier}
            ).scalar_one()
        assert blockers == [planner]
        conn.commit()
        planned = await plan
        response = await apply

    assert _errors(planned) == []
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "plan_stale"


async def test_the_engine_inserts_a_task_of_the_type_while_an_apply_waits_for_its_instance(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, process_plan._package(process_plan._spec(admin)))
    instance_id = await process_plan._start(client, key, "N-1")

    # One package changes the process and the task type its step files.
    package = process_plan._package(
        process_plan._spec(
            admin,
            version=2,
            display="Sample plan, again",
            migrations=[{"from": 1, "to": 2, "policy": "migrate"}],
        )
    )
    review = {
        "apiVersion": API_VERSION,
        "kind": "TaskType",
        "key": "review",
        "spec": {
            "displayName": "Review",
            "description": "Review, carefully",
            "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
            "fieldSchema": {"type": "object", "properties": {"decision": {"type": "string"}}},
        },
    }
    package["files"].append(
        {"path": "task-types/review.yaml", "content": yaml.safe_dump(review, sort_keys=False)}
    )
    plan = await _plan(client, key, package)
    assert _errors(plan) == []
    assert {(c["kind"], c["action"]) for c in plan["changes"]} == {
        ("TaskType", "update"),
        ("Process", "update"),
    }

    with sync_engine.connect() as conn:
        # The engine: the instance under its lock, then a task of its step.
        conn.execute(text("SET lock_timeout = '10s'"))
        conn.execute(
            text("SELECT id FROM process_instances WHERE id = :id FOR UPDATE"),
            {"id": instance_id},
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        apply = asyncio.create_task(_apply(client, key, package, plan["planHash"]))
        await _wait_until_blocked_by(sync_engine, holder)

        listed = _columns(conn, "tasks", "id", "public_id")
        conn.execute(
            text(
                f"INSERT INTO tasks (id, public_id, {listed})"
                f" SELECT gen_random_uuid(), public_id || '-again', {listed} FROM tasks"
                " WHERE type_id IN (SELECT id FROM task_types WHERE key = 'review')"
                " ORDER BY created_at LIMIT 1"
            )
        )
        conn.commit()
        response = await apply

    assert response.status_code == 200, response.text
    applied = {(a["kind"], a["action"]) for a in response.json()["applied"]}
    assert applied == {("TaskType", "update"), ("Process", "update")}


async def _record(
    client: httpx.AsyncClient, key: str, objects: list[tuple[str, str]]
) -> httpx.Response:
    return await client.post(
        "/api/v1/packages:record",
        json={
            "package": {"key": "sample-plan", "version": "1.0.0"},
            "objects": [{"kind": kind, "key": k} for kind, k in objects],
        },
        headers=auth(key),
    )


async def test_a_record_of_the_package_waits_for_its_apply_instead_of_deadlocking(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    s = await _setup(client)
    key, admin = s["key"], s["admin"]
    await _plan_and_apply(client, key, process_plan._package(process_plan._spec(admin)))
    instance_id = await process_plan._start(client, key, "N-1")
    # The agent is linked, the task type is not: the apply locks the link of
    # the one and inserts the link of the other at its end.
    recorded = await _record(client, key, [("Agent", PROCESS_AGENT)])
    assert recorded.status_code == 200, recorded.text
    # The installer is another principal: the caller's lock does not queue it.
    _, installer = await create_agent_with_key(
        client,
        key,
        name="installer",
        permissions=["packages.plan", "task_types.manage", "agents.manage"],
    )

    package = process_plan._package(
        process_plan._spec(
            admin,
            version=2,
            display="Sample plan, again",
            migrations=[{"from": 1, "to": 2, "policy": "migrate"}],
        )
    )
    documents = [
        (
            "task-types/review.yaml",
            "TaskType",
            "review",
            {
                "displayName": "Review",
                "description": "Review, carefully",
                "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
                "fieldSchema": {
                    "type": "object",
                    "properties": {"decision": {"type": "string"}},
                },
            },
        ),
        (
            "agents/process.yaml",
            "Agent",
            PROCESS_AGENT,
            {
                "displayName": "Sample process",
                "identity": {"kind": "service", "permissions": AGENT_PERMISSIONS},
                "placement": "none",
            },
        ),
    ]
    for path, kind, object_key, spec in documents:
        document = {"apiVersion": API_VERSION, "kind": kind, "key": object_key, "spec": spec}
        package["files"].append(
            {"path": path, "content": yaml.safe_dump(document, sort_keys=False)}
        )
    plan = await _plan(client, key, package)
    assert _errors(plan) == []

    with sync_engine.connect() as conn:
        # The engine holds the instance: the apply stops on it holding the links.
        conn.execute(text("SET lock_timeout = '10s'"))
        conn.execute(
            text("SELECT id FROM process_instances WHERE id = :id FOR UPDATE"),
            {"id": instance_id},
        )
        holder = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
        apply = asyncio.create_task(_apply(client, key, package, plan["planHash"]))
        await _wait_until_blocked_by(sync_engine, holder)
        applier = _blocked_backend(sync_engine, holder)

        # The installer records the same objects meanwhile: it waits for the
        # apply before it writes a link, not with the task type's in hand.
        record = asyncio.create_task(
            _record(client, installer, [("TaskType", "review"), ("Agent", PROCESS_AGENT)])
        )
        await _wait_until_blocked_by(sync_engine, applier)
        recorder = _blocked_backend(sync_engine, applier)
        with sync_engine.connect() as probe:
            waits = (
                probe.execute(
                    text("SELECT locktype FROM pg_locks WHERE pid = :pid AND NOT granted"),
                    {"pid": recorder},
                )
                .scalars()
                .all()
            )
        assert waits == ["advisory"]
        conn.commit()
        response = await apply
        recorded = await record

    assert response.status_code == 200, response.text
    assert recorded.status_code == 200, recorded.text
    applied = {(a["kind"], a["action"]) for a in response.json()["applied"]}
    assert ("Process", "update") in applied
