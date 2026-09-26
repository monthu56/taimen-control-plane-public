"""``fields.customFields`` of the actions that file work (CP-ADR-0063, oss-sync amendment, B1).

What is under test:

*The rule computes the fields.* ``ensure_work`` and ``request_decision``
render ``fields.customFields`` from the facts and file the work with them; an
exact placeholder keeps the raw value, a value that resolved to nothing is
left out. Work found by the key keeps the fields it has.

*The form is checked when the rule is written, the values when work is
filed.* A document of the wrong shape is ``422 invalid_rule_action``; values
the task type's ``fieldSchema`` refuses fail the evaluation with
``custom_fields_invalid`` and file nothing.

Observation kinds and the task type are neutral (``sample.*``): core knows
no domain.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import auth, do_bootstrap

APPEARED = "sample.appeared"
CHANGED = "sample.changed"
DEDUP = "sample:{{payload.data.id}}"
TYPE_KEY = "sample-work"
FIELD_SCHEMA = {
    "type": "object",
    "properties": {
        "sampleId": {"type": "string", "pattern": "^[a-z]+$"},
        "size": {"type": "integer", "minimum": 1},
        "label": {"type": "string"},
    },
    "required": ["sampleId"],
    "additionalProperties": False,
}
CUSTOM_FIELDS = {
    "sampleId": "{{payload.data.id}}",
    "size": "{{payload.data.size}}",
    "label": "sample {{payload.data.id}} of {{payload.data.size}}",
}
FILE_RULE: dict[str, Any] = {
    "key": "sample-appeared",
    "trigger": {"kind": "observation", "type": APPEARED},
    "action": {
        "kind": "ensure_work",
        "taskType": TYPE_KEY,
        "dedupKeyTemplate": DEDUP,
        "fields": {"title": "Sample {{payload.data.id}}", "customFields": CUSTOM_FIELDS},
    },
}


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    response = await client.post(
        "/api/v1/task-types",
        json={
            "key": TYPE_KEY,
            "displayName": "Sample work",
            "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
            "fieldSchema": FIELD_SCHEMA,
        },
        headers=auth(key),
    )
    assert response.status_code == 201, response.text
    return {"key": key, "admin": boot["adminPrincipal"]["id"]}


async def _rule(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> dict[str, Any]:
    response = await client.post("/api/v1/rules", json=body, headers=auth(key))
    assert response.status_code == 201, response.text
    rule: dict[str, Any] = response.json()
    return rule


async def _observe(client: httpx.AsyncClient, key: str, kind: str, **data: Any) -> None:
    response = await client.post(
        "/api/v1/observations",
        json={"kind": kind, "content": f"{kind} seen", "data": data},
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text


async def _evaluations(client: httpx.AsyncClient, key: str, rule_id: str) -> list[dict[str, Any]]:
    response = await client.get(f"/api/v1/rules/{rule_id}/evaluations", headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def _task(client: httpx.AsyncClient, key: str, ref: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/tasks/{ref}", headers=auth(key))
    assert response.status_code == 200, response.text
    task: dict[str, Any] = response.json()
    return task


def _rule_task_ids(sync_engine: Engine) -> list[str]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id FROM tasks WHERE origin->>'kind' = 'rule' ORDER BY created_at")
        ).all()
    return [str(row.id) for row in rows]


async def test_a_rule_files_work_with_the_custom_fields_it_computed(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    rule = await _rule(client, s["key"], FILE_RULE)

    await _observe(client, s["key"], APPEARED, id="a", size=3)
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    task = await _task(client, s["key"], task_id)
    # An exact placeholder keeps the raw value: the schema gets an integer.
    assert task["customFields"] == {"sampleId": "a", "size": 3, "label": "sample a of 3"}
    assert task["typeKey"] == TYPE_KEY
    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "matched"

    # The same key again with other facts: the work found keeps its fields.
    await _observe(client, s["key"], APPEARED, id="a", size=7)
    await worker.run_once()
    assert _rule_task_ids(sync_engine) == [task_id]
    latest = (await _evaluations(client, s["key"], rule["id"]))[0]
    assert latest["result"]["work"][0]["created"] is False
    assert (await _task(client, s["key"], task_id))["customFields"] == task["customFields"]


async def test_a_value_that_resolved_to_nothing_is_left_out(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    await _rule(client, s["key"], FILE_RULE)

    # No size: the optional field is omitted, not filed as null.
    await _observe(client, s["key"], APPEARED, id="b")
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    assert (await _task(client, s["key"], task_id))["customFields"] == {
        "sampleId": "b",
        "label": "sample b of ",
    }


async def test_request_decision_files_its_work_with_custom_fields(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    action = {
        **FILE_RULE["action"],
        "kind": "request_decision",
        "fields": {**FILE_RULE["action"]["fields"], "approver": s["admin"]},
    }
    rule = await _rule(client, s["key"], {**FILE_RULE, "key": "sample-decision", "action": action})

    await _observe(client, s["key"], APPEARED, id="c", size=1)
    await worker.run_once()
    [task_id] = _rule_task_ids(sync_engine)
    assert (await _task(client, s["key"], task_id))["customFields"]["sampleId"] == "c"
    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "matched"
    assert evaluation["result"]["work"][0]["approvalId"]


@pytest.mark.parametrize(
    ("observed", "field"),
    [
        # A value the pattern refuses.
        ({"id": "NOT-LOWER", "size": 2}, "sampleId"),
        # A value of the wrong type: the raw value of an exact placeholder.
        ({"id": "d", "size": "big"}, "size"),
        # A required field that resolved to nothing: reported missing.
        ({"size": 2}, "sampleId"),
    ],
)
async def test_values_outside_the_field_schema_fail_the_evaluation_and_file_nothing(
    client: httpx.AsyncClient,
    worker: Worker,
    sync_engine: Engine,
    observed: dict[str, Any],
    field: str,
) -> None:
    s = await _setup(client)
    rule = await _rule(
        client,
        s["key"],
        {**FILE_RULE, "action": {**FILE_RULE["action"], "dedupKeyTemplate": "sample:fixed"}},
    )

    await _observe(client, s["key"], APPEARED, **observed)
    await worker.run_once()
    assert _rule_task_ids(sync_engine) == []
    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "custom_fields_invalid"
    assert field in str(evaluation["error"])


@pytest.mark.parametrize(
    ("action", "field"),
    [
        (
            {**FILE_RULE["action"], "fields": {"title": "t", "customFields": "{{payload.data}}"}},
            "action.fields.customFields",
        ),
        (
            {**FILE_RULE["action"], "fields": {"title": "t", "customFields": {"size": 3}}},
            "action.fields.customFields.size",
        ),
        (
            {**FILE_RULE["action"], "fields": {"title": "t", "customFields": {"a-b": "x"}}},
            "action.fields.customFields.a-b",
        ),
        # Work found by the key keeps its fields: update_work does not take them.
        (
            {
                "kind": "update_work",
                "dedupKeyTemplate": DEDUP,
                "fields": {"customFields": {"label": "{{payload.data.label}}"}},
            },
            "action.fields.customFields",
        ),
    ],
)
async def test_custom_fields_of_the_wrong_form_are_refused_when_the_rule_is_written(
    client: httpx.AsyncClient, action: dict[str, Any], field: str
) -> None:
    s = await _setup(client)
    body = {**FILE_RULE, "trigger": {"kind": "observation", "type": CHANGED}, "action": action}
    response = await client.post("/api/v1/rules", json=body, headers=auth(s["key"]))
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_rule_action"
    assert error["details"]["field"] == field

    # The same holds when a rule is edited.
    rule = await _rule(client, s["key"], FILE_RULE)
    patched = await client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"action": action},
        headers={**auth(s["key"]), "If-Match": f'"rule-{rule["version"]}"'},
    )
    assert patched.status_code == 422, patched.text
    assert patched.json()["error"]["details"]["field"] == field
