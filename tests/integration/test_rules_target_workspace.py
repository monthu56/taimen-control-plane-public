"""Rule work in another workspace, for a role (CP-ADR-0063, amendment process-packages P012).

``ensure_work`` files its work in ``fields.workspaceId`` (a template; the
rule's own workspace when it is omitted or renders to nothing), and
``fields.assignee: role:<slug>`` leaves the work to any holder of the role in
that workspace — what ``regulation-drift`` of the package process-knowledge
does with a task about a process: the process's workspace, its owner.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from control_plane.config import Settings
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import auth, create_role, create_workspace, do_bootstrap

FOUND = "sample.found"


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _setup(client: httpx.AsyncClient) -> str:
    key: str = (await do_bootstrap(client))["apiKey"]["key"]
    response = await client.post(
        "/api/v1/task-types",
        json={"key": "sample-work", "displayName": "W", "lifecycleSchema": SYSTEM_TASK_LIFECYCLE},
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return key


def _rule(**fields: str) -> dict[str, Any]:
    return {
        "key": "sample-found",
        "trigger": {"kind": "observation", "type": FOUND},
        "action": {
            "kind": "ensure_work",
            "taskType": "sample-work",
            "forEach": "payload.data.items",
            "dedupKeyTemplate": "sample:{{item.id}}",
            "fields": {"title": "Sample {{item.id}}", **fields},
        },
    }


async def _run(
    client: httpx.AsyncClient, worker: Worker, key: str, rule: dict[str, Any], items: list[Any]
) -> dict[str, Any]:
    created = await client.post("/api/v1/rules", json=rule, headers=auth(key))
    assert created.status_code == 201, created.text
    observed = await client.post(
        "/api/v1/observations",
        json={"kind": FOUND, "content": "found", "data": {"items": items}},
        headers=auth(key),
    )
    assert observed.status_code in (200, 201), observed.text
    await worker.run_once()
    response = await client.get(
        f"/api/v1/rules/{created.json()['id']}/evaluations", headers=auth(key)
    )
    [evaluation] = response.json()["items"]
    return evaluation


async def _task(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{task_id}", headers=auth(key))
    assert response.status_code == 200, response.text
    task: dict[str, Any] = response.json()
    return task


async def test_work_goes_to_the_workspace_and_the_role_the_item_names(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    key = await _setup(client)
    tenders = await create_workspace(client, key, "tenders")
    lead = await create_role(client, key, "lead", workspace_id=tenders["id"])
    evaluation = await _run(
        client,
        worker,
        key,
        _rule(workspaceId="{{item.workspace}}", assignee="{{item.assignee}}"),
        [
            {"id": "a", "workspace": tenders["id"], "assignee": "role:lead"},
            # No workspace: the rule's own (a tenant rule has none).
            {"id": "b", "workspace": None, "assignee": None},
        ],
    )
    assert evaluation["status"] == "matched", evaluation
    work = {w["dedupKey"]: w for w in evaluation["result"]["work"]}

    in_tenders = await _task(client, key, work["sample:a"]["taskId"])
    assert in_tenders["workspaceId"] == tenders["id"]
    # For the role: nobody in particular, any holder of the role may take it.
    assert in_tenders["assigneeId"] is None
    requirements = await client.get(
        f"/api/v1/tasks/{in_tenders['id']}/requirements", headers=auth(key)
    )
    assert requirements.json()["roles"] == [{"id": lead["id"], "slug": "lead"}]

    default = await _task(client, key, work["sample:b"]["taskId"])
    assert default["workspaceId"] is None


async def test_a_role_the_workspace_does_not_have_fails_the_evaluation(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    key = await _setup(client)
    tenders = await create_workspace(client, key, "tenders")
    other = await create_workspace(client, key, "other")
    await create_role(client, key, "lead", workspace_id=other["id"])
    evaluation = await _run(
        client,
        worker,
        key,
        _rule(workspaceId=tenders["id"], assignee="role:lead"),
        [{"id": "a"}],
    )
    assert evaluation["status"] == "failed", evaluation
    assert evaluation["error"]["code"] == "unknown_role"
    assert evaluation["error"]["details"] == {"field": "action.fields.assignee", "role": "lead"}


async def test_a_workspace_that_is_not_an_id_fails_the_evaluation(
    client: httpx.AsyncClient, worker: Worker
) -> None:
    key = await _setup(client)
    evaluation = await _run(
        client,
        worker,
        key,
        _rule(workspaceId="{{item.workspace}}"),
        [{"id": "a", "workspace": "x"}],
    )
    assert evaluation["status"] == "failed", evaluation
    assert evaluation["error"]["code"] == "invalid_rule_field"
    assert evaluation["error"]["details"] == {"field": "action.fields.workspaceId"}


async def test_only_creating_actions_take_a_workspace(client: httpx.AsyncClient) -> None:
    key = await _setup(client)
    rule = _rule(workspaceId="{{item.workspace}}")
    rule["action"] = {
        "kind": "update_work",
        "forEach": "payload.data.items",
        "dedupKeyTemplate": "sample:{{item.id}}",
        "fields": {"title": "Sample {{item.id}}", "workspaceId": "{{item.workspace}}"},
    }
    response = await client.post("/api/v1/rules", json=rule, headers=auth(key))
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_rule_action"
    assert error["details"]["field"] == "action.fields.workspaceId"
