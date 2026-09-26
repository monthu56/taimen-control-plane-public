"""The core's knowledge requests against Memory's pinned request contract (CP-ADR-0060).

``tests/fixtures/memory_knowledge_contract.json`` holds the request models of
the Memory Service knowledge endpoints as its ``app.openapi()`` publishes them
(``ReconcileIn``, ``PackIn``, ``NamespaceKindsIn``) plus the snapshot fields
``domain.reconcile.parse_snapshot`` reads from the TOP level of ``ReconcileIn``.
The bodies are built by the same code the endpoints run (request schema ->
provider -> wire), captured by a mock transport and validated against it. The
live check against a running Memory Service is ``tests/contract``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from control_plane.api.v1.schemas import KNOWLEDGE_MAX_ITEMS, KnowledgeSnapshotRequest
from control_plane.application.commands import knowledge
from control_plane.domain.errors import ValidationError
from control_plane.infrastructure.context_provider.http import HttpContextProvider
from tests.helpers import knowledge_snapshot

CONTRACT = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "memory_knowledge_contract.json").read_text()
)


def _validate(schema: dict[str, Any], body: object) -> None:
    Draft202012Validator(schema).validate(body)


def _request_schema(path: str, method: str) -> dict[str, Any]:
    name = CONTRACT["paths"][path][method]["requestBody"]
    return CONTRACT["schemas"][name]


async def _captured(call: str, **kwargs: Any) -> httpx.Request:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    provider = HttpContextProvider(
        base_url="http://memory",
        api_key="k",
        timeout_seconds=1,
        ingest_timeout_seconds=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        await getattr(provider, call)(**kwargs)
    finally:
        await provider.aclose()
    [request] = seen
    return request


@pytest.mark.anyio
async def test_reconcile_body_is_memorys_reconcile_in() -> None:
    payload = KnowledgeSnapshotRequest.model_validate(
        {**knowledge_snapshot(), "workspaceId": "00000000-0000-0000-0000-000000000001"}
    )
    request = await _captured(
        "reconcile_snapshot",
        namespace="tenant:t:ws:r",
        scopes=["workspace:w"],
        snapshot=payload.snapshot_document(),
    )
    path = "/api/memory/reconcile"
    assert request.method == "POST" and request.url.path == path
    assert "post" in CONTRACT["paths"][path]
    body = json.loads(request.content)
    _validate(_request_schema(path, "post"), body)
    assert body["namespace"] == "tenant:t:ws:r"
    assert body["scopes"] == ["workspace:w"]
    # Memory reads the snapshot as ``model_dump(exclude={"namespace", "scopes"})``.
    document = {k: v for k, v in body.items() if k not in ("namespace", "scopes")}
    assert "snapshot" not in document
    _validate(CONTRACT["snapshotDocument"], document)
    assert set(document) <= set(CONTRACT["snapshotDocument"]["properties"])


def test_snapshot_bounds_match_memory() -> None:
    fields = CONTRACT["snapshotDocument"]["properties"]
    schema = KnowledgeSnapshotRequest.model_json_schema(by_alias=True)["properties"]
    for name in ("source", "snapshotId"):
        assert schema[name]["maxLength"] == fields[name]["maxLength"], name
    scope_types = {branch.get("type") for branch in schema["scope"]["anyOf"]}
    assert scope_types == {"string", "null"}
    assert [b["maxLength"] for b in schema["scope"]["anyOf"] if b.get("type") == "string"] == [
        fields["scope"]["maxLength"]
    ]
    assert CONTRACT["snapshotDocument"]["maxItemsTotal"] == KNOWLEDGE_MAX_ITEMS


@pytest.mark.anyio
async def test_package_body_is_memorys_pack_in() -> None:
    manifest = {"name": "selfdev", "version": 1, "kinds": [{"kind": "module"}]}
    request = await _captured("register_package", package=manifest)
    path = "/api/memory/packages"
    assert request.method == "POST" and request.url.path == path
    _validate(_request_schema(path, "post"), json.loads(request.content))


@pytest.mark.anyio
async def test_namespace_kinds_body_is_memorys_namespace_kinds_in() -> None:
    request = await _captured(
        "set_namespace_kinds", namespace="tenant:t:ws:r", packages=["selfdev@1"], strict=True
    )
    assert request.method == "PUT"
    assert request.url.path == "/api/memory/namespaces/tenant:t:ws:r/kinds"
    schema = _request_schema("/api/memory/namespaces/{namespace}/kinds", "put")
    body = json.loads(request.content)
    _validate(schema, body)
    # NamespaceKindsIn ignores unknown fields: a typo would silently drop the set.
    assert set(body) <= set(schema["properties"])


def test_pack_is_optional_on_both_sides() -> None:
    # Memory's snapshot ``pack`` defaults to "" (kinds from the namespace catalog).
    assert "pack" not in CONTRACT["snapshotDocument"]["required"]
    schema = KnowledgeSnapshotRequest.model_json_schema(by_alias=True)
    assert "pack" not in schema.get("required", [])
    payload = KnowledgeSnapshotRequest.model_validate(
        {
            **{k: v for k, v in knowledge_snapshot().items() if k != "pack"},
            "workspaceId": "00000000-0000-0000-0000-000000000001",
        }
    )
    document = payload.snapshot_document()
    assert "pack" not in document
    _validate(CONTRACT["snapshotDocument"], document)


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"name": "selfdev"},
        {"version": 1},
        {"name": "", "version": 1},
        {"name": "selfdev", "version": True},
        {"name": "selfdev", "version": None},
        {"name": 5, "version": "1"},
    ],
)
def test_manifest_without_identity_is_refused_before_memory(manifest: dict[str, Any]) -> None:
    # Whatever PackIn refuses with its own 422 the core refuses first as
    # pack_invalid, so the client never sees Memory's model error as a 502.
    with pytest.raises(ValidationError) as info:
        knowledge.require_pack_identity(manifest)
    assert info.value.code == "pack_invalid"


@pytest.mark.parametrize("version", ["1", 1, 1.5])
def test_manifest_with_identity_passes_pack_in(version: object) -> None:
    manifest = {"name": "selfdev", "version": version, "kinds": []}
    knowledge.require_pack_identity(manifest)
    _validate(_request_schema("/api/memory/packages", "post"), manifest)
