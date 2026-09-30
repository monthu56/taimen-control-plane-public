"""One rule evaluation by id (CP-ADR-0063, amendment 2026-09-29, runtime-console R001).

What is under test: work a rule filed points to its evaluation through
``origin.ref = rule_evaluation:<id>``; ``GET /rule-evaluations/{id}`` reads it
in one call, in the same shape as an item of ``/rules/{id}/evaluations``.
``rules.read`` is required and is decided on the rule's workspace; an
evaluation of another tenant is not found.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.authorization import authorize as real_authorize
from control_plane.application.queries import work_rules as rule_queries
from control_plane.config import Settings
from control_plane.domain.errors import AuthorizationError
from control_plane.worker.main import Worker
from tests.helpers import (
    auth,
    create_agent_with_key,
    create_workspace,
    do_bootstrap,
    make_tenant_directly,
)

DRIFT_RULE: dict[str, Any] = {
    "key": "drift",
    "trigger": {"kind": "observation", "type": "drift.seen"},
    "action": {
        "kind": "ensure_work",
        "taskType": "task",
        "dedupKeyTemplate": "drift:{{payload.data.id}}",
        "fields": {"title": "Drift {{payload.data.id}}"},
    },
}


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _admin(client: httpx.AsyncClient) -> dict[str, Any]:
    body = await do_bootstrap(client)
    return {"key": body["apiKey"]["key"], "tenantId": body["tenant"]["id"]}


async def _create_rule(client: httpx.AsyncClient, key: str, /, **extra: Any) -> dict[str, Any]:
    response = await client.post("/api/v1/rules", json={**DRIFT_RULE, **extra}, headers=auth(key))
    assert response.status_code == 201, response.text
    rule: dict[str, Any] = response.json()
    return rule


async def _fire(client: httpx.AsyncClient, key: str, worker: Worker) -> dict[str, Any]:
    """The admin's rule files one task; returns that task."""
    await _create_rule(client, key)
    response = await client.post(
        "/api/v1/observations",
        json={"kind": "drift.seen", "content": "drift seen", "data": {"id": "a"}},
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text
    await worker.run_once()
    response = await client.get("/api/v1/tasks", params={"limit": 100}, headers=auth(key))
    assert response.status_code == 200, response.text
    [task] = [t for t in response.json()["items"] if (t.get("origin") or {}).get("kind") == "rule"]
    return dict(task)


def _evaluation_id(task: dict[str, Any]) -> str:
    kind, _, evaluation_id = str(task["origin"]["ref"]).partition(":")
    assert kind == "rule_evaluation"
    return evaluation_id


def _insert_evaluation(sync_engine: Engine, tenant_id: str, rule_id: str) -> str:
    """A finished evaluation of ``rule_id``, without running the worker."""
    evaluation_id = str(uuid.uuid4())
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rule_evaluations (id, tenant_id, rule_id, rule_version, "
                "trigger_ref, status, result, evidence, created_task_ids, created_at, "
                "updated_at) VALUES (:id, :tenant, :rule, 1, :ref, 'not_matched', "
                "'{}'::jsonb, '[]'::jsonb, '[]'::jsonb, now(), now())"
            ),
            {
                "id": evaluation_id,
                "tenant": tenant_id,
                "rule": rule_id,
                "ref": f"event:{uuid.uuid4()}",
            },
        )
    return evaluation_id


