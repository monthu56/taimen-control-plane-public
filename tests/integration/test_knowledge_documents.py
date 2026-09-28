"""Knowledge base documents through the core (CP-ADR-0060 amendment 2026-09-28, K3; K009).

``POST /knowledge/documents`` stores text the caller already cut into chunks in
the namespace of the workspace tree root, visible to the workspace scope, with
the core's identity; the caller names neither. ``recall`` then finds the
document next to the entities it links to.
"""

import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from control_plane.application.context import graph
from tests.fake_graph_memory import FakeGraphMemory
from tests.helpers import (
    SECRET_TEXT,
    FakeKnowledge,
    auth,
    create_agent_with_key,
    create_workspace,
    do_bootstrap,
)

DOCUMENT_KEY = "document:license-1"


def _document(workspace_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "workspaceId": workspace_id,
        "naturalKey": DOCUMENT_KEY,
        "title": "License",
        "type": "license",
        "chunks": [
            {"text": SECRET_TEXT, "heading": "Terms", "order": 0},
            {"text": "Valid until 2027."},
        ],
        "links": [
            {"kind": "credential", "key": "credential:license-1", "rel": "evidenced_by"},
            {"kind": "organization", "key": "org:acme", "rel": "issued_to"},
        ],
        "meta": {"collection": "licenses"},
        **overrides,
    }


async def _setup(client: httpx.AsyncClient) -> tuple[dict[str, Any], str, str]:
    boot = await do_bootstrap(client)
    admin_key = boot["apiKey"]["key"]
    _, writer = await create_agent_with_key(
        client, admin_key, name="writer", permissions=["observations.write"]
    )
    return boot, admin_key, writer


async def _events(client: httpx.AsyncClient, key: str, event_type: str) -> list[dict[str, Any]]:
    items = (await client.get("/api/v1/events", params={"limit": 200}, headers=auth(key))).json()[
        "items"
    ]
    return [e for e in items if e["type"] == event_type]


