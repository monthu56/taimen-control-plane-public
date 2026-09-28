"""Fields of declarative-cycle published before their implementation (C002).

The request fields of the amendments to CP-ADR-0067 (default acceptance of a
task type, ``when`` of a check), CP-ADR-0063 (``identity`` of a rule) and
CP-ADR-0073 (``agent:<key>`` assignee) are in OpenAPI already. Until the step
that implements a field lands, a request using it answers ``501
not_implemented`` naming the field, and nothing is written; the same request
without the field goes through as before, and the new response fields read
empty. The acceptance of a task type and ``when`` landed with C004
(``test_declarative_cycle_acceptance.py``): here they are only taken in. The
identity of a rule landed with C005 (``test_rules_identity_relations.py``):
here it only has to name an agent of the registry. An ``agent:<key>`` assignee
is resolved since C006 (``test_agent_assignees.py``).
"""

from typing import Any

import httpx

from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from tests.helpers import auth, do_bootstrap

CHECK = {"key": "sample-check", "kind": "human", "description": "A person looks"}
CONDITIONAL_CHECK = {**CHECK, "when": ["$.task.customFields.sample"]}
TASK_TYPE = {"key": "sample-work", "displayName": "Sample work"}
RULE: dict[str, Any] = {
    "key": "sample-appeared",
    "trigger": {"kind": "observation", "type": "sample.appeared"},
    "action": {
        "kind": "ensure_work",
        "taskType": "task",
        "dedupKeyTemplate": "sample:{{payload.data.id}}",
        "fields": {"title": "Sample {{payload.data.id}}"},
    },
}


async def _count(client: httpx.AsyncClient, key: str, path: str) -> int:
    response = await client.get(path, headers=auth(key))
    assert response.status_code == 200, response.text
    return len(response.json()["items"])


async def test_task_type_acceptance_and_when_are_taken_in(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    body = {**TASK_TYPE, "lifecycleSchema": SYSTEM_TASK_LIFECYCLE}

    created = await client.post(
        "/api/v1/task-types",
        json={**body, "acceptance": [CHECK, {**CONDITIONAL_CHECK, "key": "other"}]},
        headers=auth(key),
    )
    assert created.status_code == 201, created.text
    assert created.json()["acceptance"] == [CHECK, {**CONDITIONAL_CHECK, "key": "other"}]

    task = await client.post(
        "/api/v1/tasks",
        json={"title": "Sample", "acceptance": [CONDITIONAL_CHECK]},
        headers=auth(key),
    )
    assert task.status_code == 201, task.text
    assert task.json()["acceptance"] == [CONDITIONAL_CHECK]
    patched = await client.patch(
        f"/api/v1/tasks/{task.json()['id']}",
        json={"acceptance": [CHECK, {**CONDITIONAL_CHECK, "key": "other"}]},
        headers={**auth(key), "If-Match": f'"task-{task.json()["version"]}"'},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["acceptance"][1]["when"] == CONDITIONAL_CHECK["when"]

    rule = {
        **RULE,
        "action": {**RULE["action"], "acceptance": [CONDITIONAL_CHECK]},
    }
    assert (await client.post("/api/v1/rules", json=rule, headers=auth(key))).status_code == 201


async def test_an_agent_reference_is_checked_for_form_first(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]
    response = await client.post(
        "/api/v1/tasks", json={"title": "Sample", "assigneeId": "agent:Coder"}, headers=auth(key)
    )
    assert response.status_code == 400, response.text


async def test_rule_identity_names_a_registry_agent(client: httpx.AsyncClient) -> None:
    key = (await do_bootstrap(client))["apiKey"]["key"]

    unknown = await client.post(
        "/api/v1/rules", json={**RULE, "identity": {"agent": "rules"}}, headers=auth(key)
    )
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["error"]["code"] == "unknown_agent"
    assert unknown.json()["error"]["details"] == {"field": "identity.agent", "agent": "rules"}
    assert await _count(client, key, "/api/v1/rules") == 0

    created = await client.post("/api/v1/rules", json=RULE, headers=auth(key))
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["identity"] is None
    headers = {**auth(key), "If-Match": f'"rule-{rule["version"]}"'}
    # null removes an identity; there is none, so it changes nothing by itself.
    cleared = await client.patch(
        f"/api/v1/rules/{rule['id']}",
        json={"identity": None, "description": "Samples"},
        headers=headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["identity"] is None
    assert cleared.json()["description"] == "Samples"
