"""Work rules under concurrent workers (CP-ADR-0063).

Several worker replicas may run side by side. A tenant's journal batch is
evaluated under its cursor row lock (``SKIP LOCKED``), each (rule, trigger)
has one evaluation, and filing work for a dedup key is serialized per key —
so racing workers neither evaluate a fact twice nor file a key twice. The
keys of one evaluation are locked in one order, so a journal batch and a
resumed wait over the same keys do not deadlock.
"""

import asyncio
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key, do_bootstrap

RULE: dict[str, Any] = {
    "key": "drift",
    "trigger": {"kind": "observation", "type": "drift.seen"},
    "action": {
        "kind": "ensure_work",
        "taskType": "task",
        "dedupKeyTemplate": "drift:{{payload.data.id}}",
        "fields": {"title": "Drift {{payload.data.id}}"},
    },
}


async def test_racing_workers_evaluate_each_fact_once_and_file_each_key_once(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    admin_key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await client.post("/api/v1/rules", json=RULE, headers=auth(admin_key))
    assert response.status_code == 201, response.text
    # A second rule filing under the same keys: the key is tenant-wide.
    response = await client.post(
        "/api/v1/rules", json={**RULE, "key": "drift-twin"}, headers=auth(admin_key)
    )
    assert response.status_code == 201, response.text
    for index in range(30):
        observed = await client.post(
            "/api/v1/observations",
            json={"kind": "drift.seen", "content": "seen", "data": {"id": index % 5}},
            headers=auth(admin_key),
        )
        assert observed.status_code == 201, observed.text

    workers = [Worker(settings.model_copy(update={"rules_batch_size": 4})) for _ in range(3)]
    try:
        for _ in range(12):
            await asyncio.gather(*(w.run_once() for w in workers))
    finally:
        for w in workers:
            await w.engine.dispose()

    with sync_engine.connect() as conn:
        evaluations = conn.execute(
            text("SELECT count(*), count(DISTINCT (rule_id, trigger_ref)) FROM rule_evaluations")
        ).one()
        open_per_key = conn.execute(
            text(
                "SELECT w.dedup_key, count(*) FROM rule_work_items w JOIN tasks t "
                "ON t.id = w.task_id GROUP BY w.dedup_key ORDER BY w.dedup_key"
            )
        ).all()
    # 30 facts x 2 rules, each exactly once.
    assert tuple(evaluations) == (60, 60)
    assert [(key, count) for key, count in open_per_key] == [(f"drift:{i}", 1) for i in range(5)]


SKILL_CONTRACT: dict[str, Any] = {
    "idempotency": "natural",
    "timeoutSeconds": 60,
    "retryPolicy": {"maxAttempts": 1, "backoffSeconds": 1},
    "inputs": {"type": "object"},
    "outputs": {"type": "object", "required": ["keys"]},
    "implementation": {"protocol": "local", "entrypoint": "race.check:invoke"},
}


async def test_a_batch_and_a_resumed_wait_over_the_same_keys_do_not_deadlock(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    """Both evaluations file work for the same keys, listed in opposite order."""
    admin = await do_bootstrap(client)
    admin_key = admin["apiKey"]["key"]
    published = await client.post(
        "/api/v1/skills",
        json={
            "name": "race.check",
            "version": "1",
            "sideEffects": "none",
            "riskLevel": "low",
            "contract": SKILL_CONTRACT,
        },
        headers=auth(admin_key),
    )
    assert published.status_code == 201, published.text
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=["skills.execute"]
    )
    for body in (
        {
            "key": "asked",
            "trigger": {"kind": "observation", "type": "check.asked"},
            "interpretation": {"skill": "race.check@1", "inputs": {}},
            "action": {
                **RULE["action"],
                "forEach": "skill.output.keys",
                "dedupKeyTemplate": "k:{{item}}",
                "fields": {"title": "K {{item}}"},
            },
        },
        {
            "key": "seen",
            "trigger": {"kind": "observation", "type": "keys.seen"},
            "action": {
                **RULE["action"],
                "forEach": "payload.data.keys",
                "dedupKeyTemplate": "k:{{item}}",
                "fields": {"title": "K {{item}}"},
            },
        },
    ):
        response = await client.post("/api/v1/rules", json=body, headers=auth(admin_key))
        assert response.status_code == 201, response.text

    resumer, reader = (Worker(settings) for _ in range(2))
    try:
        for round_ in range(5):
            keys = [f"{round_}-{name}" for name in ("a", "b", "c", "d")]
            await _observe(client, admin_key, "check.asked", {})
            await reader.process_rule_events()
            claimed = await client.post(
                "/api/v1/skill-invocations:claim",
                json={"protocols": ["local"], "localEntrypoints": ["race.check:invoke"]},
                headers=auth(executor_key),
            )
            assert claimed.status_code == 200, claimed.text
            lease = claimed.json()["invocation"]
            done = await client.post(
                f"/api/v1/skill-invocations/{lease['id']}:complete",
                json={"fencingToken": lease["fencingToken"], "output": {"keys": keys[::-1]}},
                headers=auth(executor_key),
            )
            assert done.status_code == 200, done.text
            with sync_engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE rule_evaluations SET next_check_at = now() WHERE status = 'waiting'"
                    )
                )
            await _observe(client, admin_key, "keys.seen", {"keys": keys})
            await asyncio.gather(resumer.resume_rule_evaluations(), reader.process_rule_events())
            with sync_engine.connect() as conn:
                cursor = conn.execute(
                    text(
                        "SELECT failure_count, parked_reason FROM event_consumer_cursors "
                        "WHERE name = 'work-rules'"
                    )
                ).one()
            assert cursor.failure_count == 0, cursor.parked_reason
    finally:
        for w in (resumer, reader):
            await w.engine.dispose()

    with sync_engine.connect() as conn:
        statuses = conn.execute(
            text("SELECT status, count(*) FROM rule_evaluations GROUP BY status")
        ).all()
        per_key = conn.execute(
            text("SELECT count(*), count(DISTINCT dedup_key) FROM rule_work_items")
        ).one()
    assert dict(statuses) == {"matched": 10}
    assert tuple(per_key) == (20, 20)


async def _observe(client: httpx.AsyncClient, key: str, kind: str, data: dict[str, Any]) -> None:
    response = await client.post(
        "/api/v1/observations",
        json={"kind": kind, "content": kind, "data": data},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
