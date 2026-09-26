"""Task context profile, the task context pack as evidence and ``cp_recall`` (CP-ADR-0064).

Memory is ``tests.fake_graph_memory.FakeGraphMemory``: a small software-delivery
graph behind the pinned Memory contract, traversed the way the typed Context
Compiler traverses it.
"""

import json
from datetime import datetime
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from control_plane.application.context import graph
from tests.fake_graph_memory import FakeGraphMemory
from tests.helpers import (
    auth,
    claim_task,
    create_agent_with_key,
    create_task,
    create_workspace,
    do_bootstrap,
    open_session,
)

ENDPOINT = "POST /tasks/{}:claim"
CLIENT_CALLER = "control-plane:control_plane_client.client.ControlPlaneClient.claim_task"
UI_CALLER = "platform-web:src/api/tasks.ts:42"

LIFECYCLE = {
    "statuses": [
        {"key": "todo", "category": "active"},
        {"key": "in_progress", "category": "active"},
        {"key": "done", "category": "terminal_success"},
    ],
    "transitions": [
        {"from": "todo", "to": ["in_progress", "done"]},
        {"from": "in_progress", "to": ["todo", "done"]},
    ],
    "initialStatus": "todo",
    "claimStatus": "in_progress",
    "releaseStatus": "todo",
    "completionStatus": "done",
}

# The profile proposed for coding-task (software-delivery pack): endpoints and
# ADRs named in the description, and who calls the endpoints.
CODING_PROFILE: dict[str, Any] = {
    "anchors": [{"from": "description", "kinds": ["endpoint", "adr", "event", "table"]}],
    "traverse": [
        {"relation": "calls", "direction": "in", "depth": 1, "limit": 20},
        {"relation": "defined_in", "direction": "out", "depth": 1},
        {"relation": "governs", "direction": "in", "from": "previous"},
    ],
    "asOf": "taskCreated",
    "budgetTokens": 4000,
}

AGENT_PERMISSIONS = [
    "sessions.open",
    "tasks.read",
    "tasks.write",
    "tasks.claim",
    "events.read",
    "artifacts.read",
]


async def _type(client: httpx.AsyncClient, key: str, profile: dict[str, Any] | None, **extra: Any):
    body: dict[str, Any] = {"key": "coding-task", "displayName": "Coding", **extra}
    body["lifecycleSchema"] = LIFECYCLE
    if profile is not None:
        body["contextSchema"] = profile
    return await client.post("/api/v1/task-types", json=body, headers=auth(key))


async def _setup(client: httpx.AsyncClient, *, profile: dict[str, Any] | None = CODING_PROFILE):
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    created = await _type(client, admin_key, profile)
    assert created.status_code == 201, created.text
    workspace = await create_workspace(client, admin_key, "platform")
    agent, agent_key = await create_agent_with_key(client, admin_key, permissions=AGENT_PERMISSIONS)
    task = await create_task(
        client,
        admin_key,
        title="Strict query parameters",
        description=(
            "Reject unknown query parameters everywhere. Mind POST /tasks/{task_id}:claim "
            "and ADR-0019; unknown names like NOPE-1 stay unresolved."
        ),
        typeKey="coding-task",
        workspaceId=workspace["id"],
    )
    return {
        "boot": boot,
        "admin_key": admin_key,
        "type": created.json(),
        "workspace": workspace,
        "agent": agent,
        "agent_key": agent_key,
        "task": task,
    }


async def _claimed(client: httpx.AsyncClient, s: dict[str, Any]) -> dict[str, Any]:
    session = await open_session(client, s["agent_key"])
    response = await claim_task(client, s["agent_key"], s["task"]["id"], session["id"])
    assert response.status_code in (200, 201), response.text
    return response.json()


async def _context(client: httpx.AsyncClient, key: str, task_id: str) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/context", json={"task": task_id, "query": "continue"}, headers=auth(key)
    )
    assert response.status_code == 200, response.text
    return response.json()


