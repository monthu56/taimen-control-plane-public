"""A rule acting as an agent, filing typed and related work (CP-ADR-0063, amendment 2026-09-27).

What is under test (declarative-cycle C005):

*G1 — identity.* A rule may name an agent of the registry; its evaluations
then run with the authority of that agent's principal and IAM binding, and
the work it files is authored by that principal. Writing such a rule needs
every permission the agent holds; an agent that lacks a right the rule needs
fails the evaluation, one that is not linked yet fails it
``credential_inactive``.

*G2/G3/G5 — a type and relations per item.* ``ensure_work`` over ``forEach``
takes the task type from the item (within ``taskTypes``), links the work to
the task that spawned it and to the work of other dedup keys — of the same
evaluation or filed before. An item that cannot be filed is refused alone;
evaluating the same document again files neither work nor relations twice.

*G4.* The ``task`` view of a rule carries ``typeKey`` and ``typeVersion``.

Observation kinds and task types are neutral (``sample.*``): core knows no domain.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.config import Settings
from control_plane.domain.work_item import SYSTEM_TASK_LIFECYCLE
from control_plane.worker.main import Worker
from tests.helpers import auth, create_agent_with_key, create_task, do_bootstrap

APPEARED = "sample.appeared"
LISTED = "sample.listed"
AGENT = "sample-rules"
ISSUER = "https://iam.example.test"
RULE_PERMISSIONS = ["events.read", "tasks.read", "tasks.write"]
TYPES = ["sample-work", "sample-check"]


@pytest.fixture
async def worker(settings: Settings) -> AsyncIterator[Worker]:
    instance = Worker(settings)
    yield instance
    await instance.engine.dispose()


def _agent_spec(permissions: list[str]) -> dict[str, Any]:
    return {
        "displayName": "Sample rules",
        "identity": {"kind": "service", "permissions": permissions},
        "placement": "none",
    }


async def _publish_agent(
    client: httpx.AsyncClient, key: str, permissions: list[str], agent: str = AGENT
) -> None:
    response = await client.post(
        "/api/v1/agents",
        json={"key": agent, "spec": _agent_spec(permissions)},
        headers=auth(key),
    )
    assert response.status_code in (200, 201), response.text


async def _link_agent(client: httpx.AsyncClient, admin_key: str, agent: str = AGENT) -> str:
    _, fleet_key = await create_agent_with_key(
        client,
        admin_key,
        name=f"fleet-{uuid.uuid4().hex[:6]}",
        kind="service",
        permissions=["agents.read", "agents.status.write"],
    )
    response = await client.put(
        f"/api/v1/agents/{agent}/identity",
        json={
            "issuer": ISSUER,
            "iamTenantId": str(uuid.uuid4()),
            "iamPrincipalId": str(uuid.uuid4()),
        },
        headers=auth(fleet_key),
    )
    assert response.status_code == 200, response.text
    principal_id: str = response.json()["principalId"]
    return principal_id


async def _setup(client: httpx.AsyncClient) -> dict[str, Any]:
    boot = await do_bootstrap(client)
    key: str = boot["apiKey"]["key"]
    for type_key in TYPES:
        response = await client.post(
            "/api/v1/task-types",
            json={
                "key": type_key,
                "displayName": type_key,
                "lifecycleSchema": SYSTEM_TASK_LIFECYCLE,
            },
            headers=auth(key),
        )
        assert response.status_code == 201, response.text
    return {"key": key, "admin": boot["adminPrincipal"]["id"]}


async def _rule(client: httpx.AsyncClient, key: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/api/v1/rules", json=body, headers=auth(key))


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


async def _events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    response = await client.get("/api/v1/events", params={"types": event_type}, headers=auth(key))
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


def _rule_work(sync_engine: Engine) -> dict[str, dict[str, Any]]:
    """Work filed by rules, by dedup key: its task, type and author."""
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT w.dedup_key, t.id, t.created_by, tt.key AS type_key
                  FROM rule_work_items w
                  JOIN tasks t ON t.id = w.task_id
                  JOIN task_types tt ON tt.id = t.type_id
                """
            )
        ).all()
    return {
        row.dedup_key: {"id": str(row.id), "createdBy": str(row.created_by), "type": row.type_key}
        for row in rows
    }