async def test_a_document_needs_observations_write(client: httpx.AsyncClient, app) -> None:
    _, admin_key, _ = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    _, reader = await create_agent_with_key(
        client, admin_key, name="reader", permissions=["tasks.read"]
    )
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        response = await client.post(
            "/api/v1/knowledge/documents", json=_document(root["id"]), headers=auth(reader)
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["required"] == ["observations.write"]
    assert fake.calls == []
    assert await _events(client, admin_key, "knowledge.document_stored") == []


async def test_a_document_lands_in_the_root_namespace_with_the_workspace_scope(
    client: httpx.AsyncClient, app
) -> None:
    boot, admin_key, writer = await _setup(client)
    tenant_id = boot["tenant"]["id"]
    root = await create_workspace(client, admin_key, "root")
    child = await create_workspace(client, admin_key, "child", parent_id=root["id"])
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        response = await client.post(
            "/api/v1/knowledge/documents", json=_document(child["id"]), headers=auth(writer)
        )
    finally:
        app.state.context_provider = None
    assert response.status_code == 200, response.text
    # Memory's answer is passed through unchanged.
    assert response.json()["natural_key"] == DOCUMENT_KEY
    assert response.json()["chunks"] == 2

    [(kind, call)] = fake.calls
    assert kind == "document"
    assert call["namespace"] == f"tenant:{tenant_id}:ws:{root['id']}"
    assert call["scopes"] == [f"workspace:{child['id']}"]
    document = call["document"]
    assert document["natural_key"] == DOCUMENT_KEY
    assert document["type"] == "license"
    assert document["chunks"] == [
        {"text": SECRET_TEXT, "heading": "Terms", "order": 0},
        {"text": "Valid until 2027.", "heading": ""},
    ]
    assert document["meta"] == {"collection": "licenses"}
    assert document["replace"] is True
    # Memory links by natural key; the typed links travel whole in properties.
    assert document["links"] == ["credential:license-1", "org:acme"]
    assert document["properties"]["links"] == _document(child["id"])["links"]

    [event] = await _events(client, admin_key, "knowledge.document_stored")
    assert event["entityType"] == "workspace" and event["entityId"] == child["id"]
    assert event["payload"] == {
        "naturalKey": DOCUMENT_KEY,
        "title": "License",
        "type": "license",
        "workspaceId": child["id"],
        "rootWorkspaceId": root["id"],
        "namespace": call["namespace"],
        "chunkCount": 2,
        "linkCount": 2,
    }
    # The text never reaches the journal.
    assert SECRET_TEXT not in json.dumps(event)


async def test_a_document_without_links_or_meta_is_the_bare_ingest(
    client: httpx.AsyncClient, app
) -> None:
    _, admin_key, writer = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    fake = FakeKnowledge()
    app.state.context_provider = fake
    body = {k: v for k, v in _document(root["id"]).items() if k not in ("links", "meta", "type")}
    try:
        response = await client.post("/api/v1/knowledge/documents", json=body, headers=auth(writer))
    finally:
        app.state.context_provider = None
    assert response.status_code == 200, response.text
    [(_, call)] = fake.calls
    assert call["document"]["type"] == "document"
    assert {"links", "properties", "meta"}.isdisjoint(call["document"])
    [event] = await _events(client, admin_key, "knowledge.document_stored")
    assert event["payload"]["linkCount"] == 0


async def test_the_client_names_no_namespace_scope_or_file(client: httpx.AsyncClient, app) -> None:
    _, admin_key, writer = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    fake = FakeKnowledge()
    app.state.context_provider = fake
    try:
        rejected = [
            await client.post(
                "/api/v1/knowledge/documents",
                json=_document(root["id"], **extra),
                headers=auth(writer),
            )
            for extra in (
                {"namespace": "tenant:other:ws:x"},
                {"scopes": ["workspace:other"]},
                # The core parses no files: text arrives already extracted.
                {"file": "JVBERi0xLjQK"},
                {"chunks": []},
                {"chunks": [{"text": "x"}] * 501},
                {"links": [{"kind": "credential", "key": "k"}]},
            )
        ]
    finally:
        app.state.context_provider = None
    for response in rejected:
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"
    assert fake.calls == []


async def test_memory_failures_are_mapped(client: httpx.AsyncClient, app) -> None:
    _, admin_key, writer = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    body = _document(root["id"])

    async def post() -> httpx.Response:
        return await client.post("/api/v1/knowledge/documents", json=body, headers=auth(writer))

    try:
        app.state.context_provider = FakeKnowledge(fail_status=400)
        invalid = await post()
        app.state.context_provider = FakeKnowledge(fail_status=503, retryable=True)
        down = await post()
        app.state.context_provider = FakeKnowledge(fail_status=403, retryable=True)
        forbidden = await post()
    finally:
        app.state.context_provider = None
    no_provider = await post()
    unknown = await client.post(
        "/api/v1/knowledge/documents",
        json=_document("00000000-0000-0000-0000-000000000001"),
        headers=auth(writer),
    )

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "document_invalid"
    assert invalid.json()["error"]["details"] == {"memoryStatus": 400}
    assert down.status_code == 502
    assert down.json()["error"]["details"] == {"memoryStatus": 503, "retryable": True}
    assert forbidden.status_code == 502
    assert forbidden.json()["error"]["details"] == {"memoryStatus": 403, "retryable": False}
    for response in (invalid, down, forbidden):
        assert "memory said" not in response.text
    assert no_provider.status_code == 503
    assert no_provider.json()["error"]["code"] == "memory_disabled"
    assert unknown.status_code == 404
    # A refused document is not journaled.
    assert await _events(client, admin_key, "knowledge.document_stored") == []


async def test_a_document_takes_the_snapshot_body_limit(
    client: httpx.AsyncClient, app: FastAPI
) -> None:
    _, admin_key, writer = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    settings = app.state.settings
    filler = "x" * 5000
    count = settings.max_body_bytes * 2 // len(filler)
    app.state.context_provider = FakeKnowledge()
    try:
        big = await client.post(
            "/api/v1/knowledge/documents",
            json=_document(root["id"], chunks=[{"text": filler} for _ in range(count)]),
            headers=auth(writer),
        )
        too_big = await client.post(
            "/api/v1/knowledge/documents",
            content=b"{" + b" " * settings.knowledge_snapshot_max_body_bytes + b"}",
            headers={**auth(writer), "Content-Type": "application/json"},
        )
    finally:
        app.state.context_provider = None
    assert big.status_code == 200, big.text[:500]
    assert too_big.status_code == 413


# --- recall -----------------------------------------------------------------------


@pytest.fixture
def memory(app) -> Any:
    # Pack patterns are cached per process; each test starts cold.
    graph._pack_patterns.clear()
    fake = FakeGraphMemory()
    app.state.context_provider = fake
    yield fake
    app.state.context_provider = None


def _keys(pack: dict[str, Any]) -> set[str]:
    return {item["natural_key"] for s in pack["sections"] for item in s["items"]}


async def test_recall_finds_the_document_linked_to_its_entities(
    client: httpx.AsyncClient, memory: FakeGraphMemory
) -> None:
    """The acceptance of K009: a stored document is found by ``recall`` and
    tied to the entities it links (Memory's ``LINKS_TO`` edges)."""
    _, admin_key, writer = await _setup(client)
    root = await create_workspace(client, admin_key, "root")
    _, agent = await create_agent_with_key(
        client, admin_key, name="agent", permissions=["events.read", "tasks.read"]
    )
    # CP-0019 is an entity the graph already holds (a snapshot put it there).
    body = _document(
        root["id"],
        naturalKey="document:claims-guide",
        title="Claims guide",
        type="document",
        links=[{"kind": "adr", "key": "CP-0019", "rel": "evidenced_by"}],
    )
    stored = await client.post("/api/v1/knowledge/documents", json=body, headers=auth(writer))
    assert stored.status_code == 200, stored.text
    # What reached Memory's wire is its DocumentIngestRequest (checked by the fake).
    [request] = memory.document_requests
    assert request["properties"]["scopes"] == [f"workspace:{root['id']}"]

    from_entity = await client.post(
        "/api/v1/context/recall",
        json={
            "anchor": "CP-0019",
            "relations": ["links_to"],
            "direction": "in",
            "workspaceId": root["id"],
        },
        headers=auth(agent),
    )
    assert from_entity.status_code == 200, from_entity.text
    assert {"CP-0019", "document:claims-guide"} <= _keys(from_entity.json()["pack"])

    from_document = await client.post(
        "/api/v1/context/recall",
        json={
            "anchor": "document:claims-guide",
            "relations": ["links_to"],
            "direction": "out",
            "workspaceId": root["id"],
        },
        headers=auth(agent),
    )
    assert from_document.status_code == 200, from_document.text
    pack = from_document.json()["pack"]
    assert _keys(pack) == {"document:claims-guide", "CP-0019"}
    [fact] = pack["facts"]
    assert (fact["subject"], fact["relation"], fact["object"]) == (
        "document:claims-guide",
        "links_to",
        "CP-0019",
    )