async def test_the_evaluation_that_filed_a_task_reads_in_one_call(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    admin = await _admin(client)
    task = await _fire(client, admin["key"], worker)
    evaluation_id = _evaluation_id(task)

    response = await client.get(
        f"/api/v1/rule-evaluations/{evaluation_id}", headers=auth(admin["key"])
    )
    assert response.status_code == 200, response.text
    evaluation = response.json()
    assert evaluation["id"] == evaluation_id
    assert evaluation["ruleId"] == task["origin"]["ruleId"]
    assert evaluation["status"] == "matched"
    assert evaluation["createdTaskIds"] == [task["id"]]
    assert evaluation["result"]["work"][0]["taskId"] == task["id"]

    # The same object as the item of the rule's history.
    history = await client.get(
        f"/api/v1/rules/{evaluation['ruleId']}/evaluations", headers=auth(admin["key"])
    )
    assert history.status_code == 200, history.text
    assert history.json()["items"] == [evaluation]


async def test_reading_an_evaluation_needs_rules_read(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    admin = await _admin(client)
    evaluation_id = _evaluation_id(await _fire(client, admin["key"], worker))
    _, stranger_key = await create_agent_with_key(
        client, admin["key"], name="stranger", permissions=["tasks.read"]
    )
    response = await client.get(
        f"/api/v1/rule-evaluations/{evaluation_id}", headers=auth(stranger_key)
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_denied"

    _, reader_key = await create_agent_with_key(
        client, admin["key"], name="reader", permissions=["rules.read"]
    )
    response = await client.get(
        f"/api/v1/rule-evaluations/{evaluation_id}", headers=auth(reader_key)
    )
    assert response.status_code == 200, response.text

    anonymous = await client.get(f"/api/v1/rule-evaluations/{evaluation_id}")
    assert anonymous.status_code == 401


async def test_an_evaluation_of_another_tenant_is_not_found(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    admin = await _admin(client)
    evaluation_id = _evaluation_id(await _fire(client, admin["key"], worker))
    _, other_key = make_tenant_directly(sync_engine, "other")

    response = await client.get(
        f"/api/v1/rule-evaluations/{evaluation_id}", headers=auth(other_key)
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["details"] == {"evaluationId": evaluation_id}

    unknown = await client.get(
        f"/api/v1/rule-evaluations/{uuid.uuid4()}", headers=auth(admin["key"])
    )
    assert unknown.status_code == 404
    malformed = await client.get("/api/v1/rule-evaluations/not-a-uuid", headers=auth(admin["key"]))
    assert malformed.status_code == 400


async def test_an_evaluation_of_an_archived_rule_reads(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    admin = await _admin(client)
    rule = await _create_rule(client, admin["key"])
    evaluation_id = _insert_evaluation(sync_engine, admin["tenantId"], rule["id"])
    archived = await client.delete(f"/api/v1/rules/{rule['id']}", headers=auth(admin["key"]))
    assert archived.status_code == 204, archived.text

    response = await client.get(
        f"/api/v1/rule-evaluations/{evaluation_id}", headers=auth(admin["key"])
    )
    assert response.status_code == 200, response.text
    assert response.json()["ruleId"] == rule["id"]


def _scoped_rules_read(
    monkeypatch: pytest.MonkeyPatch, principal_id: str, workspace_id: str
) -> list[str]:
    """Simulate a PDP binding that grants ``rules.read`` on one workspace only.

    Local authorization is flat, so the scoped case is reproduced at the one
    seam the query uses; the returned list records what was asked.
    """
    asked: list[str] = []

    async def authorize(ctx: Any, *any_of: Any, resource: Any = None, **kwargs: Any) -> None:
        names = {permission.value for permission in any_of}
        if str(ctx.principal_id) == principal_id and "rules.read" in names:
            key = resource.key if resource is not None else "tenant"
            asked.append(key)
            if resource is not None and key != f"workspace:{workspace_id}":
                raise AuthorizationError(details={"required": ["rules.read"], "resource": key})
            return
        await real_authorize(ctx, *any_of, resource=resource, **kwargs)

    monkeypatch.setattr(rule_queries, "authorize", authorize)
    return asked


async def test_rules_read_is_decided_on_the_workspace_of_the_rule(
    client: httpx.AsyncClient, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await _admin(client)
    own = await create_workspace(client, admin["key"], "own")
    foreign = await create_workspace(client, admin["key"], "foreign")
    own_rule = await _create_rule(client, admin["key"], workspaceId=own["id"])
    foreign_rule = await _create_rule(
        client, admin["key"], key="drift-foreign", workspaceId=foreign["id"]
    )
    own_evaluation = _insert_evaluation(sync_engine, admin["tenantId"], own_rule["id"])
    foreign_evaluation = _insert_evaluation(sync_engine, admin["tenantId"], foreign_rule["id"])
    reader, reader_key = await create_agent_with_key(
        client, admin["key"], name="reader", permissions=["rules.read"]
    )
    asked = _scoped_rules_read(monkeypatch, reader["id"], own["id"])

    response = await client.get(
        f"/api/v1/rule-evaluations/{own_evaluation}", headers=auth(reader_key)
    )
    assert response.status_code == 200, response.text
    assert f"workspace:{own['id']}" in asked
    response = await client.get(
        f"/api/v1/rule-evaluations/{foreign_evaluation}", headers=auth(reader_key)
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["resource"] == f"workspace:{foreign['id']}"
    assert f"workspace:{foreign['id']}" in asked


async def test_an_evaluation_of_a_tenant_rule_is_decided_on_the_tenant(
    client: httpx.AsyncClient, sync_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await _admin(client)
    own = await create_workspace(client, admin["key"], "own")
    rule = await _create_rule(client, admin["key"])
    evaluation_id = _insert_evaluation(sync_engine, admin["tenantId"], rule["id"])
    reader, reader_key = await create_agent_with_key(
        client, admin["key"], name="reader", permissions=["rules.read"]
    )
    asked = _scoped_rules_read(monkeypatch, reader["id"], own["id"])

    response = await client.get(
        f"/api/v1/rule-evaluations/{evaluation_id}", headers=auth(reader_key)
    )
    assert response.status_code == 200, response.text
    assert asked == ["tenant"]