def _moment(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _keys(pack: dict[str, Any]) -> set[str]:
    return {item["natural_key"] for s in pack["sections"] for item in s["items"]}


@pytest.fixture
def memory(app) -> Any:
    # Pack patterns are cached per process; each test starts cold.
    graph._pack_patterns.clear()
    fake = FakeGraphMemory()
    app.state.context_provider = fake
    yield fake
    app.state.context_provider = None


# --- the profile is part of the published type version -------------------------


async def test_profile_is_published_with_the_version(client: httpx.AsyncClient) -> None:
    boot = await do_bootstrap(client)
    created = await _type(client, boot["apiKey"]["key"], CODING_PROFILE)
    assert created.status_code == 201, created.text
    assert created.json()["contextSchema"] == CODING_PROFILE
    plain = await _type(client, boot["apiKey"]["key"], None)
    assert plain.json()["contextSchema"] == {}


@pytest.mark.parametrize(
    ("profile", "path"),
    [
        ({"anchors": []}, "contextSchema.anchors"),
        ({"anchors": [{"from": "summary"}]}, "contextSchema.anchors[0].from"),
        ({"anchors": [{"from": "$.approval.comment"}]}, "contextSchema.anchors[0].from"),
        (
            {"anchors": [{"from": "description", "kinds": ["End-Point"]}]},
            "contextSchema.anchors[0].kinds[0]",
        ),
        (
            {"anchors": [{"from": "description"}], "traverse": [{"relation": "calls", "depth": 9}]},
            "contextSchema.traverse[0].depth",
        ),
        (
            {
                "anchors": [{"from": "description"}],
                "traverse": [{"relation": "calls", "limit": 500}],
            },
            "contextSchema.traverse[0].limit",
        ),
        ({"anchors": [{"from": "description"}], "asOf": "yesterday"}, "contextSchema.asOf"),
        ({"anchors": [{"from": "description"}], "extra": 1}, "contextSchema"),
    ],
)
async def test_invalid_profile_is_refused_at_publication(
    client: httpx.AsyncClient, profile: dict[str, Any], path: str
) -> None:
    boot = await do_bootstrap(client)
    response = await _type(client, boot["apiKey"]["key"], profile)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_context_schema"
    assert error["details"]["path"] == path


async def test_profile_is_immutable_like_the_rest_of_the_version(
    client: httpx.AsyncClient, sync_engine: Engine
) -> None:
    boot = await do_bootstrap(client)
    created = (await _type(client, boot["apiKey"]["key"], CODING_PROFILE)).json()
    with pytest.raises(Exception, match="immutable"), sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE task_types SET context_schema = '{}'::jsonb WHERE id = :id"),
            {"id": created["id"]},
        )


# --- the pack of a claim -------------------------------------------------------------


