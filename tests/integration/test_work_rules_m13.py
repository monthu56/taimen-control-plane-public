"""M1.3 work derivation rules (CP-ADR-0063, TAI-ADR-0036 p.3-4, 6).

What is under test:

*Rules are data, validated on write.* ``/rules`` CRUD with ``rules.read`` /
``rules.write``; a condition outside the closed JSON language, an unknown task
type or skill, an unpinned skill or an ``external_write`` one is refused.

*The worker derives work from facts, once.* An observation that fires a rule
files a work item with ``origin.kind = rule``, the rule's id and the
observation as evidence; the same dedup key never files a second open item,
and a journal batch read twice is evaluated once.

*Interpretation goes through a skill.* A rule that needs one queues a
``skill_invocation`` through the ordinary path and resumes when it ended; the
result becomes an artifact, and that artifact is evidence of the work.

*Everything is audited.* ``rule.*`` events for the rule's life, one
``rule.evaluated`` per evaluation, ``work.derived`` / ``work.reconciled`` for
the work, and the evaluation history under ``/rules/{id}/evaluations``.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_workspace,
    do_bootstrap,
    open_session,
)

COMMIT = "repo.commit_observed"
ENTRYPOINT = "selfdev.adr_conformance:invoke"

# The contract of `adr.conformance_check@1` as the selfdev package publishes
# it (packages/selfdev/skills/adr.conformance_check.yaml in the superproject).
CONFORMANCE_CONTRACT: dict[str, Any] = {
    "idempotency": "natural",
    "timeoutSeconds": 600,
    "retryPolicy": {"maxAttempts": 2, "backoffSeconds": 30},
    "inputs": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["repository"],
        "additionalProperties": False,
        "properties": {
            "ref": {"type": "string", "minLength": 1},
            "adrs": {
                "type": "array",
                "items": {"type": "string", "pattern": "^(TAI|CP|PC|MEM)-[0-9-]+$"},
                "maxItems": 200,
            },
            "repository": {"type": "string", "minLength": 1},
        },
    },
    "outputs": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["ref", "results", "summary"],
        "properties": {
            "ref": {"type": "string"},
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["adr", "status", "gaps"],
                    "properties": {
                        "adr": {"type": "string"},
                        "gaps": {"type": "array", "items": {"type": "string"}},
                        "title": {"type": "string"},
                        "status": {"enum": ["implemented", "partial", "missing", "unverifiable"]},
                    },
                },
            },
            "summary": {"type": "object"},
        },
    },
    "implementation": {"protocol": "local", "entrypoint": ENTRYPOINT},
}

# The first pilot rule, as data: a commit is observed → the conformance skill
# checks the repository at that commit → every ADR not implemented is work.
PILOT_RULE: dict[str, Any] = {
    "key": "adr-conformance",
    "description": "Accepted decisions hold in the code at every observed commit",
    "trigger": {"kind": "observation", "type": COMMIT},
    "condition": {"exists": "payload.data.repo"},
    "interpretation": {
        "skill": "adr.conformance_check@1",
        "inputs": {"repository": "{{payload.data.repo}}", "ref": "{{payload.data.sha}}"},
    },
    "action": {
        "kind": "ensure_work",
        "taskType": "task",
        "forEach": "skill.output.results",
        "where": {"ne": [{"var": "item.status"}, "implemented"]},
        "dedupKeyTemplate": "adr-conformance:{{payload.data.repo}}:{{item.adr}}",
        "fields": {
            "title": "{{item.adr}} is {{item.status}} in {{payload.data.repo}}",
            "description": "Gaps: {{item.gaps}}",
            "priority": "high",
        },
    },
}

DRIFT_RULE: dict[str, Any] = {
    "key": "drift",
    "trigger": {"kind": "observation", "type": "drift.seen"},
    "condition": {"eq": [{"var": "payload.data.severity"}, "high"]},
    "action": {
        "kind": "ensure_work",
        "taskType": "task",
        "dedupKeyTemplate": "drift:{{payload.data.id}}",
        "fields": {"title": "Drift {{payload.data.id}}", "description": "{{payload.content}}"},
    },
}


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _admin(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    return key


async def _create_rule(client: httpx.AsyncClient, key: str, /, **body: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/rules", json=body, headers=auth(key))
    assert response.status_code == 201, response.text
    rule: dict[str, Any] = response.json()
    return rule


async def _observe(
    client: httpx.AsyncClient, key: str, kind: str, data: dict[str, Any], **extra: Any
) -> str:
    response = await client.post(
        "/api/v1/observations",
        json={"kind": kind, "content": f"{kind} seen", "data": data, **extra},
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text
    observation_id: str = response.json()["id"]
    return observation_id


async def _evaluations(
    client: httpx.AsyncClient, key: str, rule_id: str, **params: Any
) -> list[dict[str, Any]]:
    response = await client.get(
        f"/api/v1/rules/{rule_id}/evaluations", params={"limit": 100, **params}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _events(
    client: httpx.AsyncClient, key: str, event_type: str, entity_type: str
) -> list[dict[str, Any]]:
    items = (
        await client.get(f"/api/v1/events?limit=200&entityType={entity_type}", headers=auth(key))
    ).json()["items"]
    return [e for e in items if e["type"] == event_type]


def _rule_tasks(sync_engine: Engine) -> list[Any]:
    with sync_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, public_id, title, description, priority, status, origin, "
                    "evidence, system_status_category, created_by FROM tasks "
                    "WHERE origin->>'kind' = 'rule' ORDER BY created_at, public_id"
                )
            ).all()
        )


async def _task(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    task: dict[str, Any] = response.json()
    return task


async def _set_status(client: httpx.AsyncClient, key: str, task_id: str, status: str) -> None:
    task = await _task(client, key, task_id)
    response = await client.patch(
        f"/api/v1/tasks/{task_id}",
        json={"status": status},
        headers={**auth(key), "If-Match": f'"task-{task["version"]}"'},
    )
    assert response.status_code == 200, response.text


def _make_waiting_due(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE rule_evaluations SET next_check_at = now() WHERE status = 'waiting'")
        )


# --- rules as data ------------------------------------------------------------------


async def test_rule_lifecycle_through_the_api_is_audited(client: httpx.AsyncClient) -> None:
    admin_key = await _admin(client)
    rule = await _create_rule(client, admin_key, **DRIFT_RULE)
    assert (rule["key"], rule["version"], rule["status"]) == ("drift", 1, "enabled")
    assert rule["enabledAt"] is not None
    assert rule["authorityPrincipalId"] == rule["createdBy"]
    assert rule["action"]["fields"]["title"] == "Drift {{payload.data.id}}"

    fetched = await client.get(f"/api/v1/rules/{rule['id']}", headers=auth(admin_key))
    assert fetched.headers["ETag"] == '"rule-1"'

    taken = await client.post("/api/v1/rules", json=DRIFT_RULE, headers=auth(admin_key))
    assert taken.status_code == 409
    assert taken.json()["error"]["code"] == "rule_key_taken"

    stale = await client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"description": "x"},
        headers={**auth(admin_key), "If-Match": '"rule-7"'},
    )
    assert stale.status_code == 409
    changed = await client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"condition": None, "description": "Every drift"},
        headers={**auth(admin_key), "If-Match": '"rule-1"'},
    )
    assert changed.status_code == 200, changed.text
    assert (changed.json()["version"], changed.json()["condition"]) == (2, True)
    # Restating the current values is not a change.
    same = await client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"description": "Every drift"},
        headers={**auth(admin_key), "If-Match": '"rule-2"'},
    )
    assert same.json()["version"] == 2

    disabled = await client.post(f"/api/v1/rules/{rule['id']}:disable", headers=auth(admin_key))
    assert disabled.json()["status"] == "disabled"
    again = await client.post(f"/api/v1/rules/{rule['id']}:disable", headers=auth(admin_key))
    assert again.status_code == 200
    enabled = await client.post(f"/api/v1/rules/{rule['id']}:enable", headers=auth(admin_key))
    assert enabled.json()["status"] == "enabled"

    listed = await client.get("/api/v1/rules?key=drift", headers=auth(admin_key))
    assert [r["id"] for r in listed.json()["items"]] == [rule["id"]]

    archived = await client.delete(f"/api/v1/rules/{rule['id']}", headers=auth(admin_key))
    assert archived.status_code == 204
    assert (await client.get("/api/v1/rules", headers=auth(admin_key))).json()["items"] == []
    shown = await client.get("/api/v1/rules?status=archived", headers=auth(admin_key))
    assert [r["id"] for r in shown.json()["items"]] == [rule["id"]]
    refused = await client.post(f"/api/v1/rules/{rule['id']}:enable", headers=auth(admin_key))
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "rule_archived"
    # The key of an archived rule is free again.
    await _create_rule(client, admin_key, **DRIFT_RULE)

    types = [e["type"] for e in await _all_rule_events(client, admin_key, rule["id"])]
    assert types == [
        "rule.created",
        "rule.updated",
        "rule.disabled",
        "rule.enabled",
        "rule.archived",
    ]
    updated = next(
        e
        for e in await _all_rule_events(client, admin_key, rule["id"])
        if e["type"] == "rule.updated"
    )
    assert updated["payload"]["changes"] == ["condition", "description"]
    assert updated["payload"]["version"] == 2


async def _all_rule_events(
    client: httpx.AsyncClient, key: str, rule_id: str
) -> list[dict[str, Any]]:
    items = (
        await client.get("/api/v1/events?limit=200&entityType=rule", headers=auth(key))
    ).json()["items"]
    return [e for e in items if e["entityId"] == rule_id and e["type"] != "rule.evaluated"]


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"condition": {"eval": ["__import__('os')"]}}, "invalid_rule_condition"),
        ({"condition": {"lt": [{"var": "payload.x"}]}}, "invalid_rule_condition"),
        ({"condition": {"eq": [{"var": "env.HOME"}, 1]}}, "invalid_rule_condition"),
        ({"trigger": {"kind": "event", "type": "work.derived"}}, "invalid_rule_trigger"),
        (
            {"trigger": {"kind": "event", "type": "skill.invocation_succeeded"}},
            "invalid_rule_trigger",
        ),
        ({"action": {**DRIFT_RULE["action"], "taskType": "nope"}}, "unknown_task_type"),
        ({"action": {**DRIFT_RULE["action"], "kind": "run_shell"}}, "invalid_rule_action"),
        ({"interpretation": {"skill": "adr.conformance_check"}}, "invalid_rule_interpretation"),
        ({"interpretation": {"skill": "missing@1"}}, "unknown_skill"),
        ({"interpretation": {"skill": "git.merge@1"}}, "rule_skill_side_effects"),
        ({"key": "Not A Key"}, "invalid_rule"),
    ],
)
async def test_an_invalid_rule_is_refused_on_write(
    client: httpx.AsyncClient, override: dict[str, Any], code: str
) -> None:
    admin_key = await _admin(client)
    published = await client.post(
        "/api/v1/skills",
        json={
            "name": "git.merge",
            "version": "1",
            "sideEffects": "external_write",
            "riskLevel": "medium",
            "contract": {
                **CONFORMANCE_CONTRACT,
                "implementation": {"protocol": "local", "entrypoint": "m:run"},
            },
        },
        headers=auth(admin_key),
    )
    assert published.status_code == 201, published.text
    response = await client.post(
        "/api/v1/rules", json={**DRIFT_RULE, **override}, headers=auth(admin_key)
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert (await client.get("/api/v1/rules", headers=auth(admin_key))).json()["items"] == []


async def test_rules_have_their_own_rights(client: httpx.AsyncClient) -> None:
    admin_key = await _admin(client)
    _, reader_key = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["rules.read", "tasks.write"]
    )
    refused = await client.post("/api/v1/rules", json=DRIFT_RULE, headers=auth(reader_key))
    assert refused.status_code == 403
    rule = await _create_rule(client, admin_key, **DRIFT_RULE)
    assert (await client.get(f"/api/v1/rules/{rule['id']}", headers=auth(reader_key))).is_success
    assert (
        await client.post(f"/api/v1/rules/{rule['id']}:disable", headers=auth(reader_key))
    ).status_code == 403
    _, stranger_key = await create_agent_with_key(
        client, admin_key, name="stranger", permissions=["tasks.read"]
    )
    assert (await client.get("/api/v1/rules", headers=auth(stranger_key))).status_code == 403


# --- deriving work ------------------------------------------------------------------


async def test_an_observation_derives_work_once_per_dedup_key(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    # Facts recorded before the rule existed are not its business.
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    rule = await _create_rule(client, admin_key, **DRIFT_RULE)
    await worker.run_once()
    assert await _evaluations(client, admin_key, rule["id"]) == []

    first = await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    [task] = _rule_tasks(sync_engine)
    assert task.origin == {
        "kind": "rule",
        "ruleId": rule["id"],
        "ref": task.origin["ref"],
        "evidence": [{"kind": "observation", "observationId": first}],
    }
    assert task.title == "Drift a"
    assert task.description == "drift.seen seen"
    # The rule acts as the principal that enabled it.
    assert str(task.created_by) == rule["authorityPrincipalId"]
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert evaluation["status"] == "matched"
    assert task.origin["ref"] == f"rule_evaluation:{evaluation['id']}"
    assert evaluation["createdTaskIds"] == [str(task.id)]
    assert evaluation["evidence"] == [{"kind": "observation", "observationId": first}]
    assert evaluation["result"]["work"] == [
        {
            "dedupKey": "drift:a",
            "action": "ensure_work",
            "taskId": str(task.id),
            "publicId": task.public_id,
            "created": True,
        }
    ]

    # The same key while the work is open: no second item, and it is said so.
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    # A fact the condition rejects is evaluated, and nothing else.
    await _observe(client, admin_key, "drift.seen", {"id": "b", "severity": "low"})
    # Another kind of fact is not even evaluated.
    await _observe(client, admin_key, "note", {"id": "a", "severity": "high"})
    await worker.run_once()
    assert len(_rule_tasks(sync_engine)) == 1
    history = await _evaluations(client, admin_key, rule["id"])
    assert [e["status"] for e in history] == ["not_matched", "matched", "matched"]
    assert history[1]["result"]["work"][0]["created"] is False
    assert history[1]["createdTaskIds"] == []

    # A journal batch read again (a crash before the commit of the next one,
    # an operator moving the cursor back) is not evaluated twice.
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE event_consumer_cursors SET tx_id = 0, sequence = 0 "
                "WHERE name = 'work-rules'"
            )
        )
    await worker.run_once()
    assert len(await _evaluations(client, admin_key, rule["id"])) == 3
    assert len(_rule_tasks(sync_engine)) == 1

    # Once the work is closed, the same divergence is new work again.
    await _set_status(client, admin_key, str(task.id), "cancelled")
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    tasks = _rule_tasks(sync_engine)
    assert len(tasks) == 2
    assert tasks[1].system_status_category not in ("terminal_success", "terminal_cancelled")

    derived = await _events(client, admin_key, "work.derived", "task")
    assert {e["entityId"] for e in derived} == {str(t.id) for t in tasks}
    assert derived[0]["payload"]["ruleId"] == rule["id"]
    assert derived[0]["payload"]["dedupKey"] == "drift:a"
    evaluated = await _events(client, admin_key, "rule.evaluated", "rule")
    assert sorted(e["payload"]["result"] for e in evaluated) == [
        "matched",
        "matched",
        "matched",
        "not_matched",
    ]
    assert all(e["payload"]["ruleVersion"] == 1 for e in evaluated)
    created = next(
        e
        for e in await _events(client, admin_key, "task.created", "task")
        if e["entityId"] == str(tasks[0].id)
    )
    assert created["payload"]["origin"]["kind"] == "rule"
    assert created["payload"]["origin"]["ruleId"] == rule["id"]


async def test_disabled_rules_do_not_look_and_enabling_does_not_replay(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    rule = await _create_rule(client, admin_key, **DRIFT_RULE, status="disabled")
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    await client.post(f"/api/v1/rules/{rule['id']}:enable", headers=auth(admin_key))
    await worker.run_once()
    assert await _evaluations(client, admin_key, rule["id"]) == []
    await _observe(client, admin_key, "drift.seen", {"id": "b", "severity": "high"})
    await worker.run_once()
    assert [e["status"] for e in await _evaluations(client, admin_key, rule["id"])] == ["matched"]


async def test_an_evaluation_error_fails_the_evaluation_visibly(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    rule = await _create_rule(
        client,
        admin_key,
        **{**DRIFT_RULE, "condition": {"gt": [{"var": "payload.data.severity"}, 3]}},
    )
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "rule_condition_error"
    assert _rule_tasks(sync_engine) == []
    [event] = await _events(client, admin_key, "rule.evaluated", "rule")
    assert event["payload"]["result"] == "failed"
    assert event["payload"]["error"] == {"code": "rule_condition_error"}


async def test_a_rule_acts_with_its_authors_rights_and_stops_with_their_credential(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    author, author_key = await create_agent_with_key(
        client,
        admin_key,
        name="author",
        permissions=["rules.write", "rules.read", "events.read", "observations.write"],
    )
    rule = await _create_rule(client, author_key, **DRIFT_RULE)
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    # No tasks.write: the rule may not file what its author may not.
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert (evaluation["status"], evaluation["error"]["code"]) == ("failed", "permission_denied")
    assert _rule_tasks(sync_engine) == []

    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE api_keys SET revoked_at = now() WHERE principal_id = :p"),
            {"p": author["id"]},
        )
    await _observe(client, admin_key, "drift.seen", {"id": "b", "severity": "high"})
    await worker.run_once()
    latest = (await _evaluations(client, admin_key, rule["id"]))[0]
    assert (latest["status"], latest["error"]["code"]) == ("failed", "credential_inactive")


async def test_rules_do_not_react_to_what_rules_did(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    rule = await _create_rule(
        client,
        admin_key,
        key="follow-up",
        trigger={"kind": "event", "type": "task.created"},
        action={
            "kind": "ensure_work",
            "taskType": "task",
            "dedupKeyTemplate": "follow-up:{{payload.publicId}}",
            "fields": {"title": "Follow up {{payload.publicId}}: {{task.title}}"},
        },
    )
    created = await client.post(
        "/api/v1/tasks", json={"title": "Original"}, headers=auth(admin_key)
    )
    source = created.json()
    for _ in range(3):
        await worker.run_once()
    [task] = _rule_tasks(sync_engine)
    assert task.title == f"Follow up {source['publicId']}: Original"
    # A core event is not an observation: it is cited by its journal id.
    [event] = [
        e
        for e in await _events(client, admin_key, "task.created", "task")
        if e["entityId"] == source["id"]
    ]
    assert task.origin["evidence"] == [
        {
            "kind": "external",
            "externalRef": {"system": "control-plane", "id": f"event:{event['id']}"},
        }
    ]
    assert len(await _evaluations(client, admin_key, rule["id"])) == 1


async def test_reconciling_rules_update_and_cancel_the_work_another_rule_filed(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    await _create_rule(client, admin_key, **DRIFT_RULE)
    grow = await _create_rule(
        client,
        admin_key,
        key="drift-grows",
        trigger={"kind": "observation", "type": "drift.grew"},
        action={
            "kind": "update_work",
            "dedupKeyTemplate": "drift:{{payload.data.id}}",
            "fields": {"priority": "critical"},
        },
    )
    gone = await _create_rule(
        client,
        admin_key,
        key="drift-gone",
        trigger={"kind": "observation", "type": "drift.gone"},
        action={"kind": "cancel_work", "dedupKeyTemplate": "drift:{{payload.data.id}}"},
    )
    # Nothing to reconcile yet: skipped, not failed.
    await _observe(client, admin_key, "drift.gone", {"id": "a"})
    await worker.run_once()
    [early] = await _evaluations(client, admin_key, gone["id"])
    assert early["result"]["work"][0]["reason"] == "no_open_work"

    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    grew = await _observe(client, admin_key, "drift.grew", {"id": "a"})
    await worker.run_once()
    [task] = _rule_tasks(sync_engine)
    assert task.priority == "critical"
    assert {"kind": "observation", "observationId": grew} in task.evidence
    [update] = await _evaluations(client, admin_key, grow["id"])
    assert update["result"]["work"][0]["changes"] == ["evidence", "priority"]

    await _observe(client, admin_key, "drift.gone", {"id": "a"})
    await worker.run_once()
    [task] = _rule_tasks(sync_engine)
    assert (task.status, task.system_status_category) == ("cancelled", "terminal_cancelled")
    reconciled = await _events(client, admin_key, "work.reconciled", "task")
    assert sorted(e["payload"]["action"] for e in reconciled) == ["cancel_work", "update_work"]


async def test_request_decision_files_work_and_asks_for_a_decision(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin = await do_bootstrap(client)
    admin_key = admin["apiKey"]["key"]
    rule = await _create_rule(
        client,
        admin_key,
        **{
            **DRIFT_RULE,
            "action": {
                **DRIFT_RULE["action"],
                "kind": "request_decision",
                "fields": {
                    **DRIFT_RULE["action"]["fields"],
                    "approver": admin["adminPrincipal"]["id"],
                },
            },
        },
    )
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    [task] = _rule_tasks(sync_engine)
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    approval_id = evaluation["result"]["work"][0]["approvalId"]
    approval = (
        await client.get(f"/api/v1/approvals/{approval_id}", headers=auth(admin_key))
    ).json()
    assert approval["taskId"] == str(task.id)
    assert approval["status"] == "pending"
    assert approval["gate"] is True


# The decision's outcomes as a type declares them (CP-ADR-0061): a comment
# naming the decision, so the worker's run shows which list it executed.
DECISION_SCHEMA: dict[str, Any] = {
    "gates": {
        "default": {
            "outcomes": {
                "approved": [{"comment": {"body": "approved"}}],
                "rejected": [{"comment": {"body": "rejected"}}],
            }
        }
    }
}


async def _decision_setup(client: httpx.AsyncClient, worker: Worker) -> dict[str, Any]:
    """A ``request_decision`` rule on a type with outcomes, fired once."""
    admin = await do_bootstrap(client)
    admin_key = admin["apiKey"]["key"]
    created = await client.post(
        "/api/v1/task-types",
        json={"key": "decision", "displayName": "Decision", "approvalSchema": DECISION_SCHEMA},
        headers=auth(admin_key),
    )
    assert created.status_code == 201, created.text
    rule = await _create_rule(
        client,
        admin_key,
        **{
            **DRIFT_RULE,
            "action": {
                **DRIFT_RULE["action"],
                "kind": "request_decision",
                "taskType": "decision",
                "fields": {
                    **DRIFT_RULE["action"]["fields"],
                    "approver": admin["adminPrincipal"]["id"],
                },
            },
        },
    )
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    work = evaluation["result"]["work"][0]
    return {"admin_key": admin_key, "task_id": work["taskId"], "approval_id": work["approvalId"]}


@pytest.mark.parametrize("verb", ["approve", "reject"])
async def test_a_decision_on_request_decision_runs_the_types_outcomes(
    client: httpx.AsyncClient, worker: Worker, verb: str
) -> None:
    s = await _decision_setup(client, worker)
    admin_key, approval_id = s["admin_key"], s["approval_id"]

    decided = await client.post(
        f"/api/v1/approvals/{approval_id}:{verb}", json={}, headers=auth(admin_key)
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["outcomeStatus"] == "pending"

    await worker.run_once()
    outcome = (
        await client.get(f"/api/v1/approvals/{approval_id}/outcome", headers=auth(admin_key))
    ).json()
    expected = "approved" if verb == "approve" else "rejected"
    assert outcome["outcome"] == expected
    assert outcome["outcomeStatus"] == "executed"
    assert [(a["action"], a["status"]) for a in outcome["actions"]] == [("comment", "executed")]
    comments = (
        await client.get(f"/api/v1/tasks/{s['task_id']}/comments", headers=auth(admin_key))
    ).json()["items"]
    assert [c["body"] for c in comments] == [expected]


async def test_request_decision_work_waits_for_the_decision_before_a_claim(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    s = await _decision_setup(client, worker)
    admin_key = s["admin_key"]
    _, agent_key = await create_agent_with_key(client, admin_key)
    session = await open_session(client, agent_key)

    held = await claim_task(client, agent_key, s["task_id"], session["id"])
    assert held.status_code == 409, held.text
    assert held.json()["error"]["code"] == "approval_required"

    decided = await client.post(
        f"/api/v1/approvals/{s['approval_id']}:approve", json={}, headers=auth(admin_key)
    )
    assert decided.status_code == 200, decided.text
    claimed = await claim_task(client, agent_key, s["task_id"], session["id"])
    assert claimed.status_code == 200, claimed.text


# --- interpretation through a skill (the pilot) ------------------------------------------


async def _pilot_setup(client: httpx.AsyncClient) -> dict[str, Any]:
    admin_key = await _admin(client)
    published = await client.post(
        "/api/v1/skills",
        json={
            "name": "adr.conformance_check",
            "version": "1",
            "sideEffects": "none",
            "riskLevel": "low",
            "contract": CONFORMANCE_CONTRACT,
        },
        headers=auth(admin_key),
    )
    assert published.status_code == 201, published.text
    _, executor_key = await create_agent_with_key(
        client, admin_key, name="executor", permissions=["skills.execute"]
    )
    rule = await _create_rule(client, admin_key, **PILOT_RULE)
    return {"admin_key": admin_key, "executor_key": executor_key, "rule": rule}


async def _claim(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/skill-invocations:claim",
        json={"protocols": ["local"], "localEntrypoints": [ENTRYPOINT]},
        headers=auth(key),
    )
    assert response.status_code == 200, response.text
    lease: dict[str, Any] = response.json()["invocation"]
    return lease


def _results(*pairs: tuple[str, str]) -> dict[str, Any]:
    return {
        "ref": "abc1234",
        "results": [
            {"adr": adr, "status": status, "gaps": [] if status == "implemented" else ["probe"]}
            for adr, status in pairs
        ],
        "summary": {},
    }


async def test_pilot_commit_is_interpreted_by_a_skill_and_gaps_become_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _pilot_setup(client)
    admin_key, rule = s["admin_key"], s["rule"]
    commit = await _observe(
        client,
        admin_key,
        COMMIT,
        {"repo": "control-plane", "sha": "abc1234"},
        source="git",
        dedupKey="abc1234",
    )
    await worker.run_once()
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert evaluation["status"] == "waiting"
    invocation = (
        await client.get(
            f"/api/v1/skill-invocations/{evaluation['skillInvocationId']}", headers=auth(admin_key)
        )
    ).json()
    assert invocation["inputs"] == {"repository": "control-plane", "ref": "abc1234"}
    assert invocation["status"] == "pending"
    # Waiting holds nothing: another pass changes nothing.
    _make_waiting_due(sync_engine)
    await worker.run_once()
    assert (await _evaluations(client, admin_key, rule["id"]))[0]["status"] == "waiting"

    lease = await _claim(client, s["executor_key"])
    output = _results(("CP-0036", "implemented"), ("CP-0062", "missing"), ("CP-0063", "partial"))
    done = await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": output},
        headers=auth(s["executor_key"]),
    )
    assert done.status_code == 200, done.text
    _make_waiting_due(sync_engine)
    await worker.run_once()

    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert evaluation["status"] == "matched"
    assert evaluation["result"]["items"] == 3
    assert evaluation["result"]["selected"] == 2
    tasks = _rule_tasks(sync_engine)
    assert sorted(t.title for t in tasks) == [
        "CP-0062 is missing in control-plane",
        "CP-0063 is partial in control-plane",
    ]
    assert all(t.priority == "high" for t in tasks)
    artifact_id = evaluation["evidence"][1]["artifactId"]
    assert evaluation["evidence"] == [
        {"kind": "observation", "observationId": commit},
        {"kind": "artifact", "artifactId": artifact_id},
    ]
    for task in tasks:
        assert task.origin["evidence"] == evaluation["evidence"]
        assert task.origin["ruleId"] == rule["id"]
    with sync_engine.connect() as conn:
        artifact = conn.execute(
            text("SELECT type, name, task_id, content, metadata FROM artifacts WHERE id = :a"),
            {"a": artifact_id},
        ).one()
    assert (artifact.type, artifact.name, artifact.task_id) == (
        "skill_result",
        "adr.conformance_check@1",
        None,
    )
    assert artifact.content["output"] == output
    assert artifact.metadata["evaluationId"] == evaluation["id"]

    # The next commit with the same gaps files nothing new.
    await _observe(
        client,
        admin_key,
        COMMIT,
        {"repo": "control-plane", "sha": "def5678"},
        source="git",
        dedupKey="def5678",
    )
    await worker.run_once()
    lease = await _claim(client, s["executor_key"])
    await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": output},
        headers=auth(s["executor_key"]),
    )
    _make_waiting_due(sync_engine)
    await worker.run_once()
    assert len(_rule_tasks(sync_engine)) == 2
    latest = (await _evaluations(client, admin_key, rule["id"]))[0]
    assert [w["created"] for w in latest["result"]["work"]] == [False, False]


async def test_a_failed_interpretation_files_no_work(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _pilot_setup(client)
    await _observe(client, s["admin_key"], COMMIT, {"repo": "control-plane", "sha": "abc1234"})
    await worker.run_once()
    lease = await _claim(client, s["executor_key"])
    failed = await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:fail",
        json={
            "fencingToken": lease["fencingToken"],
            "error": {"code": "repo_unavailable", "message": "clone failed", "retryable": False},
        },
        headers=auth(s["executor_key"]),
    )
    assert failed.status_code == 200, failed.text
    _make_waiting_due(sync_engine)
    await worker.run_once()
    [evaluation] = await _evaluations(client, s["admin_key"], s["rule"]["id"])
    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "rule_skill_failed"
    assert evaluation["error"]["details"]["cause"]["code"] == "repo_unavailable"
    assert _rule_tasks(sync_engine) == []


async def test_a_rule_changed_while_waiting_skips_and_cancels_its_call(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _pilot_setup(client)
    admin_key, rule = s["admin_key"], s["rule"]
    await _observe(client, admin_key, COMMIT, {"repo": "control-plane", "sha": "abc1234"})
    await worker.run_once()
    changed = await client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"description": "A different rule now"},
        headers={**auth(admin_key), "If-Match": '"rule-1"'},
    )
    assert changed.status_code == 200
    _make_waiting_due(sync_engine)
    await worker.run_once()
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert evaluation["status"] == "skipped"
    assert evaluation["result"]["skipped"]["reason"] == "rule_changed"
    invocation = (
        await client.get(
            f"/api/v1/skill-invocations/{evaluation['skillInvocationId']}", headers=auth(admin_key)
        )
    ).json()
    assert invocation["status"] == "cancelled"


async def test_a_scheduled_rule_runs_when_due_and_asks_its_skill(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _pilot_setup(client)
    admin_key = s["admin_key"]
    scheduled = await _create_rule(
        client,
        admin_key,
        key="nightly-conformance",
        trigger={"kind": "schedule", "type": "interval", "everySeconds": 86400},
        interpretation={
            "skill": "adr.conformance_check@1",
            "inputs": {"repository": "control-plane"},
        },
        action={
            **PILOT_RULE["action"],
            "dedupKeyTemplate": "adr-conformance:control-plane:{{item.adr}}",
            "fields": {"title": "{{item.adr}} is {{item.status}}"},
        },
    )
    await worker.run_once()
    await worker.run_once()
    [evaluation] = await _evaluations(client, admin_key, scheduled["id"])
    assert evaluation["triggerRef"].startswith("schedule:")
    assert evaluation["status"] == "waiting"
    rule = (await client.get(f"/api/v1/rules/{scheduled['id']}", headers=auth(admin_key))).json()
    assert rule["nextRunAt"] > evaluation["createdAt"]
    lease = await _claim(client, s["executor_key"])
    await client.post(
        f"/api/v1/skill-invocations/{lease['id']}:complete",
        json={"fencingToken": lease["fencingToken"], "output": _results(("CP-0001", "missing"))},
        headers=auth(s["executor_key"]),
    )
    _make_waiting_due(sync_engine)
    await worker.run_once()
    [task] = _rule_tasks(sync_engine)
    assert task.title == "CP-0001 is missing"
    # A schedule is not a fact: the skill's result is the only evidence.
    assert [e["kind"] for e in task.origin["evidence"]] == ["artifact"]


async def test_evaluations_of_another_tenant_are_not_visible(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    rule = await _create_rule(client, admin_key, **DRIFT_RULE)
    from tests.helpers import make_tenant_directly

    _, other_key = make_tenant_directly(sync_engine, "other")
    response = await client.get(f"/api/v1/rules/{rule['id']}", headers=auth(other_key))
    assert response.status_code == 404
    response = await client.get(
        f"/api/v1/rules/{uuid.uuid4()}/evaluations", headers=auth(admin_key)
    )
    assert response.status_code == 404


# --- a rule cannot feed itself through its skill call --------------------------------


async def test_a_rule_does_not_react_to_the_life_of_its_own_skill_call(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _pilot_setup(client)
    admin_key = s["admin_key"]
    # Refused on write; stored anyway here, as if written before the guard:
    # the engine still must not let it re-queue itself.
    rule = await _create_rule(
        client,
        admin_key,
        key="echo",
        trigger={"kind": "event", "type": "task.created"},
        interpretation={
            "skill": "adr.conformance_check@1",
            "inputs": {"repository": "control-plane"},
        },
        action={
            **PILOT_RULE["action"],
            "dedupKeyTemplate": "echo:{{item.adr}}",
            "fields": {"title": "{{item.adr}} is {{item.status}}"},
        },
    )
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE work_rules SET trigger = "
                """'{"kind": "event", "type": "skill.invocation_succeeded"}'::jsonb """
                "WHERE id = :r"
            ),
            {"r": rule["id"]},
        )

    async def complete_next() -> None:
        lease = await _claim(client, s["executor_key"])
        done = await client.post(
            f"/api/v1/skill-invocations/{lease['id']}:complete",
            json={
                "fencingToken": lease["fencingToken"],
                "output": _results(("CP-0001", "missing")),
            },
            headers=auth(s["executor_key"]),
        )
        assert done.status_code == 200, done.text

    # A call somebody else made fires the rule once...
    invoked = await client.post(
        "/api/v1/skills/adr.conformance_check@1:invoke",
        json={"inputs": {"repository": "control-plane"}},
        headers=auth(admin_key),
    )
    assert invoked.status_code == 201, invoked.text
    await complete_next()
    await worker.run_once()
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert evaluation["status"] == "waiting"
    # ...and the success of the rule's own call does not fire it again.
    await complete_next()
    _make_waiting_due(sync_engine)
    for _ in range(3):
        await worker.run_once()
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert evaluation["status"] == "matched"
    with sync_engine.connect() as conn:
        calls = conn.execute(text("SELECT count(*) FROM skill_invocations")).scalar_one()
    assert calls == 2
    assert len(_rule_tasks(sync_engine)) == 1


# --- failures: unavailable dependencies, broken evaluations, schedules -------------------


def _cursor(sync_engine: Engine) -> Any:
    with sync_engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT failure_count, next_attempt_at, parked_reason, tx_id, sequence "
                "FROM event_consumer_cursors WHERE name = 'work-rules'"
            )
        ).one()


def _release_cursor(sync_engine: Engine) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE event_consumer_cursors SET next_attempt_at = now() "
                "WHERE name = 'work-rules'"
            )
        )


async def test_an_unavailable_decision_holds_the_batch_back_instead_of_failing_it(
    client: httpx.AsyncClient,
    worker: Worker,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.application.commands import rule_evaluations
    from control_plane.domain.errors import DependencyUnavailableError

    admin_key = await _admin(client)
    rule = await _create_rule(client, admin_key, **DRIFT_RULE)
    real_authorize = rule_evaluations.authorize

    async def pdp_down(*args: Any, **kwargs: Any) -> None:
        raise DependencyUnavailableError()

    monkeypatch.setattr(rule_evaluations, "authorize", pdp_down)
    before = _cursor(sync_engine)
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    # Nothing recorded, the cursor did not move past the fact, the tenant waits.
    assert await _evaluations(client, admin_key, rule["id"]) == []
    held = _cursor(sync_engine)
    assert held.failure_count == 1
    assert "DependencyUnavailableError" in held.parked_reason
    assert (held.tx_id, held.sequence) == (before.tx_id, before.sequence)
    assert held.next_attempt_at is not None
    # Backoff: the next tick does not retry yet.
    await worker.run_once()
    assert _cursor(sync_engine).failure_count == 1

    monkeypatch.setattr(rule_evaluations, "authorize", real_authorize)
    _release_cursor(sync_engine)
    await worker.run_once()
    assert [e["status"] for e in await _evaluations(client, admin_key, rule["id"])] == ["matched"]
    cleared = _cursor(sync_engine)
    assert (cleared.failure_count, cleared.next_attempt_at, cleared.parked_reason) == (
        0,
        None,
        None,
    )


async def test_a_broken_evaluation_stops_its_tenant_only_until_attempts_run_out(
    client: httpx.AsyncClient,
    settings: Settings,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.application.commands import rule_evaluations

    admin_key = await _admin(client)
    healthy = await _create_rule(client, admin_key, **DRIFT_RULE)
    broken = await _create_rule(client, admin_key, **{**DRIFT_RULE, "key": "broken"})
    real_act = rule_evaluations._act

    async def act(session: Any, ctx: Any, rule: Any, *args: Any) -> Any:
        if rule.key == "broken":
            raise RuntimeError("boom")
        return await real_act(session, ctx, rule, *args)

    monkeypatch.setattr(rule_evaluations, "_act", act)
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    worker = Worker(settings.model_copy(update={"rules_max_attempts": 2}))
    try:
        for attempt in (1, 2):
            await worker.run_once()
            assert _cursor(sync_engine).failure_count == attempt
            assert await _evaluations(client, admin_key, healthy["id"]) == []
            _release_cursor(sync_engine)
        await worker.run_once()
    finally:
        await worker.engine.dispose()

    [good] = await _evaluations(client, admin_key, healthy["id"])
    assert good["status"] == "matched"
    [bad] = await _evaluations(client, admin_key, broken["id"])
    assert (bad["status"], bad["error"]["code"]) == ("failed", "rule_internal_error")
    assert "RuntimeError: boom" in bad["error"]["message"]
    assert _cursor(sync_engine).failure_count == 0
    # The healthy rule's work stands; nothing of the broken attempt does.
    assert len(_rule_tasks(sync_engine)) == 1
    audited = [
        e
        for e in await _events(client, admin_key, "rule.evaluated", "rule")
        if e["entityId"] == broken["id"]
    ]
    assert [e["payload"]["error"] for e in audited] == [{"code": "rule_internal_error"}]


async def test_a_broken_schedule_closes_its_slot_and_waits_for_the_next(
    client: httpx.AsyncClient,
    worker: Worker,
    sync_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from control_plane.application.commands import rule_evaluations

    admin_key = await _admin(client)
    rule = await _create_rule(
        client,
        admin_key,
        key="sweep",
        trigger={"kind": "schedule", "type": "interval", "everySeconds": 3600},
        action={"kind": "cancel_work", "dedupKeyTemplate": "sweep"},
    )

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(rule_evaluations, "_open_work", broken)
    for _ in range(3):
        await worker.run_once()
    # One failed slot, not a retry on every tick.
    [evaluation] = await _evaluations(client, admin_key, rule["id"])
    assert (evaluation["status"], evaluation["error"]["code"]) == ("failed", "rule_internal_error")
    current = (await client.get(f"/api/v1/rules/{rule['id']}", headers=auth(admin_key))).json()
    assert current["nextRunAt"] > evaluation["createdAt"]


# --- reconciliation edges ------------------------------------------------------------------


async def test_work_under_a_live_claim_is_not_rewritten_by_a_rule(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    await _create_rule(client, admin_key, **DRIFT_RULE)
    grow = await _create_rule(
        client,
        admin_key,
        key="drift-grows",
        trigger={"kind": "observation", "type": "drift.grew"},
        action={
            "kind": "update_work",
            "dedupKeyTemplate": "drift:{{payload.data.id}}",
            "fields": {"priority": "critical"},
        },
    )
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    [task] = _rule_tasks(sync_engine)
    _, agent_key = await create_agent_with_key(client, admin_key, name="worker-agent")
    session = await open_session(client, agent_key)
    claimed = await claim_task(client, agent_key, str(task.id), session["id"])
    assert claimed.status_code == 200, claimed.text

    await _observe(client, admin_key, "drift.grew", {"id": "a"})
    await worker.run_once()
    [evaluation] = await _evaluations(client, admin_key, grow["id"])
    assert evaluation["status"] == "matched"
    assert evaluation["result"]["work"][0]["reason"] == "task_claimed"
    [task] = _rule_tasks(sync_engine)
    assert task.priority != "critical"
    assert await _events(client, admin_key, "work.reconciled", "task") == []


async def test_work_closed_by_people_is_filed_again_when_the_fact_returns(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    rule = await _create_rule(client, admin_key, **DRIFT_RULE)
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    [first] = _rule_tasks(sync_engine)
    await _set_status(client, admin_key, str(first.id), "cancelled")
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    first_again, second = _rule_tasks(sync_engine)
    assert first_again.id == first.id and first_again.status == "cancelled"
    assert second.system_status_category not in ("terminal_success", "terminal_cancelled")
    latest = (await _evaluations(client, admin_key, rule["id"]))[0]
    assert latest["result"]["work"][0]["created"] is True


async def test_a_rule_waits_for_a_concurrent_edit_instead_of_failing_on_its_version(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    await _create_rule(client, admin_key, **DRIFT_RULE)
    grow = await _create_rule(
        client,
        admin_key,
        key="drift-grows",
        trigger={"kind": "observation", "type": "drift.grew"},
        action={
            "kind": "update_work",
            "dedupKeyTemplate": "drift:{{payload.data.id}}",
            "fields": {"priority": "critical"},
        },
    )
    await _observe(client, admin_key, "drift.seen", {"id": "a", "severity": "high"})
    await worker.run_once()
    [task] = _rule_tasks(sync_engine)
    version = (await _task(client, admin_key, str(task.id)))["version"]
    await _observe(client, admin_key, "drift.grew", {"id": "a"})

    # Somebody edits the task while the rule is about to change it.
    with sync_engine.connect() as conn:
        conn.execute(
            text("UPDATE tasks SET version = version + 1, title = 'Edited by hand' WHERE id = :t"),
            {"t": task.id},
        )
        running = asyncio.create_task(worker.run_once())
        await asyncio.sleep(1.0)
        assert not running.done()
        conn.commit()
    await running

    [evaluation] = await _evaluations(client, admin_key, grow["id"])
    assert evaluation["status"] == "matched", evaluation
    [task] = _rule_tasks(sync_engine)
    assert (task.title, task.priority) == ("Edited by hand", "critical")
    # Two writes, two versions: the rule changed what the person left, not a
    # stale copy under the same version.
    assert (await _task(client, admin_key, str(task.id)))["version"] == version + 2


async def test_an_event_of_another_workspace_does_not_reach_a_workspace_rule(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin_key = await _admin(client)
    mine = await create_workspace(client, admin_key, "mine")
    theirs = await create_workspace(client, admin_key, "theirs")
    rule = await _create_rule(client, admin_key, **DRIFT_RULE, workspaceId=mine["id"])
    high = {"severity": "high"}
    await _observe(client, admin_key, "drift.seen", {"id": "b", **high}, workspaceId=theirs["id"])
    await _observe(client, admin_key, "drift.seen", {"id": "a", **high}, workspaceId=mine["id"])
    # A fact of the whole tenant reaches every rule (CP-ADR-0063 §3).
    await _observe(client, admin_key, "drift.seen", {"id": "t", **high})
    await worker.run_once()
    assert len(await _evaluations(client, admin_key, rule["id"])) == 2
    assert sorted(t.title for t in _rule_tasks(sync_engine)) == ["Drift a", "Drift t"]


async def test_an_interpretation_call_nobody_takes_expires_and_files_nothing(
    client: httpx.AsyncClient, settings: Settings, sync_engine: Engine
) -> None:
    s = await _pilot_setup(client)
    admin_key = s["admin_key"]
    await _observe(client, admin_key, COMMIT, {"repo": "control-plane", "sha": "abc1234"})
    worker = Worker(settings.model_copy(update={"rules_skill_wait_seconds": 60.0}))
    try:
        await worker.run_once()
        [evaluation] = await _evaluations(client, admin_key, s["rule"]["id"])
        assert evaluation["status"] == "waiting"
        # Within the wait, nothing happens.
        _make_waiting_due(sync_engine)
        await worker.run_once()
        assert (await _evaluations(client, admin_key, s["rule"]["id"]))[0]["status"] == "waiting"
        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE skill_invocations SET updated_at = now() - interval '2 minutes', "
                    "available_at = now() - interval '2 minutes'"
                )
            )
        _make_waiting_due(sync_engine)
        await worker.run_once()
    finally:
        await worker.engine.dispose()
    [evaluation] = await _evaluations(client, admin_key, s["rule"]["id"])
    assert (evaluation["status"], evaluation["error"]["code"]) == ("failed", "rule_skill_failed")
    invocation = (
        await client.get(
            f"/api/v1/skill-invocations/{evaluation['skillInvocationId']}", headers=auth(admin_key)
        )
    ).json()
    assert invocation["status"] == "cancelled"
    assert _rule_tasks(sync_engine) == []