def _relations(sync_engine: Engine) -> set[tuple[str, str, str]]:
    with sync_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT from_task_id, to_task_id, relation_type FROM task_relations")
        ).all()
    return {(str(r.from_task_id), str(r.to_task_id), r.relation_type) for r in rows}


FILE_RULE: dict[str, Any] = {
    "key": "sample-appeared",
    "trigger": {"kind": "observation", "type": APPEARED},
    "action": {
        "kind": "ensure_work",
        "taskType": "sample-work",
        "dedupKeyTemplate": "sample:{{payload.data.id}}",
        "fields": {"title": "Sample {{payload.data.id}}"},
    },
}


# --- G1: identity ------------------------------------------------------------------


async def test_a_rule_with_identity_files_work_as_its_agent(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    await _publish_agent(client, s["key"], RULE_PERMISSIONS)
    agent_principal = await _link_agent(client, s["key"])

    created = await _rule(client, s["key"], {**FILE_RULE, "identity": {"agent": AGENT}})
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["identity"] == {"agent": AGENT}
    [rule_created] = await _events(client, s["key"], "rule.created")
    assert rule_created["actorId"] == s["admin"]

    await _observe(client, s["key"], APPEARED, id="a")
    await worker.run_once()

    work = _rule_work(sync_engine)
    assert work["sample:a"]["createdBy"] == agent_principal
    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "matched", evaluation
    [derived] = await _events(client, s["key"], "work.derived")
    assert derived["actorId"] == agent_principal
    [task_created] = [
        e
        for e in await _events(client, s["key"], "task.created")
        if e["entityId"] == work["sample:a"]["id"]
    ]
    assert task_created["actorId"] == agent_principal
    [evaluated] = await _events(client, s["key"], "rule.evaluated")
    assert evaluated["actorId"] == agent_principal


async def test_a_rule_whose_agent_lacks_a_right_fails_its_evaluation(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    # The agent may read facts and tasks, but not file work.
    await _publish_agent(client, s["key"], ["events.read", "tasks.read"])
    await _link_agent(client, s["key"])
    rule = (await _rule(client, s["key"], {**FILE_RULE, "identity": {"agent": AGENT}})).json()

    await _observe(client, s["key"], APPEARED, id="a")
    await worker.run_once()

    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "permission_denied"
    assert evaluation["error"]["details"] == {"required": ["tasks.write"]}
    assert _rule_work(sync_engine) == {}
    assert await _events(client, s["key"], "work.derived") == []


async def test_a_rule_whose_agent_is_not_linked_fails_credential_inactive(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    await _publish_agent(client, s["key"], RULE_PERMISSIONS)
    # Written before the placement service linked the agent: allowed (G1).
    rule = (await _rule(client, s["key"], {**FILE_RULE, "identity": {"agent": AGENT}})).json()

    await _observe(client, s["key"], APPEARED, id="a")
    await worker.run_once()

    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "credential_inactive"
    assert evaluation["error"]["details"] == {"agent": AGENT}
    assert _rule_work(sync_engine) == {}


async def test_writing_a_rule_as_an_agent_needs_the_agents_rights(
    client: httpx.AsyncClient,
) -> None:
    s = await _setup(client)
    await _publish_agent(client, s["key"], RULE_PERMISSIONS)
    _, writer = await create_agent_with_key(
        client,
        s["key"],
        name="rule-writer",
        kind="service",
        permissions=["rules.write", "rules.read", "events.read", "tasks.read"],
    )

    refused = await _rule(client, writer, {**FILE_RULE, "identity": {"agent": AGENT}})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "permission_escalation"
    assert refused.json()["error"]["details"]["missing"] == ["tasks.write"]

    # Without an identity the writer may write it; it cannot then lend itself one.
    rule = (await _rule(client, writer, FILE_RULE)).json()
    headers = {**auth(writer), "If-Match": f'"rule-{rule["version"]}"'}
    patched = await client.patch(
        f"/api/v1/rules/{rule['id']}", json={"identity": {"agent": AGENT}}, headers=headers
    )
    assert patched.status_code == 403, patched.text

    # An admin may; the identity is a change of what the rule does.
    headers = {**auth(s["key"]), "If-Match": f'"rule-{rule["version"]}"'}
    patched = await client.patch(
        f"/api/v1/rules/{rule['id']}", json={"identity": {"agent": AGENT}}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["identity"], patched.json()["version"]) == ({"agent": AGENT}, 2)
    # Once the rule acts as the agent, any change by the writer needs the rights too.
    headers = {**auth(writer), "If-Match": '"rule-2"'}
    described = await client.patch(
        f"/api/v1/rules/{rule['id']}", json={"description": "Samples"}, headers=headers
    )
    assert described.status_code == 403, described.text

    headers = {**auth(s["key"]), "If-Match": '"rule-2"'}
    cleared = await client.patch(
        f"/api/v1/rules/{rule['id']}", json={"identity": None}, headers=headers
    )
    assert (cleared.json()["identity"], cleared.json()["version"]) == (None, 3)
    updated = await _events(client, s["key"], "rule.updated")
    assert [e["payload"]["changes"] for e in updated] == [["identity"], ["identity"]]


async def test_a_retired_agent_stops_its_rules_and_cannot_be_named(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    await _publish_agent(client, s["key"], RULE_PERMISSIONS)
    await _link_agent(client, s["key"])
    rule = (await _rule(client, s["key"], {**FILE_RULE, "identity": {"agent": AGENT}})).json()
    response = await client.post(
        f"/api/v1/agents/{AGENT}:retire", json={"reason": "replaced"}, headers=auth(s["key"])
    )
    assert response.status_code == 200, response.text

    await _observe(client, s["key"], APPEARED, id="a")
    await worker.run_once()
    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert (evaluation["status"], evaluation["error"]["code"]) == ("failed", "credential_inactive")
    assert _rule_work(sync_engine) == {}

    refused = await _rule(
        client, s["key"], {**FILE_RULE, "key": "sample-again", "identity": {"agent": AGENT}}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["code"] == "unknown_agent"


# --- G2/G3/G5: a type and relations per item ---------------------------------------------

EXPAND_RULE: dict[str, Any] = {
    "key": "sample-expand",
    "trigger": {"kind": "observation", "type": LISTED},
    "action": {
        "kind": "ensure_work",
        "forEach": "payload.data.items",
        "taskType": "{{item.type}}",
        "taskTypes": TYPES,
        "dedupKeyTemplate": "sample:{{item.id}}",
        "fields": {
            "title": "Sample {{item.id}}",
            "relations": {
                "spawnedBy": "{{payload.data.parent}}",
                "dependsOn": "{{item.dependsOn}}",
            },
        },
    },
}


def _item(item_id: str, type_key: str = "sample-work", *deps: str) -> dict[str, Any]:
    return {"id": item_id, "type": type_key, "dependsOn": [f"sample:{d}" for d in deps]}


async def test_for_each_files_typed_related_work_once(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    parent = await create_task(client, s["key"], title="The document")
    rule = (await _rule(client, s["key"], EXPAND_RULE)).json()
    # "b" names "a", which comes later in the list: the whole evaluation resolves.
    items = [
        _item("b", "sample-work", "a"),
        _item("a", "sample-check"),
        _item("c", "sample-work", "a", "b"),
    ]

    await _observe(client, s["key"], LISTED, parent=parent["publicId"], items=items)
    await worker.run_once()

    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "matched", evaluation
    work = _rule_work(sync_engine)
    assert {key: w["type"] for key, w in work.items()} == {
        "sample:a": "sample-check",
        "sample:b": "sample-work",
        "sample:c": "sample-work",
    }
    a, b, c = (work[f"sample:{x}"]["id"] for x in "abc")
    expected = {
        (a, parent["id"], "spawned_by"),
        (b, parent["id"], "spawned_by"),
        (c, parent["id"], "spawned_by"),
        (b, a, "depends_on"),
        (c, a, "depends_on"),
        (c, b, "depends_on"),
    }
    assert _relations(sync_engine) == expected
    # A dependent is not handed out before its dependency is done.
    claimability = await client.get(f"/api/v1/tasks/{b}/claimability", headers=auth(s["key"]))
    assert claimability.status_code == 200, claimability.text
    assert "task_not_ready" in {r["code"] for r in claimability.json()["reasons"]}

    # The same document again: nothing new, neither work nor relations.
    await _observe(client, s["key"], LISTED, parent=parent["publicId"], items=items)
    await worker.run_once()
    second, _ = await _evaluations(client, s["key"], rule["id"])
    assert second["status"] == "matched"
    assert [w.get("created") for w in second["result"]["work"]] == [False, False, False]
    assert _rule_work(sync_engine) == work
    assert _relations(sync_engine) == expected

    # A later document depends on work filed before: the journal resolves it.
    await _observe(
        client, s["key"], LISTED, parent=parent["publicId"], items=[_item("d", "sample-work", "c")]
    )
    await worker.run_once()
    d = _rule_work(sync_engine)["sample:d"]["id"]
    assert _relations(sync_engine) == expected | {
        (d, parent["id"], "spawned_by"),
        (d, c, "depends_on"),
    }


async def test_items_that_cannot_be_filed_are_refused_alone(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    parent = await create_task(client, s["key"], title="The document")
    rule = (await _rule(client, s["key"], EXPAND_RULE)).json()
    items = [
        _item("ok"),
        _item("typo", "task"),
        _item("after-typo", "sample-work", "typo"),
        _item("ghost-dep", "sample-work", "ghost"),
        _item("x", "sample-work", "y"),
        _item("y", "sample-work", "x"),
        _item("self", "sample-work", "self"),
        _item("after-ok", "sample-work", "ok"),
    ]

    await _observe(client, s["key"], LISTED, parent=parent["id"], items=items)
    await worker.run_once()

    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "matched", evaluation
    outcome = {w["dedupKey"]: w.get("refused") for w in evaluation["result"]["work"]}
    assert outcome == {
        "sample:ok": None,
        "sample:typo": "task_type_not_allowed",
        "sample:after-typo": "dependency_refused",
        "sample:ghost-dep": "dependency_not_found",
        "sample:x": "dependency_cycle",
        "sample:y": "dependency_cycle",
        "sample:self": "dependency_cycle",
        "sample:after-ok": None,
    }
    assert set(_rule_work(sync_engine)) == {"sample:ok", "sample:after-ok"}
    [evaluated] = await _events(client, s["key"], "rule.evaluated")
    assert {w["dedupKey"]: w.get("refused") for w in evaluated["payload"]["work"]} == outcome


async def test_an_evaluation_whose_every_item_is_refused_fails(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    rule = (await _rule(client, s["key"], EXPAND_RULE)).json()

    await _observe(
        client, s["key"], LISTED, parent="TASK-999999", items=[_item("a"), _item("b", "task")]
    )
    await worker.run_once()

    [evaluation] = await _evaluations(client, s["key"], rule["id"])
    assert evaluation["status"] == "failed"
    assert evaluation["error"]["code"] == "work_items_refused"
    assert evaluation["error"]["details"] == {
        "refused": ["relation_target_not_found", "task_type_not_allowed"]
    }
    assert _rule_work(sync_engine) == {}


@pytest.mark.parametrize(
    ("change", "code", "field"),
    [
        ({"taskTypes": None}, "invalid_rule_action", "action.taskType"),
        ({"taskTypes": ["sample-work", "nope"]}, "unknown_task_type", "action.taskTypes[1]"),
        ({"taskTypes": ["sample-work", "sample-work"]}, "invalid_rule_action", "action.taskTypes"),
        (
            {"taskType": "sample-check", "taskTypes": ["sample-work"]},
            "invalid_rule_action",
            "action.taskType",
        ),
        (
            {"fields": {"title": "t", "relations": {"blocks": "x"}}},
            "invalid_rule_action",
            "action.fields.relations",
        ),
        (
            {"fields": {"title": "t", "relations": {"dependsOn": ["{{nowhere.key}}"]}}},
            "invalid_rule_action",
            "action.fields.relations.dependsOn[0]",
        ),
        (
            {"fields": {"title": "t", "relations": {"dependsOn": ["k"] * 51}}},
            "invalid_rule_action",
            "action.fields.relations.dependsOn",
        ),
    ],
)
async def test_types_and_relations_are_checked_when_the_rule_is_written(
    client: httpx.AsyncClient, change: dict[str, Any], code: str, field: str
) -> None:
    s = await _setup(client)
    action = {k: v for k, v in {**EXPAND_RULE["action"], **change}.items() if v is not None}
    response = await _rule(client, s["key"], {**EXPAND_RULE, "action": action})
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert (error["code"], error["details"]["field"]) == (code, field)


@pytest.mark.parametrize("kind", ["update_work", "request_decision"])
async def test_only_ensure_work_takes_relations_and_task_types(
    client: httpx.AsyncClient, kind: str
) -> None:
    s = await _setup(client)
    action: dict[str, Any] = {
        "kind": kind,
        "dedupKeyTemplate": "sample:{{payload.data.id}}",
        "taskType": "sample-work",
        "taskTypes": TYPES,
        "fields": {"title": "t"},
    }
    if kind == "update_work":
        action.pop("taskType")
    else:
        action["fields"]["approver"] = s["admin"]
    response = await _rule(client, s["key"], {**FILE_RULE, "action": action})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["field"] == "action.taskTypes"

    action.pop("taskTypes")
    action["fields"]["relations"] = {"dependsOn": "x"}
    response = await _rule(client, s["key"], {**FILE_RULE, "action": action})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["field"] == "action.fields.relations"


# --- G4: the type in the task view -------------------------------------------------------


async def test_a_condition_tells_task_types_apart(
    client: httpx.AsyncClient, worker: Worker, sync_engine: Engine
) -> None:
    s = await _setup(client)
    rule = (
        await _rule(
            client,
            s["key"],
            {
                "key": "sample-follow-up",
                "trigger": {"kind": "event", "type": "task.created"},
                "condition": {
                    "and": [
                        {"eq": [{"var": "task.typeKey"}, "sample-check"]},
                        {"eq": [{"var": "task.typeVersion"}, 1]},
                    ]
                },
                "action": {
                    "kind": "ensure_work",
                    "taskType": "sample-work",
                    "dedupKeyTemplate": "follow:{{task.id}}",
                    "fields": {"title": "Follow up {{task.publicId}} ({{task.typeKey}})"},
                },
            },
        )
    ).json()
    checked = await client.post(
        "/api/v1/tasks",
        json={"title": "Checked", "typeKey": "sample-check"},
        headers=auth(s["key"]),
    )
    assert checked.status_code == 201, checked.text
    await create_task(client, s["key"], title="Plain")
    await worker.run_once()

    evaluations = await _evaluations(client, s["key"], rule["id"])
    assert sorted(e["status"] for e in evaluations) == ["matched", "not_matched"]
    assert set(_rule_work(sync_engine)) == {f"follow:{checked.json()['id']}"}