async def test_task_mentioning_an_endpoint_gets_its_callers(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    claim = await _claimed(client, s)

    body = await _context(client, s["agent_key"], s["task"]["id"])
    task_context = body["taskContext"]
    assert task_context["status"] == "ok", task_context
    pack = task_context["pack"]
    # The endpoint named in the description, its callers (calls, in), where it
    # is defined and the ADR governing that file.
    assert {ENDPOINT, CLIENT_CALLER, UI_CALLER} <= _keys(pack)
    assert "CP-0019" in _keys(pack)
    assert "control-plane:src/control_plane/api/v1/claims.py" in _keys(pack)
    # A caller that had stopped calling before the task existed is not in it.
    assert "control-plane:legacy.claim" not in _keys(pack)
    # A templated endpoint is sent once, as written: Memory normalizes the
    # parameters itself (resolve_candidates) and says how it matched.
    assert {"kind": "endpoint", "value": "POST /tasks/{task_id}:claim"} not in pack["unresolved"]
    report = next(a for a in pack["anchors"] if a["input"]["value"].startswith("POST /tasks"))
    assert report["matchedBy"] == "natural_key"
    assert all("NOPE" not in a["value"] for a in task_context["anchors"])
    assert task_context["claimId"] == claim["id"]
    assert _moment(task_context["asOf"]) == _moment(s["task"]["createdAt"])
    assert task_context["budgetTokens"] == 4000

    # What was asked: deterministic candidates (no second, normalized form of a
    # templated endpoint), the moment pinned, the workspace namespace, the caller's
    # visibility (local mode narrows to the workspace and the principal).
    request = memory.typed_requests[-1]
    values = [a["value"] for a in request["anchors"]]
    assert values[0] == "POST /tasks/{task_id}:claim"
    assert ENDPOINT not in values
    assert "ADR-0019" in values
    assert _moment(request["as_of"]) == _moment(s["task"]["createdAt"])
    assert request["allow_semantic"] is False
    tenant = s["boot"]["tenant"]["id"]
    ws_namespace = f"tenant:{tenant}:ws:{s['workspace']['id']}"
    assert request["scope"]["namespaces"] == [f"tenant:{tenant}", ws_namespace]
    assert f"workspace:{s['workspace']['id']}" in request["allowedScopes"]
    # Visibility is Memory's to apply: an entity of another workspace is hidden.
    assert "secret-app:src/api.ts:1" not in _keys(pack)
    # Patterns came from Memory's pack registry, not from core.
    assert memory.package_requests == [("software-delivery", "1")]


async def test_pack_is_recorded_and_reproducible_from_the_record(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    claim = await _claimed(client, s)
    before = (
        await client.get(f"/api/v1/tasks/{s['task']['id']}", headers=auth(s["agent_key"]))
    ).json()
    first = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"]
    pack_id = first["contextPackId"]
    assert pack_id and first["recorded"] is True and first["replayed"] is False

    # Recording the pack does not write the task: an agent that read the task,
    # then its context, still updates it with the version it read.
    task = (
        await client.get(f"/api/v1/tasks/{s['task']['id']}", headers=auth(s["admin_key"]))
    ).json()
    assert task["version"] == before["version"] and task["evidence"] == []
    updated = await client.patch(
        f"/api/v1/tasks/{s['task']['id']}",
        json={
            "title": "Strict query parameters (in progress)",
            "claimId": claim["id"],
            "fencingToken": claim["fencingToken"],
        },
        headers={**auth(s["agent_key"]), "If-Match": f'"task-{before["version"]}"'},
    )
    assert updated.status_code == 200, updated.text

    events = (
        await client.get(
            "/api/v1/events",
            params={"entityId": s["task"]["id"]},
            headers=auth(s["admin_key"]),
        )
    ).json()["items"]
    recorded = [e for e in events if e["type"] == "task.context_pack_recorded"]
    assert recorded and recorded[0]["payload"]["contextPackId"] == pack_id
    assert recorded[0]["payload"]["claimId"] == first["claimId"]

    record = (
        await client.get(f"/api/v1/context-packs/{pack_id}", headers=auth(s["admin_key"]))
    ).json()
    assert _moment(record["asOf"]) == _moment(s["task"]["createdAt"])
    assert record["asOfMode"] == "taskCreated"
    assert {e["natural_key"] for e in record["used"]["entities"]} == _keys(first["pack"])
    assert set(record["used"]["facts"]) == {f["fact_id"] for f in first["pack"]["facts"]}
    assert record["used"]["snapshots"]
    assert _moment(record["request"]["as_of"]) == _moment(s["task"]["createdAt"])

    # The same claim reads the same pack: the recorded request is sent again.
    again = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"]
    assert again["contextPackId"] == pack_id and again["replayed"] is True
    assert _keys(again["pack"]) == _keys(first["pack"])
    assert memory.typed_requests[-1]["anchors"] == record["request"]["anchors"]

    # A reviewer, later: a caller stopped calling after the pack was compiled.
    # The pinned moment still reproduces what the executor saw.
    memory.close_edge("f-ui-claim", "2099-01-01T00:00:00+00:00")
    replay = await client.post(
        f"/api/v1/context-packs/{pack_id}:replay", headers=auth(s["admin_key"])
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["reproduced"] is True
    assert replay.json()["contextPack"]["id"] == pack_id

    # History rewritten under the moment (a fact closed before it) is drift.
    memory.close_edge("f-ui-claim", "2026-01-15T00:00:00+00:00")
    drifted = (
        await client.post(f"/api/v1/context-packs/{pack_id}:replay", headers=auth(s["admin_key"]))
    ).json()
    assert drifted["reproduced"] is False
    assert drifted["drift"]["missingFacts"] == ["f-ui-claim"]


async def test_new_claim_compiles_a_new_pack(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    claim = await _claimed(client, s)
    first = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"]
    released = await client.post(
        f"/api/v1/claims/{claim['id']}:release",
        json={"reason": "test"},
        headers=auth(s["agent_key"]),
    )
    assert released.status_code == 200, released.text
    await _claimed(client, s)
    second = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"]
    assert second["contextPackId"] != first["contextPackId"]
    assert second["replayed"] is False
    # Each claim has its record; re-claims add nothing to the task document.
    for pack in (first, second):
        record = await client.get(
            f"/api/v1/context-packs/{pack['contextPackId']}", headers=auth(s["admin_key"])
        )
        assert record.json()["claimId"] == pack["claimId"]
    task = (
        await client.get(f"/api/v1/tasks/{s['task']['id']}", headers=auth(s["admin_key"]))
    ).json()
    assert task["evidence"] == []


async def test_pack_is_not_recorded_for_a_claim_lost_while_compiling(
    client: httpx.AsyncClient, app
) -> None:
    """The claim is checked again under the task lock when the pack is written:
    a claim released while Memory was compiling records nothing."""
    s = await _setup(client)
    claim = await _claimed(client, s)

    class ReleasingMemory(FakeGraphMemory):
        async def typed_context(self, **kwargs: Any) -> dict[str, Any]:
            released = await client.post(
                f"/api/v1/claims/{claim['id']}:release",
                json={"reason": "test"},
                headers=auth(s["agent_key"]),
            )
            assert released.status_code == 200, released.text
            return await super().typed_context(**kwargs)

    app.state.context_provider = ReleasingMemory()
    try:
        task_context = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"]
    finally:
        app.state.context_provider = None
    assert task_context["status"] == "ok"
    assert task_context["recorded"] is False and task_context["contextPackId"] is None
    assert "the claim ended before the context pack was recorded" in task_context["warnings"]


async def test_pack_is_cut_to_the_profile_budget(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client, profile={**CODING_PROFILE, "budgetTokens": 30})
    await _claimed(client, s)
    task_context = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"]
    pack = task_context["pack"]
    assert pack["omitted"]["entities"] > 0
    record = (
        await client.get(
            f"/api/v1/context-packs/{task_context['contextPackId']}", headers=auth(s["admin_key"])
        )
    ).json()
    # The record keeps what the compilation used, the answer only what fits.
    assert len(record["used"]["entities"]) > len(_keys(pack))


async def test_reader_without_the_claim_gets_an_unrecorded_pack(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    await _claimed(client, s)
    body = await _context(client, s["admin_key"], s["task"]["id"])
    assert body["taskContext"]["status"] == "ok"
    assert body["taskContext"]["contextPackId"] is None
    assert body["taskContext"]["recorded"] is False
    task = (
        await client.get(f"/api/v1/tasks/{s['task']['id']}", headers=auth(s["admin_key"]))
    ).json()
    assert task["evidence"] == []


async def test_type_without_profile_keeps_the_previous_behaviour(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client, profile=None)
    await _claimed(client, s)
    body = await _context(client, s["agent_key"], s["task"]["id"])
    assert "taskContext" not in body
    assert body["memoryStatus"] == "ok"
    assert memory.typed_requests == [] and memory.kind_requests == []
    task = (
        await client.get(f"/api/v1/tasks/{s['task']['id']}", headers=auth(s["admin_key"]))
    ).json()
    assert task["evidence"] == []


async def test_memory_failure_degrades_the_pack_not_the_context(
    client: httpx.AsyncClient, app
) -> None:
    s = await _setup(client)
    await _claimed(client, s)
    app.state.context_provider = FakeGraphMemory(fail="typed")
    try:
        body = await _context(client, s["agent_key"], s["task"]["id"])
    finally:
        app.state.context_provider = None
    assert body["memoryStatus"] == "ok"
    assert body["taskContext"]["status"] == "unavailable"
    assert body["taskContext"]["pack"] is None
    # Without a provider the stanza says so, and the rest is unchanged.
    body = await _context(client, s["agent_key"], s["task"]["id"])
    assert body["taskContext"]["status"] == "disabled"


async def test_via_anchor_replaces_a_file_with_what_is_defined_in_it(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    profile = {
        "anchors": [
            {
                "from": "$.spawnedBy.artifact[commit].changedFiles",
                "kind": "source_file",
                "via": "defined_in",
            }
        ],
        "traverse": [{"relation": "calls", "direction": "in"}],
        "asOf": "now",
    }
    s = await _setup(client, profile=profile)
    source = await create_task(client, s["admin_key"], title="Source", typeKey="coding-task")
    artifact = await client.post(
        "/api/v1/artifacts",
        json={
            "type": "commit",
            "name": "commit",
            "task": source["id"],
            "metadata": {"changedFiles": ["control-plane:src/control_plane/api/v1/claims.py"]},
        },
        headers=auth(s["admin_key"]),
    )
    assert artifact.status_code == 201, artifact.text
    relation = await client.post(
        f"/api/v1/tasks/{s['task']['id']}/relations",
        json={"toTask": source["id"], "type": "spawned_by"},
        headers=auth(s["admin_key"]),
    )
    assert relation.status_code == 201, relation.text
    claim = await _claimed(client, s)

    task_context = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"]
    assert task_context["status"] == "ok", task_context
    assert task_context["anchors"] == [
        {
            "value": ENDPOINT,
            "kind": "endpoint",
            "source": "$.spawnedBy.artifact[commit].changedFiles",
            "via": "defined_in",
        }
    ]
    assert {CLIENT_CALLER, UI_CALLER} <= _keys(task_context["pack"])
    # asOf: now is the moment the claim was taken, not the moment of the read.
    assert _moment(task_context["asOf"]) == _moment(claim["acquiredAt"])

    # The pack was compiled with the holder's artifacts.read. A reader without
    # it sees the record without the anchors taken from artifact metadata.
    pack_id = task_context["contextPackId"]
    _, reader_key = await create_agent_with_key(
        client, s["admin_key"], name="reader", permissions=["tasks.read", "events.read"]
    )
    withheld = await client.get(f"/api/v1/context-packs/{pack_id}", headers=auth(reader_key))
    assert withheld.status_code == 200, withheld.text
    body = withheld.json()
    assert body["anchors"] == [] and body["request"]["anchors"] == []
    assert body["redactedAnchors"] == 1
    assert ENDPOINT not in {e["natural_key"] for e in body["used"]["entities"]}
    full = (
        await client.get(f"/api/v1/context-packs/{pack_id}", headers=auth(s["admin_key"]))
    ).json()
    assert full["redactedAnchors"] == 0 and len(full["anchors"]) == 1
    assert full["request"]["anchors"] == [{"kind": "endpoint", "value": ENDPOINT}]

    # Inside the same live claim POST /context replays the record for any
    # reader of the task — with the same redaction, never the values.
    sent = len(memory.typed_requests)
    replayed = (await _context(client, reader_key, s["task"]["id"]))["taskContext"]
    assert replayed["replayed"] is True and replayed["recorded"] is True
    assert replayed["contextPackId"] == pack_id
    assert replayed["anchors"] == [] and replayed["redactedAnchors"] == 1
    # Nothing is left to ask: Memory is not called with the hidden anchor.
    assert replayed["status"] == "empty" and replayed["pack"] is None
    assert len(memory.typed_requests) == sent
    assert ENDPOINT not in json.dumps(replayed)
    # The holder still replays the whole record.
    again = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"]
    assert again["replayed"] is True and again["redactedAnchors"] == 0
    assert again["anchors"] == task_context["anchors"]
    assert memory.typed_requests[-1]["anchors"] == [{"kind": "endpoint", "value": ENDPOINT}]
    # :replay with every anchor withheld sends nothing either.
    replay = await client.post(f"/api/v1/context-packs/{pack_id}:replay", headers=auth(reader_key))
    assert replay.status_code == 200, replay.text
    assert replay.json()["contextPack"]["redactedAnchors"] == 1
    assert len(memory.typed_requests) == sent + 1


async def test_pack_record_needs_events_read(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    """What a pack used is durable memory: tasks.read alone does not show it,
    as /context answers such a reader memoryStatus: forbidden."""
    s = await _setup(client)
    await _claimed(client, s)
    pack_id = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"][
        "contextPackId"
    ]
    _, reader_key = await create_agent_with_key(
        client, s["admin_key"], name="tasks-only", permissions=["tasks.read"]
    )
    record = await client.get(f"/api/v1/context-packs/{pack_id}", headers=auth(reader_key))
    assert record.status_code == 403, record.text
    replay = await client.post(f"/api/v1/context-packs/{pack_id}:replay", headers=auth(reader_key))
    assert replay.status_code == 403, replay.text


# --- evidence pointers -------------------------------------------------------------


async def test_context_pack_evidence_must_exist_in_the_tenant(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    await _claimed(client, s)
    pack_id = (await _context(client, s["agent_key"], s["task"]["id"]))["taskContext"][
        "contextPackId"
    ]
    other = await create_task(client, s["admin_key"], title="Other")
    unknown = await client.patch(
        f"/api/v1/tasks/{other['id']}",
        json={"evidence": [{"kind": "context_pack", "contextPackId": s["task"]["id"]}]},
        headers={**auth(s["admin_key"]), "If-Match": f'"task-{other["version"]}"'},
    )
    assert unknown.status_code == 404, unknown.text
    cited = await client.patch(
        f"/api/v1/tasks/{other['id']}",
        json={"evidence": [{"kind": "context_pack", "contextPackId": pack_id}]},
        headers={**auth(s["admin_key"]), "If-Match": f'"task-{other["version"]}"'},
    )
    assert cited.status_code == 200, cited.text


# --- cp_recall ------------------------------------------------------------------


async def test_recall_from_an_anchor_follows_the_named_relations(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    response = await client.post(
        "/api/v1/context/recall",
        json={
            "anchor": "POST /tasks/{task_id}:claim",
            "relations": ["calls"],
            "direction": "in",
            "task": s["task"]["id"],
        },
        headers=auth(s["agent_key"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert {ENDPOINT, CLIENT_CALLER, UI_CALLER} <= _keys(body["pack"])
    assert body["semantic"] is False
    request = memory.typed_requests[-1]
    assert request["traverse"] == [
        {"relation": "calls", "direction": "in", "depth": 1, "limit": 20}
    ]
    assert "as_of" not in request  # now, unless asked
    assert f"workspace:{s['workspace']['id']}" in request["allowedScopes"]


async def test_recall_from_a_query_extracts_identifiers_and_honours_as_of(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    s = await _setup(client)
    response = await client.post(
        "/api/v1/context/recall",
        json={
            "query": "who calls POST /tasks/{task_id}:claim?",
            "relations": ["calls"],
            "direction": "in",
            "asOf": "2026-01-10T00:00:00+00:00",
            "workspaceId": s["workspace"]["id"],
        },
        headers=auth(s["agent_key"]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # On 2026-01-10 the legacy caller still called the endpoint.
    assert "control-plane:legacy.claim" in _keys(body["pack"])
    assert body["anchors"][0] == {"kind": "endpoint", "value": "POST /tasks/{task_id}:claim"}

    nothing = await client.post(
        "/api/v1/context/recall",
        json={"query": "what about the weather", "workspaceId": s["workspace"]["id"]},
        headers=auth(s["agent_key"]),
    )
    assert nothing.json()["semantic"] is True
    assert memory.typed_requests[-1]["allow_semantic"] is True


async def test_recall_contract_errors(client: httpx.AsyncClient, app) -> None:
    s = await _setup(client)
    both = {"anchor": "x", "query": "y"}
    no_provider = await client.post(
        "/api/v1/context/recall", json={"anchor": "x"}, headers=auth(s["agent_key"])
    )
    assert no_provider.status_code == 503
    assert no_provider.json()["error"]["code"] == "memory_disabled"
    app.state.context_provider = FakeGraphMemory()
    try:
        invalid = await client.post(
            "/api/v1/context/recall", json=both, headers=auth(s["agent_key"])
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "invalid_recall_request"
        bad_relation = await client.post(
            "/api/v1/context/recall",
            json={"anchor": "x", "relations": ["CALLS"]},
            headers=auth(s["agent_key"]),
        )
        assert bad_relation.status_code == 400
        _, reader_key = await create_agent_with_key(
            client, s["admin_key"], name="no-memory", permissions=["tasks.read"]
        )
        forbidden = await client.post(
            "/api/v1/context/recall", json={"anchor": "x"}, headers=auth(reader_key)
        )
        assert forbidden.status_code == 403
        failing = FakeGraphMemory(fail="typed")
        app.state.context_provider = failing
        upstream = await client.post(
            "/api/v1/context/recall", json={"anchor": "x"}, headers=auth(s["agent_key"])
        )
        assert upstream.status_code == 502
    finally:
        app.state.context_provider = None


async def test_origin_moment_is_when_the_observation_was_seen(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    """asOf: origin reads the graph as it was when the fact that spawned the
    task was observed: then the legacy caller still called the endpoint."""
    s = await _setup(client, profile={**CODING_PROFILE, "asOf": "origin"})
    observed = await client.post(
        "/api/v1/observations",
        json={
            "kind": "external_fact",
            "content": "claim endpoint misbehaves",
            "source": "monitor",
            "observedAt": "2026-01-15T12:00:00+00:00",
        },
        headers=auth(s["admin_key"]),
    )
    assert observed.status_code == 201, observed.text
    task = await create_task(
        client,
        s["admin_key"],
        title="Investigate",
        description="POST /tasks/{task_id}:claim fails",
        typeKey="coding-task",
        workspaceId=s["workspace"]["id"],
        origin={
            "kind": "rule",
            "ruleId": "alerts",
            "evidence": [{"kind": "observation", "observationId": observed.json()["id"]}],
        },
    )
    s["task"] = task
    await _claimed(client, s)
    task_context = (await _context(client, s["agent_key"], task["id"]))["taskContext"]
    assert task_context["asOfMode"] == "origin"
    assert _moment(task_context["asOf"]) == _moment("2026-01-15T12:00:00+00:00")
    assert "control-plane:legacy.claim" in _keys(task_context["pack"])
